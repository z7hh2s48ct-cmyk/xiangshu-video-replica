"""CW-054 segment 1/N — the PG lane's portable query/row/exception contract.

TEST-PG. Every case here runs against a real PostgreSQL database
(``cw054_contract_test``, registered in ``pg_test_kit.RECORDED_TEST_DATABASES``)
reached through ``TEST_POSTGRESQL_URL``; nothing is mocked and nothing falls
back to SQLite. The suite never touches the shared full-suite database, so it
cannot collide with another suite's fixture (PG-05 独立专项不互删).

What it pins down, per PG-02 (查询与类型) and the CW-054 acceptance floor:

- ``executemany`` — a non-empty batch persists **every** parameter set and
  reports ``rowcount`` equal to the batch size; an empty batch writes nothing.
  The PG lane used to swallow the call entirely, i.e. a batch write persisted
  zero rows and no assertion could tell.
- ``iterdump`` — yields one INSERT per stored row with the real column values,
  so a sensitive-data check reads actual PG results instead of traversing an
  empty iterator and passing vacuously.
- ``set_trace_callback`` — the callback fires for statements actually sent to
  the engine, and does **not** fire for the ``BEGIN IMMEDIATE`` / ``PRAGMA``
  no-ops the PG lane swallows.
- row access — by position, by name, ``dict(row)``, ``keys()``, and
  ``IndexError`` for an unknown column name.
- type roundtrip — 金额 NUMERIC/BIGINT、布尔、NULL、UTC timestamptz、JSONB.
- constraint mapping — UNIQUE / FK / CHECK / NOT NULL each surface as one
  portable error carrying its SQLSTATE, without closing or rolling back the
  connection.

Boundary (CW-055): ``commit`` / ``rollback`` / ``close`` / ``transaction``
keep their deliberate PG no-op semantics and are asserted unchanged at the end
of this file. CW-054 translates the exception *type* only — it never recovers
the transaction, because commit authority belongs to the outer fenced
transaction. Where a case needs the failed transaction cleared, the test rolls
back on the raw connection it owns rather than through the facade.

``sqlite3`` is imported for its *exception base class* only: the contract being
verified is that one PG failure is catchable as either lane's IntegrityError.
No ``sqlite3.connect`` / ``SQLiteBackend`` / ``DB_PATH`` / ``:memory:`` appears
here; the row-parity comparison against a real ``sqlite3.Row`` lives in
``test_db_portable.py`` (TEST-LOGIC), which is the classified home for it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from psycopg.types.json import Jsonb

from app.db_portable import BusinessConnection, IntegrityConstraintError
from tests.pg_test_kit import (
    create_test_database,
    drop_test_database,
    require_pg_or_explicit_skip,
    upgrade_test_database_to_head,
)

CW054_TEST_DB = "cw054_contract_test"

# SQLSTATEs the mapping contract is defined against (PG-02 约束异常映射).
UNIQUE_VIOLATION = "23505"
FK_VIOLATION = "23503"
CHECK_VIOLATION = "23514"
NOT_NULL_VIOLATION = "23502"


@pytest.fixture(scope="module")
def cw054_pg_dsn() -> Iterator[str]:
    """A dedicated migrated database for the CW-054 contract suite.

    Created through the CW-007 kit helpers so ``assert_safe_test_database``
    guards the ``DROP ... WITH (FORCE)`` against the allowlisted name and the
    kit resolves the admin DSN — no hand-rolled DSN concatenation lives here.
    """
    require_pg_or_explicit_skip()
    dsn = create_test_database(CW054_TEST_DB)
    upgrade_test_database_to_head(dsn)
    try:
        yield dsn
    finally:
        drop_test_database(CW054_TEST_DB)


@pytest.fixture()
def pg_business(cw054_pg_dsn: str) -> Iterator[BusinessConnection]:
    """A facade on the PG lane over freshly truncated contract tables."""
    conn = psycopg.connect(cw054_pg_dsn)
    conn.autocommit = False
    conn.execute("CREATE TABLE IF NOT EXISTS cw054_parent (id TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cw054_child ("
        "id TEXT PRIMARY KEY, "
        "parent_id TEXT NOT NULL REFERENCES cw054_parent(id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cw054_contract ("
        "id TEXT PRIMARY KEY, "
        "owner TEXT NOT NULL, "
        "amount NUMERIC(12, 2), "
        "cents BIGINT, "
        "flag BOOLEAN, "
        "note TEXT, "
        "payload JSONB, "
        "created_at TIMESTAMPTZ, "
        "CONSTRAINT cw054_owner_check CHECK (owner <> ''))"
    )
    conn.execute("TRUNCATE cw054_contract, cw054_child, cw054_parent CASCADE")
    conn.commit()
    try:
        yield BusinessConnection.postgres(conn)
    finally:
        # The facade's rollback()/close() are deliberate PG no-ops (CW-055),
        # so the test — which owns this transaction — tears it down on the raw
        # connection. Anything left uncommitted is discarded here.
        conn.rollback()
        conn.close()


def _seed(biz: BusinessConnection, rows: list[tuple[str, str]]) -> None:
    """Insert (id, owner) pairs one statement at a time, inside the open tx."""
    for row_id, owner in rows:
        biz.execute("INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", (row_id, owner))


def _count(biz: BusinessConnection, table: str = "cw054_contract") -> int:
    row = biz.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


# ---------------------------------------------------------------------------
# executemany — a real batch write with a real rowcount
# ---------------------------------------------------------------------------


class TestExecutemanyPGLane:
    """PG-02: 批量写入须真实落库，不能用空操作伪通过."""

    def test_non_empty_batch_persists_every_parameter_set(
        self, pg_business: BusinessConnection
    ) -> None:
        rows = [
            ("a1", "owner1", Decimal("10.50"), True, "note1"),
            ("a2", "owner2", Decimal("20.75"), False, "note2"),
            ("a3", "owner3", None, None, None),
        ]
        cur = pg_business.executemany(
            "INSERT INTO cw054_contract (id, owner, amount, flag, note) "
            "VALUES (%s, %s, %s, %s, %s)",
            rows,
        )
        # CW-054 增量验收: 非空 executemany 写入行数必须等于参数组数.
        assert cur.rowcount == len(rows)
        assert _count(pg_business) == len(rows)

    def test_rowcount_equals_batch_size_for_a_larger_batch(
        self, pg_business: BusinessConnection
    ) -> None:
        rows = [(f"b{i}", f"owner{i}") for i in range(25)]
        cur = pg_business.executemany(
            "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", rows
        )
        assert cur.rowcount == 25
        assert _count(pg_business) == 25

    def test_empty_batch_reports_zero_and_writes_nothing(
        self, pg_business: BusinessConnection
    ) -> None:
        cur = pg_business.executemany("INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", [])
        assert cur.rowcount == 0
        assert _count(pg_business) == 0

    def test_returned_cursor_exposes_rowcount_like_sqlite3(
        self, pg_business: BusinessConnection
    ) -> None:
        """``sqlite3.Connection.executemany`` returns a Cursor whose rowcount is
        the number of parameter sets. The PG lane must match that shape so a
        caller cannot tell which lane ran the batch."""
        cur = pg_business.executemany(
            "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)",
            [("c1", "o1")],
        )
        assert hasattr(cur, "rowcount")
        assert cur.rowcount == 1

    def test_batch_violating_a_constraint_maps_and_persists_nothing(
        self, pg_business: BusinessConnection
    ) -> None:
        """A constraint failure inside the batch surfaces as the portable error,
        and the half-applied batch leaves nothing behind.

        The seed is committed first so the surviving row is genuinely
        pre-existing. The facade's ``commit()`` is a deliberate PG no-op (CW-055),
        so the test commits on the raw connection it owns; an *uncommitted* seed
        would be discarded by the same rollback and the assertion would then pass
        at 0 rows instead of proving the partial batch was undone.
        """
        _seed(pg_business, [("d1", "o1")])
        pg_business.raw.commit()
        with pytest.raises(IntegrityConstraintError) as exc_info:
            pg_business.executemany(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)",
                [("d2", "o2"), ("d1", "dup")],
            )
        assert exc_info.value.sqlstate == UNIQUE_VIOLATION
        pg_business.raw.rollback()
        assert _count(pg_business) == 1, "only the pre-existing row survives"


# ---------------------------------------------------------------------------
# rowcount / RETURNING on ordinary statements
# ---------------------------------------------------------------------------


class TestRowcountAndReturningPGLane:
    """PG-02: rowcount / RETURNING 核验."""

    def test_update_rowcount_counts_affected_rows(self, pg_business: BusinessConnection) -> None:
        _seed(pg_business, [("e1", "o1"), ("e2", "o2"), ("e3", "o3")])
        cur = pg_business.execute(
            "UPDATE cw054_contract SET owner = %s WHERE owner <> %s", ("renamed", "o2")
        )
        assert cur.rowcount == 2

    def test_delete_rowcount_is_zero_when_nothing_matches(
        self, pg_business: BusinessConnection
    ) -> None:
        cur = pg_business.execute("DELETE FROM cw054_contract WHERE id = %s", ("absent",))
        assert cur.rowcount == 0

    def test_insert_returning_yields_the_new_row(self, pg_business: BusinessConnection) -> None:
        cur = pg_business.execute(
            "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s) RETURNING id, owner",
            ("f1", "owner-f1"),
        )
        assert cur.rowcount == 1
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "f1"
        assert row["owner"] == "owner-f1"


# ---------------------------------------------------------------------------
# query text — the %% escaping convention on the PG lane
# ---------------------------------------------------------------------------


class TestLiteralPercentInQueryText:
    """PG-02 查询契约: a literal ``%`` in the SQL text is a placeholder introducer.

    ``PostgresBackend.execute`` always hands psycopg a parameter sequence (the
    facade default is ``()``), and psycopg parses the query for placeholders
    whenever one is given — so a bare ``%`` is rejected client-side and never
    reaches the server. Every migrated service already spells a literal percent
    as ``%%`` (``materials.py``'s ``LIKE 'image/%%'``, ``rbac_routes.py``'s
    ``LIKE '%%/projects/…'``), and in a LIKE pattern two wildcards mean the same
    as one, so the desktop lane reads the identical text correctly.

    Pinned here because the failure is asymmetric: the SQLite lane accepts the
    bare ``%`` and the customer lane raises, so the defect can only ever appear
    in production. It is also why ``execute`` must *not* be "fixed" by omitting
    an empty parameter sequence — that would send the ``%%`` through verbatim
    and silently turn those LIKE patterns into a match on a literal percent.
    """

    def test_escaped_percent_matches_as_a_wildcard(self, pg_business: BusinessConnection) -> None:
        _seed(pg_business, [("p1", "o1"), ("q2", "o2")])
        rows = pg_business.execute(
            "SELECT id FROM cw054_contract WHERE id LIKE 'p%%' ORDER BY id"
        ).fetchall()
        assert [row["id"] for row in rows] == ["p1"]

    def test_bare_percent_is_rejected_before_reaching_the_server(
        self, pg_business: BusinessConnection
    ) -> None:
        """The trap itself, so a caller who writes ``LIKE 'p%'`` sees it here
        rather than as an unexplained 500 on the customer lane."""
        with pytest.raises(psycopg.ProgrammingError, match="placeholders"):
            pg_business.execute("SELECT id FROM cw054_contract WHERE id LIKE 'p%'")


# ---------------------------------------------------------------------------
# iterdump — real PG results, never an empty iterator
# ---------------------------------------------------------------------------


class TestIterdumpPGLane:
    """CW-054 底线: 敏感数据检查读取实际PG结果，不能遍历空dump假通过."""

    def test_yields_one_insert_per_stored_row(self, pg_business: BusinessConnection) -> None:
        _seed(pg_business, [("g1", "o1"), ("g2", "o2"), ("g3", "o3")])
        lines = [line for line in pg_business.iterdump() if '"cw054_contract"' in line]
        assert len(lines) == 3, f"expected 3 INSERTs for cw054_contract, got {lines}"
        assert all(line.startswith("INSERT INTO ") for line in lines)

    def test_dump_exposes_the_actual_column_values(self, pg_business: BusinessConnection) -> None:
        """The sensitive-data check traverses the dump for stored secrets, so
        the real values must be present in it."""
        pg_business.execute(
            "INSERT INTO cw054_contract (id, owner, note) VALUES (%s, %s, %s)",
            ("g4", "owner-g4", "SUPER-SECRET-TOKEN-9f3c"),
        )
        dump_text = "\n".join(pg_business.iterdump())
        assert "g4" in dump_text
        assert "SUPER-SECRET-TOKEN-9f3c" in dump_text
        assert "owner-g4" in dump_text

    def test_jsonb_is_rendered_as_valid_json_not_a_python_repr(
        self, pg_business: BusinessConnection
    ) -> None:
        payload = {"token": "abc", "nested": {"n": 1}, "list": [1, 2, 3]}
        pg_business.execute(
            "INSERT INTO cw054_contract (id, owner, payload) VALUES (%s, %s, %s)",
            ("g5", "o5", Jsonb(payload)),
        )
        line = next(line for line in pg_business.iterdump() if "g5" in line)
        # The dump must carry real JSON — keys sorted so the output is
        # deterministic across runs (CW-007 repeatability) — not a Python repr.
        expected_literal = "'" + json.dumps(payload, sort_keys=True) + "'"
        assert expected_literal in line, f"JSONB not rendered faithfully: {line}"
        assert "{'token'" not in line, "Python repr leaked into the dump"
        # And what it renders must parse back to the document that is stored.
        match = re.search(r"'(\{.*\})'", line)
        assert match is not None, f"no JSON literal in the dump line: {line}"
        assert json.loads(match.group(1)) == payload

    def test_null_boolean_and_numeric_render_as_sql_literals(
        self, pg_business: BusinessConnection
    ) -> None:
        pg_business.execute(
            "INSERT INTO cw054_contract (id, owner, amount, cents, flag, note) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("g6", "o6", Decimal("19.99"), 1500, True, None),
        )
        line = next(line for line in pg_business.iterdump() if "g6" in line)
        assert "NULL" in line, f"NULL not rendered as a SQL literal: {line}"
        assert "TRUE" in line, f"boolean not rendered as a SQL literal: {line}"
        assert "19.99" in line, f"NUMERIC value missing: {line}"
        assert "'19.99'" not in line, "NUMERIC was quoted as a string"
        assert "1500" in line, f"BIGINT value missing: {line}"

    def test_empty_table_contributes_no_insert(self, pg_business: BusinessConnection) -> None:
        dump_text = "\n".join(pg_business.iterdump())
        assert '"cw054_contract"' not in dump_text

    def test_is_a_real_iterator_and_repeatable(self, pg_business: BusinessConnection) -> None:
        """PG-05 连续2次可重复: two traversals of the same state agree, and the
        result is a live iterator rather than a materialised empty stub."""
        _seed(pg_business, [("g7", "o7")])
        stream = pg_business.iterdump()
        assert isinstance(stream, Iterator)
        first_line = next(stream)
        assert isinstance(first_line, str)
        first = [first_line, *list(stream)]
        second = list(pg_business.iterdump())
        assert len(second) > 0
        assert first == second


# ---------------------------------------------------------------------------
# set_trace_callback — statement tracing on the PG lane
# ---------------------------------------------------------------------------


class TestSetTraceCallbackPGLane:
    """Test statement counters must not read zero on the customer lane."""

    def test_callback_receives_each_executed_statement(
        self, pg_business: BusinessConnection
    ) -> None:
        statements: list[str] = []
        pg_business.set_trace_callback(statements.append)
        pg_business.execute("SELECT 1")
        pg_business.execute("SELECT 2")
        assert statements == ["SELECT 1", "SELECT 2"]

    def test_setting_none_disables_tracing(self, pg_business: BusinessConnection) -> None:
        statements: list[str] = []
        pg_business.set_trace_callback(statements.append)
        pg_business.execute("SELECT 1")
        pg_business.set_trace_callback(None)
        pg_business.execute("SELECT 2")
        assert statements == ["SELECT 1"]

    def test_swallowed_sqlite_only_statements_are_not_traced(
        self, pg_business: BusinessConnection
    ) -> None:
        """sqlite3's hook only sees statements actually sent to the engine.
        BEGIN IMMEDIATE and PRAGMA are no-ops on the PG lane, so tracing them
        would charge a counter with work that never happened."""
        statements: list[str] = []
        pg_business.set_trace_callback(statements.append)
        pg_business.execute("BEGIN IMMEDIATE")
        pg_business.execute("PRAGMA busy_timeout = 5000")
        assert statements == []
        pg_business.execute("SELECT 1")
        assert statements == ["SELECT 1"]

    def test_executemany_is_traced_once_for_the_batch(
        self, pg_business: BusinessConnection
    ) -> None:
        """Documented deviation: sqlite3 fires per parameter set, the PG lane
        fires once because psycopg3 sends the batch as a single command."""
        statements: list[str] = []
        pg_business.set_trace_callback(statements.append)
        sql = "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)"
        pg_business.executemany(sql, [("h1", "o1"), ("h2", "o2")])
        assert statements == [sql]


# ---------------------------------------------------------------------------
# row access on rows delivered by a real PG cursor
# ---------------------------------------------------------------------------


class TestRowAccessPGLane:
    """CW-054 底线: row按名称/位置及dict转换符合使用方."""

    def test_positional_and_named_access_agree(self, pg_business: BusinessConnection) -> None:
        pg_business.execute(
            "INSERT INTO cw054_contract (id, owner, amount) VALUES (%s, %s, %s)",
            ("i1", "owner-i1", Decimal("3.14")),
        )
        row = pg_business.execute(
            "SELECT id, owner, amount FROM cw054_contract WHERE id = %s", ("i1",)
        ).fetchone()
        assert row is not None
        assert row[0] == row["id"] == "i1"
        assert row[1] == row["owner"] == "owner-i1"
        assert row[2] == row["amount"] == Decimal("3.14")

    def test_dict_conversion_produces_a_column_mapping(
        self, pg_business: BusinessConnection
    ) -> None:
        """app/viral_store.py and app/wallet_routes.py both do ``dict(row)`` /
        ``Model(**dict(row))`` in production, so this must work on the PG lane."""
        pg_business.execute(
            "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("i2", "owner-i2")
        )
        row = pg_business.execute(
            "SELECT id, owner FROM cw054_contract WHERE id = %s", ("i2",)
        ).fetchone()
        assert row is not None
        assert dict(row) == {"id": "i2", "owner": "owner-i2"}

    def test_keys_membership_pattern_used_by_callers(self, pg_business: BusinessConnection) -> None:
        """rbac_routes.py guards optional columns with ``x not in row.keys()``."""
        row = pg_business.execute(
            "SELECT id, owner FROM cw054_contract WHERE id = %s", ("absent",)
        ).fetchone()
        assert row is None
        present = pg_business.execute("SELECT 1 AS one, 2 AS two").fetchone()
        assert present is not None
        assert present.keys() == ["one", "two"]
        assert "one" in present.keys()
        assert "three" not in present.keys()

    def test_iteration_and_len_are_tuple_shaped(self, pg_business: BusinessConnection) -> None:
        row = pg_business.execute("SELECT 1 AS a, 'x' AS b, NULL AS c").fetchone()
        assert row is not None
        assert list(row) == [1, "x", None]
        assert len(row) == 3
        first, second, third = row
        assert (first, second, third) == (1, "x", None)

    def test_unknown_column_name_raises_index_error(self, pg_business: BusinessConnection) -> None:
        """sqlite3.Row raises IndexError for an unknown name; the PG lane used
        to leak tuple.index's ValueError, so a caller guarding an optional
        column with ``except IndexError`` broke only in production."""
        row = pg_business.execute("SELECT 1 AS a").fetchone()
        assert row is not None
        with pytest.raises(IndexError):
            _ = row["nope"]

    def test_out_of_range_position_raises_index_error(
        self, pg_business: BusinessConnection
    ) -> None:
        row = pg_business.execute("SELECT 1 AS a").fetchone()
        assert row is not None
        with pytest.raises(IndexError):
            _ = row[99]

    def test_column_name_lookup_folds_case_both_ways(self, pg_business: BusinessConnection) -> None:
        """The identifier-folding gap only a real PG server can show.

        PostgreSQL lower-cases an *unquoted* identifier, so ``SELECT 1 AS
        ownerUserId`` describes the column as ``owneruserid`` while the desktop
        lane keeps ``ownerUserId``; a *quoted* identifier keeps its case the
        other way round. ``sqlite3.Row`` resolves a name case-insensitively, so
        ``_NamedRow`` must too or the same ``row["..."]`` expression works on
        one lane and raises on the other.
        """
        unquoted = pg_business.execute("SELECT 1 AS ownerUserId").fetchone()
        assert unquoted is not None
        # PG folded the alias: the description really is lower case.
        assert unquoted.keys() == ["owneruserid"]
        assert unquoted["owneruserid"] == 1
        assert unquoted["ownerUserId"] == 1
        assert unquoted["OWNERUSERID"] == 1

        quoted = pg_business.execute('SELECT 2 AS "ownerUserId"').fetchone()
        assert quoted is not None
        # A quoted alias keeps its case, so now the lower-case spelling is the
        # one that needs folding.
        assert quoted.keys() == ["ownerUserId"]
        assert quoted["ownerUserId"] == 2
        assert quoted["owneruserid"] == 2

    def test_non_index_key_raises_index_error_not_type_error(
        self, pg_business: BusinessConnection
    ) -> None:
        """sqlite3.Row rejects a float/bytes/None key with IndexError; a bare
        tuple subscript would raise TypeError instead."""
        row = pg_business.execute("SELECT 1 AS a").fetchone()
        assert row is not None
        for bad_key in (1.0, b"a", None):
            with pytest.raises(IndexError):
                _ = row[bad_key]


# ---------------------------------------------------------------------------
# type roundtrip — 金额 / 布尔 / NULL / UTC / JSON
# ---------------------------------------------------------------------------


class TestTypeRoundtripPGLane:
    """CW-054 底线: 金额/布尔/NULL/UTC/JSON roundtrip正确."""

    def _roundtrip(self, biz: BusinessConnection, column: str, value: object) -> object:
        biz.execute(
            f"INSERT INTO cw054_contract (id, owner, {column}) VALUES (%s, %s, %s)",
            (f"rt-{column}", "o", value),
        )
        row = biz.execute(
            f"SELECT {column} FROM cw054_contract WHERE id = %s", (f"rt-{column}",)
        ).fetchone()
        assert row is not None
        return row[0]

    def test_numeric_amount_roundtrips_as_decimal(self, pg_business: BusinessConnection) -> None:
        amount = Decimal("12345.67")
        got = self._roundtrip(pg_business, "amount", amount)
        assert got == amount
        assert isinstance(got, Decimal), f"金额 lost its type: {type(got)}"

    def test_bigint_cents_roundtrips_as_int(self, pg_business: BusinessConnection) -> None:
        got = self._roundtrip(pg_business, "cents", 1500)
        assert got == 1500
        assert isinstance(got, int) and not isinstance(got, bool)

    def test_boolean_roundtrips_identity(self, pg_business: BusinessConnection) -> None:
        pg_business.execute(
            "INSERT INTO cw054_contract (id, owner, flag) VALUES (%s, %s, %s)",
            ("rt-true", "o", True),
        )
        pg_business.execute(
            "INSERT INTO cw054_contract (id, owner, flag) VALUES (%s, %s, %s)",
            ("rt-false", "o", False),
        )
        rows = dict(
            (r["id"], r["flag"])
            for r in pg_business.execute(
                "SELECT id, flag FROM cw054_contract WHERE id LIKE 'rt-%%'"
            ).fetchall()
        )
        assert rows["rt-true"] is True
        assert rows["rt-false"] is False

    def test_null_roundtrips_as_none(self, pg_business: BusinessConnection) -> None:
        assert self._roundtrip(pg_business, "note", None) is None

    def test_utc_datetime_roundtrips_timezone_aware(self, pg_business: BusinessConnection) -> None:
        moment = datetime(2026, 9, 10, 12, 30, 45, tzinfo=UTC)
        got = self._roundtrip(pg_business, "created_at", moment)
        # Assert the driver really handed back a datetime: a TIMESTAMPTZ column
        # read as text could compare equal to an ISO-8601 string and hide the
        # lost offset.
        assert isinstance(got, datetime)
        assert got == moment
        assert got.tzinfo is not None, "UTC offset lost in roundtrip"

    def test_jsonb_roundtrips_through_the_jsonb_wrapper(
        self, pg_business: BusinessConnection
    ) -> None:
        payload = {"key": "value", "nested": {"a": 1, "b": [2, 3]}}
        got = self._roundtrip(pg_business, "payload", Jsonb(payload))
        assert got == payload
        assert isinstance(got, dict)

    def test_jsonb_roundtrips_from_pre_serialized_text(
        self, pg_business: BusinessConnection
    ) -> None:
        """The shape the migrated services still write: a JSON *string* into a
        JSONB column. psycopg3 casts it, and reads it back as a dict."""
        payload = {"pre": "serialized", "n": 7}
        got = self._roundtrip(pg_business, "payload", json.dumps(payload))
        assert got == payload

    def test_bare_dict_parameter_is_not_adaptable(self, pg_business: BusinessConnection) -> None:
        """Documents the migration constraint CW-058/059 must clear: a bare
        dict has no psycopg3 adapter, so callers must wrap it in ``Jsonb`` or
        pre-serialize. This is a real divergence from the SQLite lane, which
        accepted the dict-shaped value as opaque text."""
        with pytest.raises(psycopg.ProgrammingError, match="cannot adapt type"):
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner, payload) VALUES (%s, %s, %s)",
                ("rt-bare", "o", {"bare": "dict"}),
            )

    def test_unicode_and_quotes_roundtrip_verbatim(self, pg_business: BusinessConnection) -> None:
        note = "庭院项目 O'Brien — 乡墅管理专家 🏡"
        assert self._roundtrip(pg_business, "note", note) == note


# ---------------------------------------------------------------------------
# constraint exception mapping — UNIQUE / FK / CHECK / NOT NULL
# ---------------------------------------------------------------------------


class TestConstraintExceptionMappingPGLane:
    """CW-054 底线: 唯一/FK/check失败映射到既定业务结果且不污染连接."""

    def test_unique_violation(self, pg_business: BusinessConnection) -> None:
        _seed(pg_business, [("k1", "o1")])
        with pytest.raises(IntegrityConstraintError) as exc_info:
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("k1", "o2")
            )
        assert exc_info.value.sqlstate == UNIQUE_VIOLATION

    def test_foreign_key_violation(self, pg_business: BusinessConnection) -> None:
        with pytest.raises(IntegrityConstraintError) as exc_info:
            pg_business.execute(
                "INSERT INTO cw054_child (id, parent_id) VALUES (%s, %s)",
                ("child1", "missing-parent"),
            )
        assert exc_info.value.sqlstate == FK_VIOLATION
        assert exc_info.value.constraint_name == "cw054_child_parent_id_fkey"

    def test_check_violation(self, pg_business: BusinessConnection) -> None:
        with pytest.raises(IntegrityConstraintError) as exc_info:
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("k2", "")
            )
        assert exc_info.value.sqlstate == CHECK_VIOLATION
        assert exc_info.value.constraint_name == "cw054_owner_check"

    def test_not_null_violation(self, pg_business: BusinessConnection) -> None:
        with pytest.raises(IntegrityConstraintError) as exc_info:
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("k3", None)
            )
        assert exc_info.value.sqlstate == NOT_NULL_VIOLATION

    def test_mapped_error_is_catchable_as_the_pg_lanes_integrity_error(
        self, pg_business: BusinessConnection
    ) -> None:
        """CW-042-b: the dual inheritance (sqlite3 + psycopg) was transitional
        and is retired with the SQLite lane — the mapped error is now a pure
        ``psycopg.IntegrityError``, so every existing
        ``except psycopg.IntegrityError`` handler keeps working unchanged."""
        with pytest.raises(psycopg.IntegrityError):
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("k4", "")
            )
        pg_business.raw.rollback()
        with pytest.raises(IntegrityConstraintError):
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("k6", "")
            )

    def test_original_psycopg_error_stays_reachable_as_cause(
        self, pg_business: BusinessConnection
    ) -> None:
        with pytest.raises(IntegrityConstraintError) as exc_info:
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("k7", "")
            )
        cause = exc_info.value.__cause__
        assert isinstance(cause, psycopg.errors.CheckViolation)
        assert cause.sqlstate == CHECK_VIOLATION

    def test_mapping_does_not_close_or_recover_the_connection(
        self, pg_business: BusinessConnection
    ) -> None:
        """Boundary guard for CW-055: CW-054 translates the exception type and
        nothing else. The connection is left in PG's INERROR state — the fenced
        transaction still owns the decision to roll back or discard it. Had
        CW-054 auto-rolled-back here, it would have destroyed fenced state."""
        raw = pg_business.raw
        assert isinstance(raw, psycopg.Connection)
        with pytest.raises(IntegrityConstraintError):
            pg_business.execute(
                "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)", ("k8", "")
            )
        assert raw.closed is False, "CW-054 must not close the connection"
        assert raw.info.transaction_status == psycopg.pq.TransactionStatus.INERROR, (
            "CW-054 must not roll back the fenced transaction"
        )

    def test_connection_is_reusable_once_the_owner_clears_the_transaction(
        self, pg_business: BusinessConnection
    ) -> None:
        """CW-054 增量验收: 失败后连接可复用且错误映射稳定. The test owns this
        transaction, so it performs the rollback itself — through the raw
        connection, never through the facade's CW-055 no-op."""
        raw = pg_business.raw
        assert isinstance(raw, psycopg.Connection)
        for attempt in range(2):
            with pytest.raises(IntegrityConstraintError) as exc_info:
                pg_business.execute(
                    "INSERT INTO cw054_contract (id, owner) VALUES (%s, %s)",
                    ("k9", ""),
                )
            # 错误映射稳定: the same SQLSTATE every time.
            assert exc_info.value.sqlstate == CHECK_VIOLATION
            raw.rollback()
            assert raw.info.transaction_status != psycopg.pq.TransactionStatus.INERROR
        _seed(pg_business, [("k10", "valid_owner")])
        assert _count(pg_business) == 1, "connection unusable after a violation"


