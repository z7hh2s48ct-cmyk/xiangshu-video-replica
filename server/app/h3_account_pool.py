"""H3 account configuration and scheduler routing; all mutations share the capacity lock."""

import hashlib
from typing import Any

from fastapi import HTTPException

from app.db_portable import BusinessConnection
from app.settings import SettingsDecryptError, SettingsRepository

LEGACY_ACCOUNT_ID = "legacy-metaso"
# An uncertain submission may still be rendering upstream. Never free its slot
# until reconciliation establishes a terminal state.
ACTIVE_STATUSES = "('SUBMITTING','RUNNING','ARCHIVING','SUBMISSION_UNCERTAIN')"


def pool_capacity(conn: BusinessConnection) -> int | None:
    if not conn.is_postgres:
        return None
    row = conn.execute(
        "SELECT count(*), COALESCE(sum(concurrency_limit) FILTER (WHERE enabled),0) "
        "FROM h3_provider_accounts"
    ).fetchone()
    return int(row[1]) if row and row[0] else None


def _usage_sql(account: str) -> str:
    return (
        "(SELECT count(*) FROM h3_provider_task_accounts binding "
        "JOIN generation_tasks busy ON busy.id=binding.task_id "
        f"WHERE binding.account_id={account}.id AND busy.status IN {ACTIVE_STATUSES})"
    )


def eligible_task_sql(conn: BusinessConnection, alias: str) -> str:
    """Static internal SQL aliases only; called under the runtime capacity lock."""
    if alias not in {"t", "generation_tasks"}:
        raise ValueError("Unsupported task alias")
    if pool_capacity(conn) is None:
        return "TRUE"
    from app.generation import read_runtime_limits, shared_generation_capacity_available

    other_available = shared_generation_capacity_available(
        conn, max_concurrent_tasks=read_runtime_limits(conn)["max_concurrent_h3_tasks"]
    )
    other_sql = "TRUE" if other_available else "FALSE"
    return f"""(
        ({alias}.provider != 'metaso' AND {other_sql})
        OR {alias}.status = 'SUCCEEDED'
        OR ({alias}.provider = 'metaso' AND EXISTS (
          SELECT 1 FROM h3_provider_accounts account
          WHERE account.enabled
            AND {_usage_sql("account")} < account.concurrency_limit
            AND (NOT EXISTS (SELECT 1 FROM h3_provider_task_accounts pin
                             WHERE pin.task_id={alias}.id)
                 OR EXISTS (SELECT 1 FROM h3_provider_task_accounts pin
                            WHERE pin.task_id={alias}.id AND pin.account_id=account.id))
        ))
    )"""


def assign_task_account(conn: BusinessConnection, task: dict[str, Any]) -> None:
    if task["provider"] != "metaso" or pool_capacity(conn) is None:
        return
    task_id = str(task["id"])
    if conn.execute(
        "SELECT 1 FROM h3_provider_task_accounts WHERE task_id=%s", (task_id,)
    ).fetchone():
        return
    row = conn.execute(
        f"""SELECT account.id FROM h3_provider_accounts account
            WHERE enabled AND {_usage_sql("account")} < concurrency_limit
            ORDER BY last_dispatched_at ASC NULLS FIRST, id LIMIT 1 FOR UPDATE"""
    ).fetchone()
    if row is None:
        raise RuntimeError("H3 account capacity changed during a locked dispatch")
    conn.execute(
        "INSERT INTO h3_provider_task_accounts(task_id,account_id) VALUES (%s,%s)",
        (task_id, row[0]),
    )
    conn.execute(
        "UPDATE h3_provider_accounts SET last_dispatched_at=clock_timestamp() WHERE id=%s",
        (row[0],),
    )


