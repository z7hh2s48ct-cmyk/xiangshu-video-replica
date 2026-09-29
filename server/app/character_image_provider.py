"""真实角色图片 Provider：复用 Apilio 图像编辑客户端，按标准视角逐张出图。

骨架契约（``CharacterImageProvider.generate_view``）是一次调用即产出图片的
同步语义，与 Apilio 的同步 edit 接口一致——供应商侧的异步任务链不引入本
链路。prompt 由冻结的 persona 快照与视角片段组合而成：同一版本内五个视角
共享同一份身份描述，避免各自漂移。

依赖方向：本模块顶层导入 ``character_image_generation`` 的类型与错误类；
反向的引用（注册表、任务创建校验）由对方在函数体内延迟导入，避免成环。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import cast

from app.character_contracts import RequiredCharacterViewType
from app.character_image_generation import (
    CharacterImageProviderFailed,
    CharacterImageRequest,
    CharacterImageResult,
)
from app.db_portable import BusinessConnection
from app.first_frames import (
    APILIO_DEFAULT_BASE_URL,
    FIRST_FRAME_IMAGE_CONTENT_TYPES,
    FIRST_FRAME_MODELS,
    ApilioImageProvider,
    FirstFrameModel,
    ImageInput,
    ImageProviderFailed,
    RetryableImageProviderFailed,
)
from app.settings import SettingsRepository, SettingsUnavailableError

APILIO_CHARACTER_PROVIDER = "apilio"

# 单视角竖版 2:3。取 1024x1536：宽高都能被 16 整除、总像素落在网关非实验
# 档位内（见 first_frames.FIRST_FRAME_IMAGE_SIZES 注释里的硬约束），且与假
# Provider 的默认出图尺寸一致，联调与验收时看到的画面比例相同。
CHARACTER_VIEW_PIXEL_SIZE = "1024x1536"
CHARACTER_VIEW_ASPECT_RATIO = "2:3"
# 与首帧链路共用同一份网关输出类型白名单，避免两处漂移。
CHARACTER_ALLOWED_OUTPUT_TYPES = FIRST_FRAME_IMAGE_CONTENT_TYPES

# 视角片段沿用五视图契约的英文命名（与 simple_character 拼合图的 panel 描述
# 同源），让模型在单张出图与整张拼合图两条链路上看到一致的口径。
CHARACTER_VIEW_PROMPT_FRAGMENTS: dict[RequiredCharacterViewType, str] = {
    "FRONT_FACE": (
        "View: head-and-shoulders close-up, fully frontal, face and upper chest "
        "clearly visible with safe margins."
    ),
    "FRONT_HALF": (
        "View: half-body front view cropped at the waist, body fully facing the camera."
    ),
    "FRONT_FULL": (
        "View: full-body front view, feet and head fully visible with safe margins, "
        "arms relaxed naturally at the sides, feet parallel."
    ),
    "LEFT_45": (
        "View: full-body three-quarter view turned 45 degrees to the camera's left, "
        "complete body silhouette from crown to soles."
    ),
    "LEFT_SIDE": (
        "View: full-body left profile view, complete silhouette from crown to soles, "
        "face seen from the side."
    ),
}

# 身份与摄影不变量：与 SIMPLE_CONTACT_SHEET_PROMPT 的关键段落同口径，保证
# 单张出图同样「像本人、像真人、像棚拍」。
CHARACTER_IDENTITY_INVARIANTS = """\
Identity invariants: the exact same person as the uploaded photo; preserve facial \
proportions and recognizable appearance, including eye shape, nose, lips, skin tone, \
hairstyle and hair length, headwear, visible jewelry, makeup level, body build and \
clothing exactly as shown. Where the photo does not show the lower body, extend the \
visible outfit simply with plain trousers and plain low-profile closed shoes; no belt, \
bag, visible brand or extra accessories. Keep body proportions realistic. Real human \
skin with visible pores and natural texture; individual hair strands; realistic fabric \
weave. Neutral relaxed expression; eyes level when visible. Seamless neutral gray \
studio backdrop without hot spots or center glare. Shot on a Canon EOS R5 with an \
85mm f/1.8 lens, soft even studio lighting, mild floor grounding shadow. No text, \
labels, logos, watermark, props, furniture, room background, extra people, malformed \
hands, extra limbs, or beauty-filter effects."""


class ApilioCharacterImageProvider:
    provider_name = APILIO_CHARACTER_PROVIDER

    def __init__(self, *, client: ApilioImageProvider) -> None:
        self.client = client

    def generate_view(self, request: CharacterImageRequest) -> CharacterImageResult:
        model = str(request.model or "").strip()
        if model not in FIRST_FRAME_MODELS:
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_MODEL_UNSUPPORTED",
                "character image model is not supported",
                retriable=False,
            )
        # 白名单校验后把 str 收敛回协议字面量类型，供 edit 的类型签名使用。
        typed_model = cast(FirstFrameModel, model)
        aspect_ratio, size_override = apilio_character_model_parameters(typed_model)
        try:
            generated = self.client.edit(
                model=typed_model,
                prompt=build_character_view_prompt(request),
                source_image=character_source_image(request.source_content),
                character_reference_images=[],
                output_count=1,
                aspect_ratio=aspect_ratio,
                size_override=size_override,
            )
        except RetryableImageProviderFailed as exc:
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_UNAVAILABLE",
                "character image provider request failed",
                retriable=True,
                http_status=provider_http_status(str(exc)),
            ) from exc
        except ImageProviderFailed as exc:
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_FAILED",
                "character image provider rejected the request",
                retriable=False,
                http_status=provider_http_status(str(exc)),
            ) from exc
        if not generated:
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_INVALID_RESPONSE",
                "character image provider returned an invalid response",
                retriable=False,
            )
        image = generated[0]
        content_type = image.content_type.split(";", 1)[0].strip().lower()
        if not image.content or content_type not in CHARACTER_ALLOWED_OUTPUT_TYPES:
            raise CharacterImageProviderFailed(
                "CHARACTER_PROVIDER_INVALID_RESPONSE",
                "character image provider returned an invalid response",
                retriable=False,
            )
        return CharacterImageResult(
            content=image.content,
            content_type=content_type,
            # 同步接口没有供应商任务号；用内容哈希合成一个稳定的可追溯编号。
            provider_task_id=f"apilio-character-{hashlib.sha256(image.content).hexdigest()[:24]}",
            cost_amount=0.0,
        )


def apilio_character_model_parameters(model: str) -> tuple[str | None, str | None]:
    """把版本上的模型名映射成 Apilio 编辑请求的尺寸参数。

    两个模型的尺寸入口不同（见 build_apilio_edit_multipart）：gpt-image-2 只认
    ``size``（且白名单里没有 2:3 档位，所以 aspect_ratio 必须留空由 size 兑现，
    否则会在拼装阶段被拒）；nano-banana-pro-2k 走 ``aspect_ratio``，size_override
    会被忽略。
    """
    if model == "nano-banana-pro-2k":
        return CHARACTER_VIEW_ASPECT_RATIO, None
    return None, CHARACTER_VIEW_PIXEL_SIZE


def character_source_image(content: bytes) -> ImageInput:
    """按魔数标注源图类型——CharacterImageRequest 只冻结了字节内容。

    源图在身份上传链路已校验为 JPG/PNG，这里再兜一层 WebP 并把不认识的内容
    明确拒绝，避免把坏字节送进供应商再回收一个含糊的失败。
    """
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        content_type, extension = "image/png", ".png"
    elif content.startswith(b"\xff\xd8"):
        content_type, extension = "image/jpeg", ".jpg"
    elif len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        content_type, extension = "image/webp", ".webp"
    else:
        raise CharacterImageProviderFailed(
            "CHARACTER_VERSION_SOURCE_INVALID",
            "character version source image content is invalid",
            retriable=False,
        )
    return ImageInput(
        content=content,
        content_type=content_type,
        filename=f"character-source{extension}",
    )


def build_character_view_prompt(request: CharacterImageRequest) -> str:
    """冻结 persona + 视角片段 → 单张出图 prompt。

    persona 快照里的空字段不产出空行：提示词短一些，模型的自由度反而更贴近
    「只换视角、不改人」的目标。
    """
    lines = [
        "You are a professional portrait photographer. Render one studio reference "
        "photograph of the authorized real person from the uploaded photo.",
        "",
        CHARACTER_VIEW_PROMPT_FRAGMENTS[request.view_type],
        "",
    ]
    persona_lines = character_persona_prompt_lines(request.persona_snapshot)
    if persona_lines:
        lines.extend(persona_lines)
        lines.append("")
    lines.append(CHARACTER_IDENTITY_INVARIANTS)
    return "\n".join(lines)


def character_persona_prompt_lines(persona: dict[str, object]) -> list[str]:
    lines: list[str] = []
    name = clean_prompt_text(persona.get("name"))
    occupation = clean_prompt_text(persona.get("occupation"))
    if name or occupation:
        joined = " — ".join(part for part in (name, occupation) if part)
        lines.append(f"Persona: {joined}.")
    for key, label in (
        ("scene_description", "Scene"),
        ("costume_description", "Costume"),
        ("default_background", "Background override"),
        ("positive_prompt", "Additional requirements"),
        ("negative_prompt", "Avoid"),
    ):
        value = clean_prompt_text(persona.get(key))
        if value:
            lines.append(f"{label}: {value}")
    constraints = persona.get("appearance_constraints_json")
    if isinstance(constraints, dict) and constraints:
        lines.append(f"Appearance constraints: {json.dumps(constraints, ensure_ascii=False)}")
    return lines


def load_apilio_character_provider(
    conn: BusinessConnection,
) -> ApilioCharacterImageProvider | None:
    """读取已保存的 Apilio 配置构造 Provider；未配置或不可解密时返回 None。

    调用方负责把 None 翻译成面向用户的中文错误——本函数不做 HTTP 语义。
    base_url 固定官方地址：库里的 legacy 配置不允许被改写指向别处（与首帧
    链路的 token 保护策略一致）。
    """
    try:
        config = SettingsRepository(conn).load_provider_config("apilio")
    except SettingsUnavailableError:
        return None
    raw_key = config.get("api_key") if isinstance(config, dict) else None
    api_key = clean_prompt_text(raw_key)
    if not api_key:
        return None
    return ApilioCharacterImageProvider(
        client=ApilioImageProvider(api_key=api_key, base_url=APILIO_DEFAULT_BASE_URL),
    )


def provider_http_status(message: str) -> int | None:
    match = re.search(r"HTTP (\d{3})", message)
    return int(match.group(1)) if match else None


def clean_prompt_text(value: object) -> str:
    return str(value).strip() if value is not None else ""
