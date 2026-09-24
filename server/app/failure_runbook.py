"""失败码 runbook —— error_code → 修复建议的单一来源（BILLING-OBS P2-2）。

管理端的「生成记录」失败聚合与「任务诊断」尝试历史都会直接展示
``error_code``：客服/运维/客户看到 ``ANALYSIS_PROVIDER_UNREACHABLE`` 这类
内部编号，没有这份映射就不知道下一步做什么。本模块是唯一来源——新增失败码
时在这里登记一句可执行的建议，两个视图自动生效；漏登记会被
``server/tests/test_failure_runbook.py`` 的覆盖面契约拦截。

范围：只覆盖会写进任务行 ``error_code`` 的失败码（拆解/生成/取帧/脚本/
口播/爆款/支付回调/设置自检）。请求入口的即时校验码（HTTP 422/404 的
``detail.code``）不在此列——调用方当场就拿到了 message，不属于事后诊断。
"""

from __future__ import annotations

# 文案规则：一句「这是什么、下一步做什么」，写给客服/运维/客户三方中
# 最先看到它的人；不出现内部文件名、类名与密钥形态信息。
FAILURE_RUNBOOK: dict[str, str] = {
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
    "IMAGE_TASK_RECONCILE_RESUMED": ("管理员已核对上游任务并恢复处理。无需操作，等待继续推进。"),
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


def failure_advice(error_code: str | None) -> str | None:
    """查回修复建议；大小写/空白归一，未知码返回 None（fail-open）。"""
    if not error_code:
        return None
    return FAILURE_RUNBOOK.get(error_code.strip().upper())
