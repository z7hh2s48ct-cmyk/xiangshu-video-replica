"""Business fee subjects. An absent retail tariff never authorizes a charge."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from app.db_portable import BusinessConnection

Unit = Literal["second", "image", "call"]


@dataclass(frozen=True)
class Service:
    name: str
    unit: Unit
    provider: str
    module: str
    customer_charge_allowed: bool = True
    # Pure platform plumbing: the supplier bills nothing per call, so only zero cost passes.
    zero_cost_platform: bool = False


SERVICES: dict[str, Service] = {
    "video_768p": Service("视频生成 · 768P", "second", "metaso", "video"),
    "video_2k": Service("视频生成 · 2K", "second", "metaso", "video"),
    "analysis": Service("视频分析", "call", "apilio", "replica"),
    "first_frame": Service("首帧图片", "image", "apilio", "replacement"),
    "character": Service("人物形象及任务图片", "image", "apilio", "people"),
    "rewrite": Service("文案改写", "call", "deepseek", "copy"),
    "oral": Service("数字人口播", "second", "hifly", "oral"),
    "asr": Service("语音转写", "second", "dashscope", "copy"),
    "viral_data": Service("爆款视频数据请求", "call", "tikhub", "viral"),
    "viral_search": Service("爆款视频搜索", "call", "tikhub", "viral"),
    "viral_search_refresh": Service("爆款视频刷新", "call", "tikhub", "viral"),
    "viral_statistics": Service("爆款视频互动统计", "call", "tikhub", "viral"),
    "link_resolution": Service("链接解析", "call", "douyidou", "workbench"),
    "prompt_optimize": Service("提示词 AI 优化", "call", "apilio", "workbench"),
    "avatar_clone": Service("口播分身创建", "call", "hifly", "people"),
    "voice_clone": Service("声音克隆", "call", "hifly", "people"),
    "quality_inspection": Service("图片及视频质量检查", "call", "apilio", "internal", False),
    "analysis_repair": Service("分析结果修复", "call", "apilio", "internal", False),
    "analysis_repair_deepseek": Service(
        "分析结果修复 · DeepSeek", "call", "deepseek", "internal", False
    ),
    "cos": Service("云存储", "call", "cos", "infrastructure", False, True),
    "zpay": Service("支付通道", "call", "zpay", "infrastructure", False, True),
}

# Existing cost hooks refer to these provider subjects; they share the same tariff.
COST_SUBJECTS = {
    "video_generation_768p": "video_768p",
    "video_generation_2k": "video_2k",
    "video_analysis_768p": "analysis",
    "video_analysis_2k": "analysis",
    "first_frame_image": "first_frame",
    "character_sheet_image": "character",
}

# 客户可配置的消耗侧折扣接口目录（管理员在套餐里勾选；空范围 = 全部接口）。
INTERFACE_KEYS: tuple[str, ...] = (
    "video_generation",
    "video_analysis",
    "first_frame",
    "character",
    "script_rewrite",
    "oral",
    "asr",
    "viral_extract",
    "link_resolution",
    "prompt_optimize",
)

# 平台内部科目（客户不可充值消费）统一归入内部接口，不出现在套餐勾选目录。
PLATFORM_INTERFACE_KEY = "platform_internal"

# 计费科目 → 折扣接口键（覆盖 SERVICES 全集；可计费科目的键必须在 INTERFACE_KEYS 中）。
SERVICE_INTERFACE: dict[str, str] = {
    "video_768p": "video_generation",
    "video_2k": "video_generation",
    "analysis": "video_analysis",
    "first_frame": "first_frame",
    "character": "character",
    "rewrite": "script_rewrite",
    "oral": "oral",
    "asr": "asr",
    "viral_data": "viral_extract",
    "viral_search": "viral_extract",
    "viral_search_refresh": "viral_extract",  # Refresh uses same discount interface
    "viral_statistics": "viral_extract",
    "link_resolution": "link_resolution",
    "prompt_optimize": "prompt_optimize",
    "avatar_clone": "oral",
    "voice_clone": "oral",
    "quality_inspection": PLATFORM_INTERFACE_KEY,
    "analysis_repair": PLATFORM_INTERFACE_KEY,
    "analysis_repair_deepseek": PLATFORM_INTERFACE_KEY,
    "cos": PLATFORM_INTERFACE_KEY,
    "zpay": PLATFORM_INTERFACE_KEY,
}


def interface_for_service(service: str) -> str:
    """计费科目对应的折扣接口键；未知科目显式拒绝（沿用 read_tariff 口径）."""
    if service not in SERVICES:
        raise ValueError("未知计费科目")
    return SERVICE_INTERFACE[service]


class Tariff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    unit_credits: Decimal | None = Field(default=None, ge=0, le=1_000_000, decimal_places=6)
    unit_cost_fen: Decimal | None = Field(default=None, ge=0, le=100_000_000, decimal_places=6)
    unit_rounding: Literal["ceil", "exact"] = "ceil"
    version: int = Field(default=0, ge=0)


def amount(value: Decimal | str | int | float) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("计费用量格式不正确") from exc
    if not result.is_finite() or result < 0 or result > Decimal("2147483647"):
        raise ValueError("计费用量必须是有限非负数且不超出允许范围")
    return result.quantize(Decimal("0.000001"))


def calculate_credits(
    tariff: Tariff | None,
    units: Decimal | str | int | float,
    *,
    discount_basis_points: int = 10_000,
    rounding: str = "ceil",
) -> int:
    usage = amount(units)
    if tariff is None or not tariff.enabled or not tariff.unit_credits or not usage:
        return 0
    if not 1 <= discount_basis_points <= 10_000:
        raise ValueError("折扣超出允许范围")
    if tariff.unit_rounding == "ceil":
        usage = usage.to_integral_value(rounding=ROUND_CEILING)
    value = usage * tariff.unit_credits * Decimal(discount_basis_points) / 10_000
    credits = max(
        1,
        int(
            value.to_integral_value(rounding=ROUND_FLOOR if rounding == "floor" else ROUND_CEILING)
        ),
    )
    if credits > 2_147_483_647:
        raise ValueError("积分金额超出允许范围")
    return credits


def merge_customer_discount(platform_basis_points: int, rate: Decimal | None) -> int:
    """平台口径与客户折扣「取更优」：折算 basis points 后取较小者（bp 越小折扣越强）.

    客户折扣是售价侧概念（R-A），只作用于 ``discount_basis_points``；平台侧已有更
    强折扣时不会被客户的弱折扣顶掉。
    """
    if rate is None:
        return platform_basis_points
    from app.discount_service import validate_discount_rate

    checked = validate_discount_rate(rate)
    customer_basis_points = int((checked * 10_000).to_integral_value(rounding=ROUND_FLOOR))
    return max(1, min(platform_basis_points, customer_basis_points))


def read_tariff(conn: BusinessConnection, service: str) -> Tariff | None:
    if service not in SERVICES:
        raise ValueError("未知计费科目")
    row = conn.execute(
        "SELECT enabled, unit_credits, unit_cost_fen, unit_rounding, version "
        "FROM billing_tariffs WHERE service = %s FOR SHARE",
        (service,),
    ).fetchone()
    if row is None:
        return None
    return Tariff(
        **{
            key: row[key]
            for key in ("enabled", "unit_credits", "unit_cost_fen", "unit_rounding", "version")
        }
    )


def retail_snapshot(
    conn: BusinessConnection,
    service: str,
    units: Decimal | str | int | float,
    *,
    user_id: str | None = None,
) -> dict[str, object]:
    """零售计价快照；``user_id`` 给出时合并该用户的消耗侧折扣（取更优）.

    折扣在快照里冻结为 ``discount_basis_points`` + ``discount_rate``（生效率）
    + ``discount_source``（客户权益来源 token；平台口径更强时为 None）；
    结算侧只读快照重算，事后改折扣配置不重算历史（R-D）。
    """
    from app.customer_pricing import read_pricing
    from app.discount_service import get_best_discount

    _, config = read_pricing(conn)
    tariff = read_tariff(conn, service)
    platform_basis_points = config.discount_basis_points if config else 10000
    rounding = config.consumption_rounding if config else "ceil"
    permitted = SERVICES[service].customer_charge_allowed
    record_rate: Decimal | None = None
    record_basis_points: int | None = None
    record_source: str | None = None
    if user_id is not None and permitted and conn.is_postgres:
        # SES-01：valid_from 由 DB 侧 clock_timestamp() 写入，生效判断必须同源
        # 取 DB 时钟；用应用墙钟会在进程时钟落后时漏掉刚授予的套餐权益，
        # 让客户按原价被预扣（上线评审 H-1）。SELECT 无 FROM 恒返回一行，
        # None 分支仅为类型收窄（不可达）。
        now_row = conn.raw.execute("SELECT clock_timestamp()").fetchone()
        at_time = now_row[0] if now_row is not None else datetime.now(UTC)
        record = get_best_discount(
            conn.raw,
            user_id=user_id,
            interface_key=interface_for_service(service),
            at_time=at_time,
        )
        if record is not None:
            record_rate = record.discount_rate
            record_basis_points = max(
                1, int((record_rate * 10_000).to_integral_value(rounding=ROUND_FLOOR))
            )
            record_source = "recharge_package" if record.source_recharge_order_id else "manual"
    discount = merge_customer_discount(platform_basis_points, record_rate)
    # 生效折扣归因：平台口径严格更强时不得虚报客户权益来源；``discount_rate``
    # 冻结生效值（= bp/10000），账目与展示须与实收一致。客户权益与平台同强
    # （record 折算 bp == 合并值）时仍归客户：权益真实生效。无任何折扣 → None。
    if record_basis_points is not None and record_basis_points <= discount:
        discount_rate = record_rate
        discount_source = record_source
    else:
        discount_rate = (
            (Decimal(discount) / Decimal(10_000)).quantize(Decimal("0.0001"))
            if discount < 10_000
            else None
        )
        discount_source = None
    credits = calculate_credits(
        tariff if permitted else None, units, discount_basis_points=discount, rounding=rounding
    )
    return {
        "service": service,
        "version": tariff.version if tariff else 0,
        "unit": SERVICES[service].unit,
        "units": str(amount(units)),
        "enabled": bool(permitted and tariff and tariff.enabled and tariff.unit_credits),
        "unit_credits": str(tariff.unit_credits or 0) if tariff else "0",
        "unit_rounding": tariff.unit_rounding if tariff else "ceil",
        "credits": credits,
        "discount_basis_points": discount,
        "discount_rate": str(discount_rate) if discount_rate is not None else None,
        "discount_source": discount_source,
        "consumption_rounding": rounding,
        "points_per_yuan": config.points_per_yuan if config else None,
        "free_reason": None
        if credits
        else (
            "platform_service"
            if not permitted
            else "unconfigured"
            if tariff is None
            else "disabled"
            if not tariff.enabled
            else "zero_price"
        ),
    }


def credits_from_snapshot(snapshot: dict[str, object], units: Decimal | str | int | float) -> int:
    return calculate_credits(
        Tariff(
            enabled=bool(snapshot["enabled"]),
            unit_credits=Decimal(str(snapshot["unit_credits"])),
            unit_rounding=cast(Literal["ceil", "exact"], str(snapshot["unit_rounding"])),
        ),
        units,
        discount_basis_points=int(str(snapshot["discount_basis_points"])),
        rounding=str(snapshot["consumption_rounding"]),
    )


def snapshot_discount_rate(snapshot: dict[str, object]) -> Decimal | None:
    """快照里冻结的折扣率（账目落库侧解析）；缺失/非法一律 None."""
    raw = snapshot.get("discount_rate")
    if raw is None:
        return None
    try:
        rate = Decimal(str(raw))
    except InvalidOperation:
        return None
    if not rate.is_finite() or rate <= 0 or rate > 1:
        return None
    return rate.quantize(Decimal("0.0001"))


def oral_budget_units(
    conn: BusinessConnection, *, script_text: str | None, audio_asset_id: str | None
) -> Decimal:
    if not audio_asset_id:
        return Decimal(max(1, (len(script_text or "") + 1) // 2))
    import json

    asset = conn.execute(
        "SELECT metadata_json FROM assets WHERE id=%s", (audio_asset_id,)
    ).fetchone()
    metadata = json.loads(str(asset[0] or "{}")) if asset else {}
    return amount(
        metadata.get("audio_duration_seconds")
        or metadata.get("duration_seconds")
        or metadata.get("duration_sec")
        or 0
    )
