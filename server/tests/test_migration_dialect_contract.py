"""Migration dialect-parameter contract (M0 review C1 follow-up).

Every ``create_index`` call that declares ``sqlite_where`` must also declare
``postgresql_where``: SQLAlchemy silently drops dialect-foreign ``*_where``
kwargs, and a partial unique index that loses its predicate on PostgreSQL
degrades into a table-wide unique constraint (C1:
``uq_wallet_transactions_terminal_round`` blocked every wallet settlement).

This check is pure AST analysis — it runs everywhere (no PostgreSQL needed),
so even a CI run without the PG fixture catches the class of defect the
schema-level rehearsal missed.
"""

from __future__ import annotations

import ast
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations" / "versions"

# 022 shipped with only sqlite_where on this index; DB-04's No-Go forbids
# editing published revisions, so the fix landed append-only in
# 025_postgres_runtime_compatibility (PG-only drop/recreate with
# postgresql_where). This exemption must not grow: new migrations always
# declare both dialect predicates.
LEGACY_EXEMPTIONS: dict[str, set[str]] = {
    "022_internal_billing.py": {"uq_wallet_transactions_terminal_round"},
}


def _iter_create_index_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name == "create_index":
            yield node


def _first_str_arg(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def test_every_sqlite_where_index_also_declares_postgresql_where() -> None:
    migration_files = sorted(MIGRATIONS_DIR.glob("*.py"))
    assert migration_files, "no migration files found"

    violations: list[str] = []
    checked = 0
    for path in migration_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _iter_create_index_calls(tree):
            keywords = {kw.arg for kw in call.keywords if kw.arg}
            if "sqlite_where" not in keywords:
                continue
            checked += 1
            index_name = _first_str_arg(call) or "<dynamic>"
            if "postgresql_where" not in keywords:
                if index_name in LEGACY_EXEMPTIONS.get(path.name, set()):
                    continue
                violations.append(f"{path.name}: index {index_name!r}")
    assert checked >= 6, f"expected at least 6 sqlite_where indexes, checked {checked}"
    assert not violations, (
        "create_index with sqlite_where but no postgresql_where "
        "(predicate silently dropped on PG): " + "; ".join(violations)
    )


def test_saved_prompt_author_foreign_key_restricts_deletion() -> None:
    migration = (MIGRATIONS_DIR / "061_saved_prompt_metadata.py").read_text(encoding="utf-8")

    assert 'ondelete="RESTRICT"' in migration
    assert 'ondelete="SET NULL"' not in migration
