"""Classify typed failures without exposing supplier payloads or guessing from text."""

from urllib.error import HTTPError, URLError

from app.viral_tikhub import ViralSourceHttpStatusError, ViralSourceUnavailable

FAILURES = {
    "CONFIGURATION": ("服务未配置或配置无效", "请管理员核对数据服务配置后重新执行。"),
    "AUTHENTICATION": ("服务认证失败", "请管理员检查服务授权；不要在备注中填写密钥。"),
    "BALANCE": ("供应商余额不足", "请核对供应商账户余额和本轮已发生费用，再决定是否重试。"),
    "PARAMETERS": ("请求参数不被接受", "请核对关键词、平台和请求参数后重试。"),
    "RATE_LIMIT": ("服务请求限流", "请降低并发并等待服务限流窗口结束后重试。"),
    "TIMEOUT": ("服务响应超时", "请核对接口记录与费用，确认结果后再重试。"),
    "SERVICE": ("上游服务或网络异常", "请检查服务可用性与网络，恢复后重试。"),
    "INTERRUPTED": (
        "执行中断，结果待核对",
        "执行记录已超时收尾，不自动重复付费请求；核对接口费用后手动重试。",
    ),
    "RESULT_WRITE": ("结果未完成入库", "请核对内容、存储和供应商费用后重试。"),
    "UNKNOWN": ("原因尚未归类", "请查看本轮执行明细及接口记录，核对成功部分与费用后重试。"),
}


def classify_collection_failure(cause: BaseException) -> str:
    seen: set[int] = set()
    current: BaseException | None = cause
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ViralSourceUnavailable):
            return "CONFIGURATION"
        if isinstance(current, TimeoutError):
            return "TIMEOUT"
        status = (
            current.status
            if isinstance(current, ViralSourceHttpStatusError)
            else current.code
            if isinstance(current, HTTPError)
            else None
        )
        if status in (401, 403):
            return "AUTHENTICATION"
        if status == 402:
            return "BALANCE"
        if status in (400, 422):
            return "PARAMETERS"
        if status == 429:
            return "RATE_LIMIT"
        if status is not None and status >= 500:
            return "SERVICE"
        if isinstance(current, URLError) and isinstance(current.reason, TimeoutError):
            return "TIMEOUT"
        if isinstance(current, URLError):
            return "SERVICE"
        current = current.__cause__ or current.__context__
    return "UNKNOWN"
