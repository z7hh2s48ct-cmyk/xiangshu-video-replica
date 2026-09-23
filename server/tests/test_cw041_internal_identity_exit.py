"""CW-041: the internal identity surface is physically retired.

The pre-GA slice (PR #77) proved the entries fail closed; this batch phase
(CW-042-b, owner decision D5) deletes ``app/internal_accounts.py`` outright —
the internal account/token management CLI has no remaining consumer, and the
internal-access-token TABLE stays only because published migrations are frozen
(auth.py's PG-lane Bearer probe and control_routes' dashboard join still read
it).

Pinned here:

- The module stays deleted: any reintroduction of ``app.internal_accounts``
  (or a new app-level reference to it) fails this contract.
- The published ``internal_access_tokens`` table remains reachable read-only
  on the PG lane — the admin dashboard join must keep working.
- ``app.internal_billing`` deliberately STAYS: despite the historical name it
  is the live customer wallet core (CW-029 reused its RESERVE/SETTLE/RELEASE
  as the recharge/settlement engine; generation.py's PG worker bills through
  it). It is NOT part of the internal-identity exit.
"""

from __future__ import annotations

from pathlib import Path

_APP_DIR = Path(__file__).resolve().parent.parent / "app"


def test_internal_accounts_module_stays_deleted() -> None:
    assert not (_APP_DIR / "internal_accounts.py").exists(), (
        "app/internal_accounts.py was retired by CW-041/CW-042-b; "
        "reintroduction requires an owner decision"
    )


def test_no_app_module_references_internal_accounts() -> None:
    offenders = [
        path.name
        for path in sorted(_APP_DIR.glob("*.py"))
        if "internal_accounts" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"app modules reference the retired internal_accounts surface: {offenders}"
    )


def test_internal_billing_module_remains_the_wallet_core() -> None:
    """Scope guard: internal_billing is the LIVE customer wallet engine
    (CW-029 reused it for recharge/settlement) — the identity exit must not
    sweep it away."""
    assert (_APP_DIR / "internal_billing.py").exists()
    text = (_APP_DIR / "generation.py").read_text(encoding="utf-8")
    assert "from app.internal_billing import" in text


def test_internal_access_tokens_table_still_probed_on_pg_lane() -> None:
    """The published table is frozen (迁移红线); the auth probe and the
    admin dashboard join read it — they must survive the retirement."""
    auth_text = (_APP_DIR / "auth.py").read_text(encoding="utf-8")
    assert "internal_access_tokens" in auth_text
    control_text = (_APP_DIR / "control_routes.py").read_text(encoding="utf-8")
    assert "internal_access_tokens" in control_text
