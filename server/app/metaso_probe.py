"""视频生成服务的付费探针客户端。

付费探针要证明的是「生产那条提交 / 查询链路真的能跑通」，所以这里不另写一套
HTTP 调用，而是直接复用 ``MetasoH3Provider``：同样的请求校验、同样的鉴权头、
同样的调用日志。探针只做两件事——

1. 提交一次规格最小的文生视频任务（4 秒 · 768P · 纯文本，不带任何素材），
   用最低的费用走通「鉴权 → 余额 → 权限 → 受理」；
2. 用返回的任务编号读回一次任务状态（只读），证明生产轮询用到的查询权限可用。

不等待成片：视频生成通常要数分钟到数十分钟，探针的价值在于「受理成功」，
等成片既慢又不增加证据。

失败分类是本模块的核心：管理端要据此如实告诉操作者「这次有没有花钱」。
``submit_image_to_video`` 把所有传输层失败一律包成 ``SubmissionUncertain``，
对生产任务这是正确的保守，但对探针会把「密钥填错」也说成「可能已扣费」，
所以这里沿异常链取回 HTTP 状态码，并在 200 响应缺 task_id 时检查响应体，
只有**确认供应商明确拒绝**才断言未创建任务，其余一律按「结果不确定」处理。
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.error import HTTPError

from app.external_calls import parse_provider_error
from app.generation import (
    H3ProviderFailed,
    MetasoH3Provider,
    MetasoHttpTransport,
    SubmissionUncertain,
    UrllibMetasoHttpTransport,
    build_h3_request,
)

# 提示词只需满足「非空」；内容中性、无人物无文字，避免触发供应商的内容审核而
# 让探针因审核失败，掩盖真正想验证的鉴权与计费链路。
PROBE_PROMPT = "一只白色的猫安静地坐在窗台上，看向窗外。"
# H3 支持的最短时长与最低分辨率；T2V 必须给出具体画幅比例（adaptive 会被拒）。
PROBE_DURATION_SECONDS = 4
PROBE_RESOLUTION = "768P"
PROBE_RATIO = "16:9"

_MAX_CAUSE_DEPTH = 8


class MetasoProbeError(RuntimeError):
    """探针失败的共同基类。"""


class MetasoProbeSettingsUnavailable(MetasoProbeError):
    """没有可用的 API Key：请求尚未发出，肯定没有产生费用。"""


class MetasoProbeRejected(MetasoProbeError):
    """供应商明确拒绝了提交（4xx，或 200 响应里带明确错误）：肯定没有创建任务。"""

    def __init__(self, *, http_status: int | None, reason: str | None) -> None:
        self.http_status = http_status
        self.reason = reason
        super().__init__(reason or f"HTTP {http_status}")


class MetasoProbeUncertain(MetasoProbeError):
    """提交结果无法确认（超时、5xx、响应无法识别）：任务可能已经创建并计费。"""


class MetasoProbeReadBackFailed(MetasoProbeError):
    """任务已受理（可能已计费），但随后读回状态失败。"""

    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        super().__init__("provider task status read-back failed")


class _LastResponseTransport:
    """转手记录最近一次成功返回的响应体。

    提交失败时要靠它判断「供应商是不是明确报了错」：``submit_image_to_video`` 在
    响应缺 task_id 时只抛一个不带响应体的异常，事后再读就来不及了。
    """

    def __init__(self, inner: MetasoHttpTransport) -> None:
        self.inner = inner
        self.last_body: bytes | None = None

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None = None,
    ) -> bytes:
        # 每次调用前清空，避免把上一次调用的响应体当成这一次的。
        self.last_body = None
        response = self.inner.request(method, url, headers=headers, body=body)
        self.last_body = response
        return response


def _http_status(exc: BaseException) -> int | None:
    """沿异常链找底层 HTTPError 的状态码；找不到（超时、断网等）返回 None。"""
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None:
            return None
        if isinstance(current, HTTPError):
            return current.code
        current = current.__cause__
    return None


class MetasoPaidProbeClient:
    def __init__(self, *, api_key: str, transport: MetasoHttpTransport | None = None) -> None:
        key = api_key.strip()
        if not key:
            raise MetasoProbeSettingsUnavailable("provider API key is not configured")
        self._recorder = _LastResponseTransport(transport or UrllibMetasoHttpTransport())
        self._provider = MetasoH3Provider(api_key=key, transport=self._recorder)

    def submit_minimal_video(self) -> str:
        """提交规格最小的文生视频任务，返回供应商任务编号。"""
        request = build_h3_request(
            prompt_text=PROBE_PROMPT,
            duration_seconds=PROBE_DURATION_SECONDS,
            resolution=PROBE_RESOLUTION,
            ratio=PROBE_RATIO,
        )
        try:
            return self._provider.submit_image_to_video(request)
        except SubmissionUncertain as exc:
            status = _http_status(exc)
            if status is not None:
                if 400 <= status < 500:
                    # 4xx 意味着请求被供应商直接拒收，没有受理。
                    raise MetasoProbeRejected(http_status=status, reason=f"HTTP {status}") from exc
                raise MetasoProbeUncertain("provider submission result is unknown") from exc
            # 没有 HTTP 状态码：可能是超时，也可能是「200 但响应里没有 task_id」。
            # 后者只有在响应体明确报错时才能断言未受理。
            code, message = parse_provider_error(self._recorder.last_body, allow_plain_text=False)
            reason = message or code
            if reason:
                raise MetasoProbeRejected(http_status=None, reason=reason) from exc
            raise MetasoProbeUncertain("provider submission result is unknown") from exc

    def read_back(self, task_id: str) -> None:
        """只读查询一次任务状态；不关心状态本身，只证明查询链路可用。"""
        try:
            self._provider.query_image_to_video(task_id)
        except H3ProviderFailed as exc:
            raise MetasoProbeReadBackFailed(task_id) from exc
