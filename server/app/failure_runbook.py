"""失败码 runbook —— error_code → 修复建议 + 原因分类 + 处理人的单一来源。

对应 BILLING-OBS P2-2 / 方案 P1-1。

管理端的「生成记录」失败聚合与「任务诊断」尝试历史都会直接展示
``error_code``：客服/运维/客户看到 ``ANALYSIS_PROVIDER_UNREACHABLE`` 这类
内部编号，没有这份映射就不知道下一步做什么。本模块是唯一来源——新增失败码
时在这里登记（1）一句可执行的建议（``FAILURE_RUNBOOK``）、（2）原因分类与
谁来处理（``FAILURE_CLASSIFICATION``），两个视图自动生效；漏登记会被
``server/tests/test_failure_runbook.py`` 的覆盖面契约拦截。

方案 P1-1 的原因分类：客户素材 / 内容审核 / 系统繁忙 / 服务商故障 /
配置问题 / 系统缺陷；客户主动取消单列「客户取消」——把它硬塞进上述任何
一类都会误导处理方向（既不是素材问题，也不需要技术排查）。处理人只在
客服告知客户（SUPPORT）/ 运营重试（OPERATIONS）/ 技术处理（TECHNICAL）
三者中取值，管理端据此把失败分派到正确的角色。

范围：只覆盖会写进任务行 ``error_code`` 的失败码（拆解/生成/取帧/脚本/
口播/爆款/支付回调/设置自检）。请求入口的即时校验码（HTTP 422/404 的
``detail.code``）不在此列——调用方当场就拿到了 message，不属于事后诊断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 文案规则：一句「这是什么、下一步做什么」，写给客服/运维/客户三方中
# 最先看到它的人；不出现内部文件名、类名与密钥形态信息。
FAILURE_RUNBOOK: dict[str, str] = {
    "PROVIDER_POLL_TIMEOUT": (
        "生成超过自动核对时限，结果仍需人工确认。先核对任务是否已完成或扣费，"
        "再决定恢复查询或补偿，避免重复生成。"
    ),
    # ---- 视频拆解（analysis_tasks.error_code）----
    "ANALYSIS_WORKER_FAILED": (
        "拆解进程发生未分类错误。重试一次；仍失败携任务编号报障，运维按编号查 worker 日志堆栈。"
    ),
    "ANALYSIS_WORKER_INTERRUPTED": (
        "拆解执行中断（进程退出或租约过期被接管）。直接重试；"
        "频繁出现请检查 worker 容器资源与重启记录。"
    ),
    "ANALYSIS_PROVIDER_FAILED": (
        "拆解服务返回不可用内容（非网络问题）。稍后重试一次；持续失败核对接入商服务状态。"
    ),
    "ANALYSIS_PROVIDER_UNREACHABLE": (
        "无法连接视频拆解服务。检查服务器出网、代理与出口白名单，网络恢复后重试。"
    ),
    "ANALYSIS_PROVIDER_RATE_LIMITED": (
        "拆解服务限流（429）。降低并发或稍后重试；长期限流需扩容上游账号配额。"
    ),
    "ANALYSIS_PROVIDER_SETTINGS_REQUIRED": (
        "尚未配置可用的视频分析服务。到管理端「设置」填写分析服务配置并测试连接后重试。"
    ),
    "ANALYSIS_VIDEO_URL_UNAVAILABLE": (
        "视频地址无法被云端读取（多因存储为本地模式）。切换对象存储后重新上传视频再拆解。"
    ),
    "ANALYSIS_TASK_CANCELLED": (
        "拆解任务已取消（用户主动停止，预扣已解冻、未计费）。如需继续，重新发起拆解即可。"
    ),
    "APILIO_SETTINGS_REQUIRED": "APILIO 服务未配置。到管理端设置补全配置并测试连接。",
    "APILIO_SETTINGS_UNAVAILABLE": (
        "APILIO 配置不可用。检查密钥/地址完整性与网络，测试连接通过后重试。"
    ),
    # ---- 视频生成与首帧/图像（generation_tasks / image 任务）----
    "PROVIDER_TERMINAL": (
        "上游任务以失败态终止。查看错误说明与上游原话；重新生成或更换素材；连续失败检查账号额度。"
    ),
    "H3_PROVIDER_FAILED": (
        "生成任务失败或返回无效结果。重试一次；确认提示词/素材无违规；持续失败携任务编号报障。"
    ),
    "ARCHIVE_RETRY_EXHAUSTED": (
        "归档重试已耗尽（上游结果地址可能已过期）。检查对象存储配置与网络后重新生成。"
    ),
    "FIRST_FRAME_URL_SIGN_FAILED": (
        "首帧链接签名失败（未触达供应商、未计费）。检查对象存储密钥与服务器时钟后原地重试。"
    ),
    "H3_SETTINGS_UNAVAILABLE": (
        "H3 配置不可用。到管理端检查生成服务/APILIO 设置并测试连接后重试。"
    ),
    "PROVIDER_REQUEST_CONTRACT": (
        "请求契约校验未通过（未触达供应商）。刷新工作台数据后重试；反复出现属版本缺陷，请报障。"
    ),
    "VISUAL_VALIDATION_UNAVAILABLE": (
        "生成结果等待视觉校验。产物已保留，请稍后在记录中复核或人工确认，无需重试。"
    ),
    "LEASE_EXPIRED_NEEDS_ATTENTION": (
        "任务租约过期，需要人工确认。先核对该任务是否已有结果，再在对账/重试入口处理，避免重复提交。"
    ),
    "RECONCILE_ACTOR_UNAVAILABLE": "对账所需操作者不可用。用有权限的账号在对账入口重新发起操作。",
    "RECONCILE_OPERATION_FAILED": "对账操作失败。查看错误说明；确认上游状态后重新发起对账。",
    "PROMPT_WORKER_INTERRUPTED": "提示词优化执行中断（原文已保留）。直接在原处重试优化。",
    "FIRST_FRAME_CHECKPOINT_RESUME": (
        "首帧已保存，正在从检查点恢复后续处理。无需操作，等待自动完成。"
    ),
    "IMAGE_TASK_LEASE_EXPIRED": (
        "图像任务执行中断且已停止自动重试，需要管理员核对。先确认上游任务结果，再决定恢复或重发。"
    ),
    "IMAGE_TASK_PROVIDER_BUSY": (
        "生成服务限流繁忙。任务已自动错峰重试；终态失败时稍后重新生成即可，无需联系管理员。"
    ),
    "IMAGE_TASK_RECONCILE_RESUMED": ("管理员已核对上游任务并恢复处理。无需操作，等待继续推进。"),
    # 方案 P0-10：以下码经 ``code = "..."`` 变量写入任务行，旧覆盖测试扫不到。
    "IMAGE_TASK_FAILED": (
        "图像生成失败。查看接口调用记录里的服务商原话；可重试一次，持续失败携任务编号报障。"
    ),
    "IMAGE_TASK_PROVIDER_FAILED": (
        "图像服务返回了不可用的结果（张数不符、状态不可读等），重试同一任务结果相同。"
        "请重新生成；持续出现时对照调用记录里的原始响应排查。"
    ),
    "IMAGE_TASK_STORAGE_UNAVAILABLE": (
        "图片已生成但保存到素材库失败。检查对象存储配置与网络后重新生成。"
    ),
    "IMAGE_TASK_SUBMISSION_UNCERTAIN": (
        "图像任务提交结果未确认，已停止自动重试。先在生成记录里「重新核对」，确认服务商是否受理，"
        "避免重复扣费。"
    ),
    # ---- 人物视图（character_generation_tasks.error_code）----
    "CHARACTER_PROVIDER_TIMEOUT": "人物图片服务超时。系统会自动重试；持续超时检查服务状态与并发。",
    "CHARACTER_PROVIDER_RATE_LIMITED": (
        "人物图片服务限流（429）。稍后重试或降低并发；长期限流需要扩容服务账号额度。"
    ),
    "CHARACTER_PROVIDER_UNAVAILABLE": (
        "人物图片服务暂时不可用（5xx）。稍后重试；持续失败查看调用记录里的原始响应并联系服务商。"
    ),
    "CHARACTER_PROVIDER_INVALID_RESPONSE": (
        "人物图片服务返回了无法使用的结果。重新生成；持续出现时对照调用记录里的原始响应排查。"
    ),
    "CHARACTER_PROVIDER_MISMATCH": (
        "任务记录的服务与当前配置不一致。到技术配置确认人物图片服务，再重新生成。"
    ),
    "CHARACTER_PROVIDER_NOT_CONFIGURED": (
        "尚未配置人物图片服务。到技术配置填写并测试连接后重新生成。"
    ),
    "CHARACTER_STORAGE_UNAVAILABLE": "人物图片保存失败。检查对象存储配置与网络后重新生成。",
    "CHARACTER_LEASE_EXPIRED": (
        "人物图片任务执行中断且未自动恢复。重新生成；频繁出现检查后台任务进程是否稳定。"
    ),
    "CHARACTER_LEASE_LOST": (
        "人物图片任务被其他进程接管后结束。重新生成即可；频繁出现检查后台任务进程是否重复启动。"
    ),
    "CHARACTER_VERSION_NOT_GENERATABLE": (
        "该人物版本当前不能生成视图（已归档或资料不全）。换用可用版本。"
    ),
    "CHARACTER_VERSION_SOURCE_CHANGED": "人物原图在生成期间被更换。按新的原图重新生成视图。",
    "CHARACTER_VERSION_SOURCE_MISSING": "人物原图缺失。重新上传人物原图后再生成视图。",
    "IDENTITY_NOT_ACTIVE": (
        "人物身份未生效（授权未完成或已停用），不能生成视图。请客户完成人物授权后重新生成。"
    ),
    "FAKE_CHARACTER_PROVIDER_FORBIDDEN": (
        "正式环境不允许使用模拟人物图片服务。到技术配置切换为正式服务后重新生成。"
    ),
    # ---- 源画面取帧（source_frame_tasks.error_code）----
    "SOURCE_FRAME_TASK_FAILED": "取帧任务失败。重试；检查源视频可读性与对象存储配置。",
    "SOURCE_FRAME_TASK_CANCELLED": "取帧任务已停止。如需继续，请重新开始取帧任务。",
    "SOURCE_FRAME_TASK_RECOVERY_REQUIRED": (
        "取帧任务执行中断需恢复。重新开始取帧；频繁出现检查 worker 稳定性。"
    ),
    "SOURCE_FRAME_STORAGE_UNAVAILABLE": "对象存储不可用（源帧）。检查存储配置与网络后重试。",
    # ---- 口播稿生成与改写（script_from_audio / script_rewrite 任务）----
    "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN": (
        "口播稿生成提交结果待核对。等系统对账后再决定重试，避免重复提交产生重复计费。"
    ),
    "SCRIPT_FROM_AUDIO_PIPELINE_FAILED": (
        "口播稿生成流程失败。重试；检查音频可读性；持续失败携任务编号报障。"
    ),
    "SCRIPT_FROM_AUDIO_PROVIDER_FAILED": (
        "口播稿生成上游失败。稍后重试；结合错误说明与上游原话定位。"
    ),
    "SCRIPT_REWRITE_SUBMISSION_UNCERTAIN": "改写提交结果待核对。等待对账结论后再重试。",
    "SCRIPT_REWRITE_TASK_FAILED": "改写任务失败。重试；检查文本内容与服务配置。",
    # 方案 P0-9：改写服务的 detail.code 族经 fail_script_rewrite_task 的
    # 变量路径（code = detail["code"]）写入任务行，此前没有 runbook 覆盖。
    "DEEPSEEK_NETWORK_FAILED": (
        "改写请求未能确认送达结果。先核对任务状态确认服务商是否已受理，再决定是否重试。"
    ),
    "DEEPSEEK_REQUEST_FAILED": (
        "改写服务返回错误响应。稍后重试一次；仍失败请检查文本服务密钥与配置。"
    ),
    "DEEPSEEK_RESPONSE_INVALID": (
        "改写服务返回内容无法解析。重试一次；持续出现时对照调用记录里的服务商原话排查。"
    ),
    "DEEPSEEK_RESPONSE_TRUNCATED": "改写结果超过输出上限被截断。缩短原文后重试，或分段改写。",
    "DEEPSEEK_RESPONSE_EMPTY": "改写服务返回了空内容。重试一次；持续失败检查原文长度与服务配置。",
    # ---- 口播视频（管理端按任务状态映射的展示码）----
    "ORAL_TASK_FAILED": "口播视频生成失败。查看错误说明；重试或更换素材；持续失败检查服务配置。",
    "ORAL_SUBMISSION_UNCERTAIN": "口播提交结果待核对。等待系统对账，避免重复提交。",
    "ORAL_ARCHIVE_FAILED": "口播成片归档失败。检查对象存储状态后重试归档。",
    # ---- 爆款采集（viral 任务）----
    "VIRAL_IMPORT_FAILED": "爆款视频导入失败。确认链接可访问后重试；持续失败检查采集与存储配置。",
    "VIRAL_MEDIA_PREPARATION_FAILED": ("爆款媒体准备失败。重试；检查源媒体与对象存储配置。"),
    "VIRAL_REFRESH_FAILED": "爆款数据刷新失败。稍后重试；持续失败检查采集服务配置。",
    # ---- 支付回调（ZPAY）----
    "ZPAY_INVALID_SIGN_TYPE": "回调签名类型不受支持。核对 ZPAY 后台回调的签名算法与当前版本一致。",
    "ZPAY_SIGNATURE_MISMATCH": (
        "回调签名不匹配。核对商户密钥与回调地址；不要手工入账，先与渠道核对。"
    ),
    "ZPAY_PID_MISMATCH": "回调商户号不匹配。核对系统内配置的 PID 与渠道后台是否一致。",
    "ZPAY_TRADE_NOT_SUCCESS": (
        "渠道侧交易未成功，订单未入账属正常。若客户反馈已付款，先与渠道核对该笔交易。"
    ),
    "ZPAY_MISSING_FIELDS": "回调缺少必需字段。核对渠道回调格式与接口版本。",
    "ZPAY_INVALID_AMOUNT": ("回调金额无效。核对订单金额与渠道侧金额；异常回调请联系渠道排查。"),
    # ---- 设置自检 ----
    "DIAGNOSTIC_INTERNAL_ERROR": (
        "连接测试发生内部错误。重试；仍失败请下载诊断日志并检查服务端日志。"
    ),
}


# ---------------------------------------------------------------------------
# 失败分类（P2-1「生成记录加缩略图与失败分类」）
# ---------------------------------------------------------------------------
# 原因分类 × 处理人：管理端在「谁该处理这条失败」上比建议文本更需要一个可筛选的
# 口径——客服照着念、运营照着重试、技术照着查，三者的动作完全不同。
#
# 与 FAILURE_RUNBOOK 同键：键集必须完全一致（少一个红、多一个也红），由
# ``test_failure_runbook.py`` 的契约钉住，所以两处不会漂移；一份是「怎么说」，
# 一份是「谁来看、看什么」。
#
# 分类口径：
#   CUSTOMER_ASSET  问题出在客户提供的素材或客户侧动作（原图缺失、链接不可读、主动取消）
#   CONTENT_REVIEW  内容审核（码表里没有静态映射：审核结论只出现在服务商原话里，
#                   由 failure_explanation 按原话关键词从「上游终止」族升级得到）
#   PROVIDER_BUSY   上游限流/繁忙类可重试状态（429、超时）
#   PROVIDER_FAULT  上游故障或返回不可用结果（5xx、无效响应、提交结果待核对）
#   CONFIG          配置缺失/不一致（服务未配置、密钥、存储模式、渠道参数）
#   DEFECT          系统缺陷或自身基础设施问题（worker 中断、存储不可用、契约校验）
#   NOT_A_FAILURE   流程状态而非失败（主动取消、检查点自愈、对账已恢复）——不需要按故障处理
FailureCategory = Literal[
    "CUSTOMER_ASSET",
    "CONTENT_REVIEW",
    "PROVIDER_BUSY",
    "PROVIDER_FAULT",
    "CONFIG",
    "DEFECT",
    "NOT_A_FAILURE",
    "UNCLASSIFIED",
]
# 处理人：客服告知客户 / 运营重试 / 技术处理。与原因的对应不是一对一——
# 例如「配置问题」通常要技术去改，而「客户素材」由客服引导客户。
FailureOwner = Literal["SUPPORT", "OPS", "ENGINEERING"]

FAILURE_CLASSIFICATION: dict[str, tuple[FailureCategory, FailureOwner]] = {
    "PROVIDER_POLL_TIMEOUT": ("PROVIDER_FAULT", "OPS"),
    "ANALYSIS_WORKER_FAILED": ("DEFECT", "ENGINEERING"),
    "ANALYSIS_WORKER_INTERRUPTED": ("DEFECT", "ENGINEERING"),
    "ANALYSIS_PROVIDER_FAILED": ("PROVIDER_FAULT", "OPS"),
    "ANALYSIS_PROVIDER_UNREACHABLE": ("PROVIDER_FAULT", "OPS"),
    "ANALYSIS_PROVIDER_RATE_LIMITED": ("PROVIDER_BUSY", "OPS"),
    "ANALYSIS_PROVIDER_SETTINGS_REQUIRED": ("CONFIG", "ENGINEERING"),
    "ANALYSIS_VIDEO_URL_UNAVAILABLE": ("CONFIG", "ENGINEERING"),
    "ANALYSIS_TASK_CANCELLED": ("NOT_A_FAILURE", "SUPPORT"),
    "APILIO_SETTINGS_REQUIRED": ("CONFIG", "ENGINEERING"),
    "APILIO_SETTINGS_UNAVAILABLE": ("CONFIG", "ENGINEERING"),
    "PROVIDER_TERMINAL": ("PROVIDER_FAULT", "OPS"),
    "H3_PROVIDER_FAILED": ("PROVIDER_FAULT", "OPS"),
    "ARCHIVE_RETRY_EXHAUSTED": ("DEFECT", "ENGINEERING"),
    "FIRST_FRAME_URL_SIGN_FAILED": ("CONFIG", "ENGINEERING"),
    "H3_SETTINGS_UNAVAILABLE": ("CONFIG", "ENGINEERING"),
    "PROVIDER_REQUEST_CONTRACT": ("DEFECT", "ENGINEERING"),
    "VISUAL_VALIDATION_UNAVAILABLE": ("DEFECT", "OPS"),
    "LEASE_EXPIRED_NEEDS_ATTENTION": ("DEFECT", "OPS"),
    "RECONCILE_ACTOR_UNAVAILABLE": ("CONFIG", "OPS"),
    "RECONCILE_OPERATION_FAILED": ("DEFECT", "OPS"),
    "PROMPT_WORKER_INTERRUPTED": ("DEFECT", "OPS"),
    "FIRST_FRAME_CHECKPOINT_RESUME": ("NOT_A_FAILURE", "OPS"),
    "IMAGE_TASK_LEASE_EXPIRED": ("DEFECT", "OPS"),
    "IMAGE_TASK_PROVIDER_BUSY": ("PROVIDER_BUSY", "OPS"),
    "IMAGE_TASK_RECONCILE_RESUMED": ("NOT_A_FAILURE", "OPS"),
    "IMAGE_TASK_FAILED": ("PROVIDER_FAULT", "OPS"),
    "IMAGE_TASK_PROVIDER_FAILED": ("PROVIDER_FAULT", "OPS"),
    "IMAGE_TASK_STORAGE_UNAVAILABLE": ("DEFECT", "ENGINEERING"),
    "IMAGE_TASK_SUBMISSION_UNCERTAIN": ("PROVIDER_FAULT", "OPS"),
    "CHARACTER_PROVIDER_TIMEOUT": ("PROVIDER_BUSY", "OPS"),
    "CHARACTER_PROVIDER_RATE_LIMITED": ("PROVIDER_BUSY", "OPS"),
    "CHARACTER_PROVIDER_UNAVAILABLE": ("PROVIDER_FAULT", "OPS"),
    "CHARACTER_PROVIDER_INVALID_RESPONSE": ("PROVIDER_FAULT", "OPS"),
    "CHARACTER_PROVIDER_MISMATCH": ("CONFIG", "ENGINEERING"),
    "CHARACTER_PROVIDER_NOT_CONFIGURED": ("CONFIG", "ENGINEERING"),
    "CHARACTER_STORAGE_UNAVAILABLE": ("DEFECT", "ENGINEERING"),
    "CHARACTER_LEASE_EXPIRED": ("DEFECT", "OPS"),
    "CHARACTER_LEASE_LOST": ("DEFECT", "OPS"),
    "CHARACTER_VERSION_NOT_GENERATABLE": ("CUSTOMER_ASSET", "SUPPORT"),
    "CHARACTER_VERSION_SOURCE_CHANGED": ("CUSTOMER_ASSET", "SUPPORT"),
    "CHARACTER_VERSION_SOURCE_MISSING": ("CUSTOMER_ASSET", "SUPPORT"),
    "IDENTITY_NOT_ACTIVE": ("CUSTOMER_ASSET", "SUPPORT"),
    "FAKE_CHARACTER_PROVIDER_FORBIDDEN": ("CONFIG", "ENGINEERING"),
    "SOURCE_FRAME_TASK_FAILED": ("CUSTOMER_ASSET", "OPS"),
    "SOURCE_FRAME_TASK_CANCELLED": ("NOT_A_FAILURE", "OPS"),
    "SOURCE_FRAME_TASK_RECOVERY_REQUIRED": ("DEFECT", "OPS"),
    "SOURCE_FRAME_STORAGE_UNAVAILABLE": ("DEFECT", "ENGINEERING"),
    "SCRIPT_FROM_AUDIO_SUBMISSION_UNCERTAIN": ("PROVIDER_FAULT", "OPS"),
    "SCRIPT_FROM_AUDIO_PIPELINE_FAILED": ("CUSTOMER_ASSET", "OPS"),
    "SCRIPT_FROM_AUDIO_PROVIDER_FAILED": ("PROVIDER_FAULT", "OPS"),
    "SCRIPT_REWRITE_SUBMISSION_UNCERTAIN": ("PROVIDER_FAULT", "OPS"),
    "SCRIPT_REWRITE_TASK_FAILED": ("PROVIDER_FAULT", "OPS"),
    "DEEPSEEK_NETWORK_FAILED": ("PROVIDER_FAULT", "OPS"),
    "DEEPSEEK_REQUEST_FAILED": ("PROVIDER_FAULT", "OPS"),
    "DEEPSEEK_RESPONSE_INVALID": ("PROVIDER_FAULT", "OPS"),
    "DEEPSEEK_RESPONSE_TRUNCATED": ("CUSTOMER_ASSET", "SUPPORT"),
    "DEEPSEEK_RESPONSE_EMPTY": ("PROVIDER_FAULT", "OPS"),
    "ORAL_TASK_FAILED": ("PROVIDER_FAULT", "OPS"),
    "ORAL_SUBMISSION_UNCERTAIN": ("PROVIDER_FAULT", "OPS"),
    "ORAL_ARCHIVE_FAILED": ("DEFECT", "OPS"),
    "VIRAL_IMPORT_FAILED": ("CUSTOMER_ASSET", "OPS"),
    "VIRAL_MEDIA_PREPARATION_FAILED": ("CUSTOMER_ASSET", "OPS"),
    "VIRAL_REFRESH_FAILED": ("PROVIDER_FAULT", "OPS"),
    "ZPAY_INVALID_SIGN_TYPE": ("CONFIG", "ENGINEERING"),
    "ZPAY_SIGNATURE_MISMATCH": ("CONFIG", "ENGINEERING"),
    "ZPAY_PID_MISMATCH": ("CONFIG", "ENGINEERING"),
    "ZPAY_TRADE_NOT_SUCCESS": ("NOT_A_FAILURE", "SUPPORT"),
    "ZPAY_MISSING_FIELDS": ("CONFIG", "ENGINEERING"),
    "ZPAY_INVALID_AMOUNT": ("CONFIG", "ENGINEERING"),
    "DIAGNOSTIC_INTERNAL_ERROR": ("DEFECT", "ENGINEERING"),
}


def failure_classification(
    error_code: str | None,
) -> tuple[FailureCategory, FailureOwner] | None:
    """查回 (原因分类, 处理人)；归一与 fail-open 语义同 :func:`failure_advice`。"""
    if not error_code:
        return None
    return FAILURE_CLASSIFICATION.get(error_code.strip().upper())


@dataclass(frozen=True)
class FailureExplanation:
    """一条失败码的完整解释：原因分类 + 处理人 + 修复建议。

    分类与处理人是稳定代码，中文标签由管理端词典翻译，后端不另存一份。
    """

    category: FailureCategory
    owner: FailureOwner
    advice: str


# 内容审核升级：上游审核拒绝与「上游以失败态终止」共用同一批错误码，
# 静态映射看不出区别，只能凭服务商原话区分。只对「上游终止/结果不可用」
# 族启用扫描——限流、网络、配置族的原话里出现这些词是误报温床。
# 关键词故意收紧：``policy`` 单独会把隐私政策类文本拉进来；
# ``blocked``/``refused`` 单独会把防火墙与连接拒绝拉进来，故都要求组合词。
_CONTENT_REVIEW_SCAN_CODES = frozenset(
    {
        "PROVIDER_TERMINAL",
        "H3_PROVIDER_FAILED",
        "IMAGE_TASK_FAILED",
        "IMAGE_TASK_PROVIDER_FAILED",
        "CHARACTER_PROVIDER_INVALID_RESPONSE",
        "ORAL_TASK_FAILED",
    }
)

_CONTENT_REVIEW_HINTS = (
    "violation",
    "policy_violation",
    "content policy",
    "content_policy",
    "moderation",
    "prohibited",
    "inappropriate",
    "nsfw",
    "sensitive content",
    "sensitive_content",
    "content filter",
    "content_filter",
    "审核",
    "违规",
    "不合规",
    "敏感词",
)


def _content_review_message(provider_message: str | None) -> bool:
    if not provider_message:
        return False
    lowered = provider_message.casefold()
    return any(hint in lowered for hint in _CONTENT_REVIEW_HINTS)


def failure_explanation(
    error_code: str | None, *, provider_message: str | None = None
) -> FailureExplanation | None:
    """查回完整解释；大小写/空白归一，未知码返回 None（fail-open）。

    两表键集合由测试锁定一致，因此查得到建议就查得到分类；这里的防御性
    None 只针对未登记码，与 ``failure_advice`` 的行为保持一致。

    ``provider_message``（服务商原话，已脱敏）只用于内容审核升级：上游
    审核拒绝没有专属错误码，原话命中审核关键词时把分类升为 CONTENT_REVIEW，
    处理人相应改为 SUPPORT（告知客户修改素材后重试）。
    """
    if not error_code:
        return None
    code = error_code.strip().upper()
    advice = FAILURE_RUNBOOK.get(code)
    classification = FAILURE_CLASSIFICATION.get(code)
    if advice is None or classification is None:
        return None
    category, owner = classification
    if code in _CONTENT_REVIEW_SCAN_CODES and _content_review_message(provider_message):
        category, owner = "CONTENT_REVIEW", "SUPPORT"
    return FailureExplanation(category=category, owner=owner, advice=advice)


def failed_record_explanation(
    error_code: str | None, *, provider_message: str | None = None
) -> FailureExplanation:
    """Only call for a failed/uncertain record; unknown codes stay visibly unclassified."""
    return failure_explanation(error_code, provider_message=provider_message) or FailureExplanation(
        category="UNCLASSIFIED",
        owner="ENGINEERING",
        advice="原因尚未归类。请交技术核对任务和调用记录，并确认本轮积分状态；核对前不要重复提交或重复补偿。",
    )


def failure_advice(error_code: str | None) -> str | None:
    """查回修复建议；大小写/空白归一，未知码返回 None（fail-open）。"""
    if not error_code:
        return None
    return FAILURE_RUNBOOK.get(error_code.strip().upper())
