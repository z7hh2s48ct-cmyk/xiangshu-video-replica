"""独立告警名额、租约和可见结果；邮件调用不持有数据库事务。"""

import logging
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.alert_policy import load_alert_rules, policy_for
from app.db_pg import pg_transaction
from app.db_portable import BusinessConnection

if TYPE_CHECKING:
    from app.failure_rate_alerts import AlertsOverview

logger = logging.getLogger(__name__)


def deliver_alerts(overview: "AlertsOverview") -> None:
    try:
        _deliver(overview)
    except Exception as exc:  # noqa: BLE001 — 通知不可拖垮采集或生成。
        logger.warning("alert dispatch failed: %s", type(exc).__name__)


def _deliver(overview: "AlertsOverview") -> None:
    from app.email_delivery import deliver_quietly, email_sender_from_settings

    grouped: dict[str, list[tuple[Any, str, str, str]]] = defaultdict(list)
    addresses: dict[str, str] = {}
    with pg_transaction() as raw:
        conn = BusinessConnection.postgres(raw)
        policies, _rules = load_alert_rules(conn)
        default = conn.execute("SELECT recipient_user_id FROM alert_settings WHERE id=1").fetchone()
        try:
            sender = email_sender_from_settings(conn)
        except Exception as exc:  # noqa: BLE001 — 无效配置同样显式待配置，不记送达。
            logger.warning("alert channel configuration invalid: %s", type(exc).__name__)
            sender = None
        for item in overview.items:
            policy = policy_for(item.key, policies)
            if not policy.enabled or item.count < policy.threshold_count:
                continue
            recipient = policy.recipient_user_id or (
                str(default[0]) if default and default[0] else None
            )
            recipient_key = recipient or "UNASSIGNED"
            target = (
                conn.execute(
                    "SELECT email FROM users WHERE id=%s AND role IN ('admin','audito"
                    "r') AND is_active=1 AND email IS NOT NULL AND email<>''",
                    (recipient,),
                ).fetchone()
                if recipient
                else None
            )
            now = datetime.fromisoformat(overview.generated_at).astimezone(UTC)
            period = item.dedup_period[:7] if item.dedup_period else now.strftime("%Y-%m-%dT%H")
            event_key = f"{item.key}:{period}"
            unavailable = (
                "CHANNEL_NOT_CONFIGURED"
                if policy.channel is None
                or sender is None
                or not getattr(sender, "alert_digest_configured", True)
                else "RECIPIENT_NOT_CONFIGURED"
                if target is None
                else None
            )
            if unavailable:
                conn.execute(
                    """INSERT INTO alert_deliveries
                    (event_key,recipient_key,alert_key,state,last_error)
                    VALUES(%s,%s,%s,'UNCONFIGURED',%s) ON CONFLICT(event_key,recipient_key)
                    DO UPDATE SET state='UNCONFIGURED',last_error=excluded.last_error,
                    updated_at=clock_timestamp()
                    WHERE alert_deliveries.state IN ('UNCONFIGURED','FAILED')""",
                    (event_key, recipient_key, item.key, unavailable),
                )
                continue
            token = str(uuid.uuid4())
            claimed = conn.execute(
                """INSERT INTO alert_deliveries(event_key,recipient_key,alert_key,state,
                    lease_token,lease_until,attempts,last_attempt_at)
                VALUES(%s,%s,%s,'CLAIMED',%s,
                    clock_timestamp()+interval '5 minutes',1,clock_timestamp())
                ON CONFLICT(event_key,recipient_key) DO UPDATE SET state='CLAIMED',
                    lease_token=excluded.lease_token,
                  lease_until=excluded.lease_until,attempts=alert_deliveries.attempts+1,
                  last_attempt_at=clock_timestamp(),updated_at=clock_timestamp()
                WHERE alert_deliveries.state IN ('FAILED','UNCONFIGURED')
                   OR (alert_deliveries.state='CLAIMED'
                      AND alert_deliveries.lease_until<clock_timestamp())
                RETURNING lease_token""",
                (event_key, recipient_key, item.key, token),
            ).fetchone()
            if claimed:
                grouped[recipient_key].append((item, event_key, recipient_key, token))
                assert target is not None
                addresses[recipient_key] = str(target[0])
    # 上面认领已提交；发信期间不占用池连接或事务锁。
    if sender is None:
        return
    for recipient_key, entries in grouped.items():
        items = [entry[0] for entry in entries]
        lines = "\n".join(f"- {item.headline}（{item.detail}）" for item in items)
        try:
            delivered = deliver_quietly(
                lambda: sender.send_alert_digest(
                    to=addresses[recipient_key],
                    total=len(items),
                    danger_count=sum(item.severity == "danger" for item in items),
                    items=lines,
                    generated_at=overview.generated_at,
                ),
                kind="alert_digest",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert email delivery failed: %s", type(exc).__name__)
            delivered = False
        with pg_transaction() as raw:
            for _item, event_key, target_key, token in entries:
                raw.execute(
                    """UPDATE alert_deliveries SET state=%s,last_error=%s,
                    sent_at=CASE WHEN %s THEN clock_timestamp() ELSE sent_at END,
                    lease_token=NULL,lease_until=NULL,updated_at=clock_timestamp()
                    WHERE event_key=%s AND recipient_key=%s AND lease_token=%s""",
                    (
                        "SENT" if delivered else "FAILED",
                        None if delivered else "EMAIL_DELIVERY_FAILED",
                        delivered,
                        event_key,
                        target_key,
                        token,
                    ),
                )
