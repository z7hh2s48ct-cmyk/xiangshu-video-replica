"""审计列表、详情和导出共用的中文业务摘要；缺失历史证据不冒充快照。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.billing_catalog import COST_SUBJECTS, SERVICES

EVENT_LABELS = {
    "ADMIN_ADJUSTMENT": "人工调整积分",
    "ADMIN_DEVICE_UNBIND": "解绑设备",
    "ADMIN_DEVICE_DISABLE": "永久禁用设备",
    "ADMIN_DEVICE_REVOKE": "永久禁用设备",
    "ADMIN_DEVICE_PAIRING_ADMIN_APPROVED": "管理员批准设备绑定",
    "ADMIN_DEVICE_REPLACE": "替换绑定设备",
    "ADMIN_SESSION_LOGOUT": "下线",
    "ADMIN_SESSION_SWITCH": "切换登录设备",
    "analysis.create": "创建视频拆解",
    "ACTIVATION_CODE_ISSUED": "启用激活码",
    "ACTIVATION_CODE_ACTIVATED": "激活客户账户",
    "ACTIVATION_CODE_SUSPENDED": "暂停激活码",
    "ACTIVATION_CODE_RESUMED": "恢复激活码",
    "ACTIVATION_CODE_REVOKED": "撤销激活码",
    "ACTIVATION_CODE_ARCHIVED": "归档激活码",
    "ACTIVATION_CODE_DELIVERED": "交付激活码",
    "admin.activation_code.archived": "归档激活码",
    "admin.activation_code.revealed": "查看激活码明文",
    "admin.activation_code.revealed_replay": "查看激活码明文（重放）",
    "admin.activation_code_batch.created": "创建激活码批次",
    "customer.suspend": "暂停客户",
    "customer.resume": "恢复客户",
    "customer_annotation.update": "修改客户标注",
    "customer.annotations.update": "修改客户标注",
    "customer_unit_price.update": "修改客户单价",
    "customer_unit_price.reset": "恢复客户默认单价",
    "customer_adjustment.create": "提交人工调整",
    "customer_package.grant": "开通套餐（已收款）",
    "customer_discount.create": "设置专项折扣",
    "customer_discount.deactivate": "停用专项折扣",
    "customer_pricing.update": "修改全局报价",
    "operation_rate.update": "修改业务价格",
    "billing.tariff.update": "修改业务售价与成本",
    "billing_settings.update": "修改充值与计价设置",
    "recharge_package.create": "新增充值套餐",
    "recharge_package.update": "修改充值套餐",
    "registration_bonus.settings.update": "修改注册赠送",
    "payment.sync": "同步收款状态",
    "payment.ledger_repair": "补记已收款积分",
    "payment.provider.update": "修改支付方式",
    "payment.wechat.update": "修改微信收款配置",
    "zpay_settings.update": "修改收款通道配置",
    "provider_settings.update": "修改服务配置",
    "provider_settings.secret_reveal": "查看接口密钥明文",
    "provider_settings.paid_test": "付费连接测试",
    "runtime_settings.update": "修改运行参数",
    "h3.account.update": "修改视频服务账号",
    "viral_runtime.update": "修改采集设置",
    "viral_platform.probe": "探测采集平台连接",
    "control.export": "导出数据",
    "control.reconciliation.read": "查看对账结果",
    "external_call.response_view": "查看接口原始响应",
    "generation_record.content_view": "查看生成内容",
    "generation_record.thumbnail_view": "查看生成缩略图",
    "admin_session.password_login": "管理员密码登录",
    "admin_session.exchange": "使用恢复凭据登录",
    "admin_team.create": "新增团队成员",
    "admin_team.update": "修改团队权限",
    "admin_team.password_reset": "重置团队成员密码",
    "team.member.create": "新增团队成员",
    "team.member.update": "修改团队权限",
    "team.member.password_reset": "重置团队成员密码",
}
GROUP_LABELS = {
    "funds": "资金",
    "pricing": "价格与套餐",
    "account": "账号与设备",
    "system": "系统配置",
    "content": "内容与采集",
    "secret_export": "密钥与导出",
    "login": "登录",
}
FIELD_LABELS = {
    "available_credits": "可用积分",
    "collection_enabled": "定时采集",
    "import_enabled": "客户链接导入",
    "keywords": "关键词",
    "per_keyword_limit": "默认每词条数",
    "collection_interval_days": "采集间隔（天）",
    "collection_time": "上海执行时刻",
    "quality_min_likes": "最低点赞",
    "quality_duration_min_ms": "最短时长",
    "quality_duration_max_ms": "最长时长",
    "quality_exclude_words": "排除词",
    "monthly_budget_fen": "月度预算",
    "api_key_state": "接口密钥",
    "service_endpoint_state": "服务地址",
    "max_generation_count_per_batch": "每批生成上限",
    "max_concurrent_h3_tasks": "视频服务并发数",
    "active_storage_provider": "存储服务",
    "display_name": "成员名称",
    "note_chars": "备注字符数",
    "reserved_credits": "生成冻结积分",
    "unit_credits": "售价",
    "unit_cost_fen": "成本",
    "enabled": "收费状态",
    "is_active": "账号状态",
    "status": "状态",
    "customer_unit_price": "客户单价",
    "points_per_yuan": "1元对应积分",
    "discount_basis_points": "全局折扣",
    "video_768p": "视频生成768P单价",
    "video_2k": "视频生成2K单价",
    "oral": "口播单价",
    "consumption_rounding": "扣费取整",
    "unit_rounding": "用量取整",
    "amount_fen": "金额",
    "credits": "积分",
    "discount_rate": "折扣",
    "valid_until": "有效期",
    "name": "名称",
    "role": "角色",
    "is_super_admin": "超级管理员权限",
    "internal_base_unit_price_fen": "默认单价",
    "oral_unit_price_fen": "口播单价",
    "min_recharge_fen": "最低充值",
    "recharge_step_fen": "充值步长",
    "collection_interval_seconds": "采集间隔秒",
    "max_videos_per_keyword": "每关键词采集上限",
    "min_digg_count": "最低点赞数",
    "min_play_count": "最低播放数",
    "auto_collect_enabled": "自动采集",
    "budget_daily_fen": "每日预算",
    "daily_budget_fen": "每日预算",
    "tags": "客户标签",
    "note": "客户备注",
    "owner_username": "负责人",
    "enabled_channels": "支付方式",
    "default_provider": "默认支付方式",
    "notify_sensitive_operations": "高敏告警",
}
ENTITY_LABELS = {
    "runtime_settings": "运行参数",
    "team_member": "团队成员",
    "customer_pricing": "全局报价",
    "billing_settings": "充值计价",
    "recharge_package": "充值套餐",
    "payment_provider": "收款设置",
    "wechat_settings": "微信收款配置",
    "provider_settings": "服务配置",
    "control_ledger": "数据导出",
    "registration_bonus": "注册赠送",
    "admin_team": "团队权限",
    "ACTIVATION_CODE": "激活码",
    "ACTIVATION_CODE_DELIVERY": "激活码交付",
    "DEVICE": "登录设备",
    "CUSTOMER_SESSION": "客户登录",
}


def audit_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    return {
        key: {"before": before.get(key), "after": after.get(key)}
        for key in FIELD_LABELS
        if key in before or key in after
        if before.get(key) != after.get(key)
    }


def provider_audit_changes(before: dict[str, str], after: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field, label in [
        ("api_key", "api_key_state"),
        ("key", "api_key_state"),
        ("endpoint", "service_endpoint_state"),
        ("base_url", "service_endpoint_state"),
    ]:
        if before.get(field) != after.get(field):
            result[label] = {
                "before": "已配置" if before.get(field) else "未配置",
                "after": "已更新" if after.get(field) else "未配置",
            }
    return result


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _value(field: str, value: object) -> str:
    if value is None:
        return "待核对"
    if field == "customer_unit_price" and isinstance(value, dict):
        price = _decimal(value.get("effective_unit_price_fen"))
        return ("默认单价" if value.get("mode") == "DEFAULT" else "专项单价") + (
            f" ¥{price / 100:.2f}" if price is not None else "（金额待核对）"
        )
    if isinstance(value, bool):
        return "启用" if value else "停用"
    number = _decimal(value)
    if field.endswith("_ms") and number is not None:
        return f"{_number(number / 1000)} 秒"
    if field.endswith("_fen") and number is not None:
        return "< ¥0.01" if 0 < abs(number) < 1 else f"¥{number / 100:.2f}"
    if field in {"discount_rate", "discount_basis_points"} and number is not None:
        return f"{_number(number * (10 if field == 'discount_rate' else Decimal('0.001')))}折"
    if (
        "credits" in field or field in {"points_per_yuan", "video_768p", "video_2k", "oral"}
    ) and number is not None:
        return f"{_number(number)} 积分"
    if isinstance(value, (list, tuple)):
        return "、".join(str(item) for item in value if isinstance(item, (str, int))) or "无"
    if isinstance(value, dict):
        return "已调整（业务快照不完整）"
    return {
        "BOUND": "已绑定",
        "UNBOUND": "已解绑",
        "REVOKED": "永久禁用",
        "ACTIVE": "正常",
        "SUSPENDED": "已暂停",
        "ceil": "向上取整",
        "floor": "向下取整",
        "exact": "按实际用量",
        "admin": "管理员",
        "auditor": "审计员",
        "alipay": "支付宝",
        "wxpay": "微信",
        "offline": "线下转账",
    }.get(str(value), str(value))


def _safe_field_value(field: str, value: object) -> object:
    if isinstance(value, dict):
        if field != "customer_unit_price":
            return None
        return {
            key: item
            for key, item in value.items()
            if key in {"mode", "custom_unit_price_fen", "effective_unit_price_fen"}
            and isinstance(item, (str, int, float, type(None)))
        }
    if isinstance(value, list):
        return [item for item in value if isinstance(item, (str, int, float))]
    return value if isinstance(value, (str, int, float, bool, type(None))) else None


def business_detail(detail: object) -> dict[str, Any] | None:
    """只释放业务白名单；密钥和凭据字段即使误入历史元数据也不得出境。"""
    if not isinstance(detail, dict):
        return None
    result: dict[str, Any] = {}
    for snapshot in ("old", "new"):
        value = detail.get(snapshot)
        if isinstance(value, dict):
            result[snapshot] = {
                key: _safe_field_value(key, item)
                for key, item in value.items()
                if key in FIELD_LABELS
            }
        elif snapshot in detail and value is None:
            result[snapshot] = None
    changes = detail.get("changes")
    if isinstance(changes, dict):
        result["changes"] = {
            key: {side: _safe_field_value(key, value.get(side)) for side in ("before", "after")}
            for key, value in changes.items()
            if key in FIELD_LABELS and isinstance(value, dict)
        }
    metadata = detail.get("metadata")
    if isinstance(metadata, dict):
        legacy = result.setdefault("changes", {})
        for key in FIELD_LABELS:
            if f"old_{key}" in metadata or f"new_{key}" in metadata:
                legacy[key] = {
                    "before": _safe_field_value(key, metadata.get(f"old_{key}")),
                    "after": _safe_field_value(key, metadata.get(f"new_{key}")),
                }
        if not result.get("new"):
            after = {
                key: _safe_field_value(key, value)
                for key, value in metadata.items()
                if key in FIELD_LABELS
            }
            if after:
                result["new"] = after
    # Correlation metadata does not reconstruct missing historical business snapshots.
    # Empty/null before-and-after values must retain the explicit unknown boundary.
    return result if any(result.get(key) for key in ("old", "new", "changes")) else None


def audit_business_fields(
    item: dict[str, Any], groups: dict[str, tuple[str, ...]]
) -> dict[str, Any]:
    event = item["event_type"]
    source = item["source_document_type"]
    detail = business_detail(item.get("change_detail"))
    changes = dict(detail.get("changes", {})) if detail else {}
    if detail and ("old" in detail or "new" in detail):
        before, after = detail.get("old") or {}, detail.get("new") or {}
        changes.update(
            {
                key: {"before": before.get(key), "after": after.get(key)}
                for key in FIELD_LABELS
                if key in before or key in after
                if before.get(key) != after.get(key)
            }
        )
    summary = "；".join(
        f"{FIELD_LABELS[key]}：{_value(key, value.get('before'))}"
        f" → {_value(key, value.get('after'))}"
        for key, value in changes.items()
    )
    if not summary and (
        item.get("old_unit_price_fen") is not None or item.get("new_unit_price_fen") is not None
    ):
        summary = f"售价：{_value('amount_fen', item.get('old_unit_price_fen'))} → "
        summary += _value("amount_fen", item.get("new_unit_price_fen"))
    if not summary:
        summary = {
            "ADMIN_DEVICE_UNBIND": "已解绑；在线会话已结束",
            "ADMIN_DEVICE_DISABLE": "设备已永久禁用；原状态历史未记录",
            "ADMIN_SESSION_LOGOUT": "在线 → 离线",
            "customer.suspend": "正常 → 已暂停",
            "customer.resume": "已暂停 → 正常",
            "admin_session.password_login": "密码登录成功",
            "admin_session.exchange": "恢复凭据登录成功",
            "control.export": "数据已导出",
            "provider_settings.secret_reveal": "已查看接口密钥明文",
            "admin.activation_code.revealed": "已查看激活码明文",
            "admin.activation_code.revealed_replay": "已重放激活码明文",
        }.get(event, "历史未记录业务前后快照，待核对")
    label = EVENT_LABELS.get(event, "未映射操作（待核对）")
    negative = False
    balance = changes.get("available_credits", {})
    before, after = _decimal(balance.get("before")), _decimal(balance.get("after"))
    if before is not None and after is not None:
        negative = after < before
    if event == "ADMIN_ADJUSTMENT":
        label = (
            "退款扣减"
            if negative or source == "REFUND_APPROVAL"
            else {
                "OFFLINE_PAYMENT": "开通套餐（已收款）",
                "FREE_GRANT": "赠送积分",
                "CREDIT_COMPENSATION": "补偿积分",
                "COMPENSATION_APPROVAL": "补偿积分",
                "CS_TICKET": "客服调整积分",
                "LEDGER_CORRECTION": "积分更正",
            }.get(source, "人工调整积分")
        )
    group = next((key for key, events in groups.items() if event in events), "system")
    subject = item.get("change_subject") or ""
    service = SERVICES.get(COST_SUBJECTS.get(subject, subject))
    target = (
        item.get("target_company_name")
        or item.get("target_username")
        or (service.name if service else ENTITY_LABELS.get(source, "业务配置项"))
    )
    return {
        "event_label": label,
        "event_group": group,
        "event_group_label": GROUP_LABELS[group],
        "target_label": target,
        "change_summary": summary,
        "change_detail": detail,
        "negative_adjustment": negative,
    }