def account_api_key(conn: BusinessConnection, *, task_id: str | None = None) -> str:
    repository = SettingsRepository(conn)
    if pool_capacity(conn) is None:
        return repository.load_provider_config("metaso").get("api_key", "")
    if task_id:
        row = conn.execute(
            "SELECT a.encrypted_api_key FROM h3_provider_accounts a "
            "JOIN h3_provider_task_accounts p ON p.account_id=a.id WHERE p.task_id=%s",
            (task_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT encrypted_api_key FROM h3_provider_accounts WHERE enabled ORDER BY id LIMIT 1"
        ).fetchone()
    if row is None:
        raise SettingsDecryptError("H3 account is unavailable or task has no account binding")
    try:
        return repository.fernet.decrypt(str(row[0]).encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise SettingsDecryptError("H3 account credentials cannot be read") from exc


def read_accounts(conn: BusinessConnection) -> dict[str, Any]:
    rows = conn.execute(
        f"SELECT id,name,concurrency_limit,enabled,version,{_usage_sql('a')} AS active_tasks "
        "FROM h3_provider_accounts a ORDER BY created_at,id"
    ).fetchall()
    accounts = [dict(row) | {"configured": True} for row in rows]
    if not accounts:
        legacy = SettingsRepository(conn).load_provider_config("metaso").get("api_key")
        if legacy:
            runtime = conn.execute(
                "SELECT max_concurrent_h3_tasks FROM runtime_settings WHERE id=1"
            ).fetchone()
            accounts = [
                {
                    "id": LEGACY_ACCOUNT_ID,
                    "name": "原视频生成账号",
                    "concurrency_limit": int(runtime[0]) if runtime else 2,
                    "enabled": True,
                    "version": 0,
                    "active_tasks": 0,
                    "configured": True,
                }
            ]
    return {
        "accounts": accounts,
        "managed": bool(rows),
        "total_concurrency": sum(int(a["concurrency_limit"]) for a in accounts if a["enabled"]),
    }


def _insert_account(
    conn: BusinessConnection, account_id: str, name: str, key: str, limit: int, enabled: bool
) -> None:
    conn.execute(
        "INSERT INTO h3_provider_accounts"
        "(id,name,encrypted_api_key,key_digest,concurrency_limit,enabled) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (
            account_id,
            name,
            SettingsRepository(conn).fernet.encrypt(key.encode()).decode("ascii"),
            hashlib.sha256(key.encode()).hexdigest(),
            limit,
            enabled,
        ),
    )


def save_account(
    conn: BusinessConnection,
    *,
    account_id: str,
    name: str,
    api_key: str,
    concurrency_limit: int,
    enabled: bool,
    expected_version: int,
) -> dict[str, Any]:
    from app.generation import lock_shared_generation_capacity

    lock_shared_generation_capacity(conn)
    if not name.strip() or not 1 <= concurrency_limit <= 1_000_000:
        raise HTTPException(422, detail="请填写账号名称及正整数并发上限。")
    # Import once, before any newly configured key can receive a task. Historical
    # tasks remain pinned too, so reconciliation never queries another account.
    initialized_legacy = False
    if pool_capacity(conn) is None:
        legacy_key = SettingsRepository(conn).load_provider_config("metaso").get("api_key", "")
        started = (
            "provider='metaso' AND (attempt>0 OR provider_task_id IS NOT NULL "
            "OR status NOT IN ('PENDING','QUEUED'))"
        )
        if (
            not legacy_key
            and conn.execute(f"SELECT 1 FROM generation_tasks WHERE {started} LIMIT 1").fetchone()
        ):
            raise HTTPException(409, detail="请先恢复原视频账号密钥，以关联已有任务。")
        if legacy_key:
            old_limit = conn.execute(
                "SELECT max_concurrent_h3_tasks FROM runtime_settings WHERE id=1"
            ).fetchone()[0]
            _insert_account(
                conn, LEGACY_ACCOUNT_ID, "原视频生成账号", legacy_key, int(old_limit), True
            )
            conn.execute(
                "INSERT INTO h3_provider_task_accounts(task_id,account_id) "
                f"SELECT id,%s FROM generation_tasks WHERE {started}",
                (LEGACY_ACCOUNT_ID,),
            )
            initialized_legacy = account_id == LEGACY_ACCOUNT_ID
    row = conn.execute(
        "SELECT * FROM h3_provider_accounts WHERE id=%s FOR UPDATE", (account_id,)
    ).fetchone()
    actual_version = int(row["version"]) if row else 0
    if expected_version != (0 if initialized_legacy else actual_version):
        raise HTTPException(409, detail="账号配置已更新，请刷新后再保存。")
    key = api_key.strip()
    if not row and not key:
        raise HTTPException(422, detail="新增账号需要填写 API Key。")
    if key:
        digest = hashlib.sha256(key.encode()).hexdigest()
        if conn.execute(
            "SELECT 1 FROM h3_provider_accounts WHERE key_digest=%s AND id!=%s",
            (digest, account_id),
        ).fetchone():
            raise HTTPException(409, detail="该密钥已添加，不能重复计算账号并发。")
        if (
            row
            and digest != row["key_digest"]
            and conn.execute(
                "SELECT 1 FROM h3_provider_task_accounts WHERE account_id=%s LIMIT 1", (account_id,)
            ).fetchone()
        ):
            raise HTTPException(
                409, detail="此账号已有任务，请新增账号并暂停旧账号，以保留任务查询能力。"
            )
    if row:
        encrypted = (
            SettingsRepository(conn).fernet.encrypt(key.encode()).decode("ascii")
            if key
            else row["encrypted_api_key"]
        )
        conn.execute(
            "UPDATE h3_provider_accounts SET name=%s,encrypted_api_key=%s,key_digest=%s,"
            "concurrency_limit=%s,enabled=%s,version=version+1,updated_at=now() WHERE id=%s",
            (
                name.strip(),
                encrypted,
                hashlib.sha256(key.encode()).hexdigest() if key else row["key_digest"],
                concurrency_limit,
                enabled,
                account_id,
            ),
        )
    else:
        _insert_account(conn, account_id, name.strip(), key, concurrency_limit, enabled)
    return read_accounts(conn)