# ---------------------------------------------------------------------------
# boundary guards — CW-055 semantics must be untouched by this task
# ---------------------------------------------------------------------------


class TestCW055BoundaryUnchanged:
    """CW-054 must not turn a no-op commit into a mid-flight business commit."""

    def test_facade_commit_does_not_persist_on_the_pg_lane(
        self, cw054_pg_dsn: str, pg_business: BusinessConnection
    ) -> None:
        _seed(pg_business, [("m1", "o1")])
        pg_business.commit()  # deliberate no-op on PG
        assert _count(pg_business) == 1, "visible inside the still-open transaction"
        with psycopg.connect(cw054_pg_dsn) as other:
            row = other.execute(
                "SELECT count(*) FROM cw054_contract WHERE id = %s", ("m1",)
            ).fetchone()
        assert row is not None
        assert row[0] == 0, "facade commit must not reach the database (CW-055)"

    def test_facade_rollback_does_not_discard_on_the_pg_lane(
        self, pg_business: BusinessConnection
    ) -> None:
        _seed(pg_business, [("m2", "o2")])
        pg_business.rollback()  # deliberate no-op on PG
        assert _count(pg_business) == 1

    def test_facade_close_leaves_the_pg_connection_open(
        self, pg_business: BusinessConnection
    ) -> None:
        raw = pg_business.raw
        assert isinstance(raw, psycopg.Connection)
        pg_business.close()  # deliberate no-op on PG
        assert raw.closed is False
        assert _count(pg_business) == 0

    def test_facade_transaction_context_manager_does_not_begin_or_commit(
        self, cw054_pg_dsn: str, pg_business: BusinessConnection
    ) -> None:
        with pg_business.transaction() as tx:
            _seed(tx, [("m3", "o3")])
        with psycopg.connect(cw054_pg_dsn) as other:
            row = other.execute(
                "SELECT count(*) FROM cw054_contract WHERE id = %s", ("m3",)
            ).fetchone()
        assert row is not None
        assert row[0] == 0, "transaction() must not commit on the PG lane (CW-055)"
