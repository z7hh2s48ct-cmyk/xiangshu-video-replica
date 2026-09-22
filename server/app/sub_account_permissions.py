"""子账号功能权限：读取与准入强制（CUSTOMER-CENTER-V2 Phase 3b / audit-40 权限矩阵）.

母账号通过 ``sub_account_permissions``（迁移 20260922T1800）给某个子账号限定
「12 类业务权限 + 2 项系统权限」；**无行 = 全允许**——与额度表「无行 = 不限」
对称，存量子账号零回填即保持原行为（fail-open，向后兼容）：

- ``allowed_businesses`` 存允许的业务键数组的 TEXT-JSON（仓库约定 JSON 全存
  TEXT）；规范化排序去重，空数组合法（全部业务禁用）。
- ``allow_api_keys`` / ``allow_publish_accounts`` 分别控制 Token 创建与发布
  账号绑定（导入/扫码）。
- 「设为管理员」（SUB_ADMIN）是 ``users.account_type`` 的状态，不属于本表。
- 全开值保存 = 删除行（``is_all_permissions``）：只有一种「全开」表达，
  避免「无行」与「全开行」两套等价态漂移。

强制点（每处都在写事务内、资源变更之前）：

- ``usage_billing.accept_operation``：业务准入，**独立于 credits**——未配置
  计价的免费操作同样受功能准入约束；
- ``api_key_routes._mutate_key``：Token 创建 / 默认 / 轮换；
- ``publish_browser_routes``：发布账号导入（Desktop 导出）与扫码绑定。

母账号（``parent_user_id IS NULL``）与无权限行的子账号一律直通；disabled 的
唯一答案是 403——三个稳定错误码分别对应三类准入。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException

# 12 类业务权限（与消费流水业务的客户口径一致）：键 → 中文标签。
# 顺序即 UI 勾选网格顺序（原型 v3「功能权限（12 类业务）」）。
BUSINESS_FEATURES: tuple[tuple[str, str], ...] = (
    ("video", "视频生成"),
    ("oral", "数字人口播"),
    ("character", "人物形象"),
    ("first_frame", "首帧图片"),
    ("analysis", "视频拆解"),
    ("rewrite", "文案改写"),
    ("asr", "语音识别"),
    ("link_resolution", "链接解析"),
    ("prompt_optimize", "提示词优化"),
    ("avatar_clone", "形象克隆"),
    ("voice_clone", "声音克隆"),
    ("viral_data", "爆款数据"),
)

BUSINESS_KEYS: frozenset[str] = frozenset(key for key, _ in BUSINESS_FEATURES)
BUSINESS_LABELS: dict[str, str] = dict(BUSINESS_FEATURES)

# 计费科目 → 业务键（见 ``billing_catalog.SERVICES``）：未列出的科目（内部
# 质检/修复、基础设施 cos/zpay）不受功能准入约束，直接放行。
SERVICE_FEATURE: dict[str, str] = {
    "video_768p": "video",
    "video_2k": "video",
    "analysis": "analysis",
    "first_frame": "first_frame",
    "character": "character",
    "rewrite": "rewrite",
    "oral": "oral",
    "asr": "asr",
    "viral_data": "viral_data",
    "link_resolution": "link_resolution",
    "prompt_optimize": "prompt_optimize",
    "avatar_clone": "avatar_clone",
    "voice_clone": "voice_clone",
}


class PermissionConnection(Protocol):
    """权限读取需要的最小连接面（与 ``sub_account_quota.QuotaConnection`` 同理）。

    ``params`` 取 ``Any``（参数逆变），同时接受 ``usage_billing`` 的
    ``BusinessConnection`` 与路由里的裸 ``psycopg`` 连接。
    """

    def execute(self, sql: str, params: Any = ...) -> Any: ...


@dataclass(frozen=True)
class SubAccountPermissions:
    """一个受限子账号的权限行（无行不构造此对象，读侧以 ``None`` 表达全允许）。"""

    businesses: frozenset[str]
    allow_api_keys: bool
    allow_publish_accounts: bool


# 一条 SQL 同时回答「是否子账号」与「是否有权限行」：母账号（parent NULL）
# 与无行的子账号（join 全 NULL）都折叠为「无限制」。
_READ_SQL = (
    "SELECT u.parent_user_id, p.allowed_businesses, p.allow_api_keys, "
    "p.allow_publish_accounts "
    "FROM users u LEFT JOIN sub_account_permissions p ON p.user_id = u.id "
    "WHERE u.id = %s"
)


def normalize_businesses(values: Sequence[str]) -> list[str]:
    """校验并规范化业务键：未知键报 ``ValueError``；去重后按声明顺序排序。"""
    unknown = sorted({str(value) for value in values} - BUSINESS_KEYS)
    if unknown:
        raise ValueError("未知业务权限键：" + "、".join(unknown))
    keep = {str(value) for value in values}
    return [key for key, _ in BUSINESS_FEATURES if key in keep]


def is_all_permissions(
    *,
    businesses: Sequence[str] | frozenset[str],
    allow_api_keys: bool,
    allow_publish_accounts: bool,
) -> bool:
    """是否全开（12 类业务 + 两项系统权限）：全开 = 删除行，"全开行"不存在。"""
    return allow_api_keys and allow_publish_accounts and set(businesses) >= BUSINESS_KEYS


def read_sub_account_permissions(
    conn: PermissionConnection, user_id: str
) -> SubAccountPermissions | None:
    """该账号的权限行；``None`` = 无限制（母账号或无行的子账号）。"""
    row = conn.execute(_READ_SQL, (user_id,)).fetchone()
    if row is None:
        return None
    parent_user_id, raw_businesses, allow_api_keys, allow_publish_accounts = row
    if parent_user_id is None or raw_businesses is None:
        return None
    if isinstance(raw_businesses, str):  # TEXT-JSON 列（JSON 全存 TEXT）
        raw_businesses = json.loads(raw_businesses)
    return SubAccountPermissions(
        businesses=frozenset(str(key) for key in raw_businesses),
        allow_api_keys=bool(allow_api_keys),
        allow_publish_accounts=bool(allow_publish_accounts),
    )


def _forbidden(code: str, message: str) -> HTTPException:
    return HTTPException(403, detail={"code": code, "message": message})


def enforce_sub_account_feature(conn: PermissionConnection, *, actor_id: str, service: str) -> None:
    """预扣/提交前的业务准入：禁用业务 → 403；母账号/无行/允许 → no-op。

    与额度强制不同，本检查不含并发敏感状态（权限行变更由管理端行锁串行化），
    不需要额外的 advisory lock。
    """
    feature = SERVICE_FEATURE.get(service)
    if feature is None:
        return
    permissions = read_sub_account_permissions(conn, actor_id)
    if permissions is None or feature in permissions.businesses:
        return
    label = BUSINESS_LABELS[feature]
    raise _forbidden(
        "SUB_ACCOUNT_FEATURE_DISABLED",
        f"权限受限：该子账号未开放「{label}」，请联系母账号管理员。",
    )


def enforce_sub_account_api_keys(conn: PermissionConnection, *, actor_id: str) -> None:
    """Token 创建/默认/轮换前的准入：禁用 → 403。"""
    permissions = read_sub_account_permissions(conn, actor_id)
    if permissions is None or permissions.allow_api_keys:
        return
    raise _forbidden(
        "SUB_ACCOUNT_API_KEYS_DISABLED",
        "权限受限：该子账号不允许创建 API Token，请联系母账号管理员。",
    )


def enforce_sub_account_publish_accounts(conn: PermissionConnection, *, actor_id: str) -> None:
    """发布账号导入/扫码绑定前的准入：禁用 → 403。"""
    permissions = read_sub_account_permissions(conn, actor_id)
    if permissions is None or permissions.allow_publish_accounts:
        return
    raise _forbidden(
        "SUB_ACCOUNT_PUBLISH_ACCOUNTS_DISABLED",
        "权限受限：该子账号不允许使用发布账号，请联系母账号管理员。",
    )
