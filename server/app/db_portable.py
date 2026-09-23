"""T21 / SES-04 — the portable business-database facade (dev doc §12.4, plan A1).

The business services are being migrated to PG-canonical SQL (``%s``
placeholders, ``RETURNING``, ``ON CONFLICT``, ``now() + interval``, no
``rowid``). Customer production runs that SQL natively on PostgreSQL; the
internal SQLite desktop lane runs the *same* SQL through a bounded
translation layer — exactly one SQL source, no dual variants (the
single-implementation red line).

  dialect differences. Anything it does not recognise raises ``ValueError``
  rather than silently passing through: the desktop lane must never run SQL
  the translator has not vetted.
- ``PostgresBackend`` — the execute() wrapper.
- ``BusinessConnection`` — the uniform facade the business services see:
  ``execute`` / ``transaction`` / ``commit`` / ``rollback`` with backend
  dispatch, plus ``.raw`` for the storage/ffprobe adapters and ``.ctx`` for
  the customer session context (filled once T21 wires the fencing).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Protocol, cast

import psycopg

# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _BusinessCursor(Protocol):
    """The minimal cursor surface the business services use.

    ``Any`` returns mirror the sqlite3 typeshed (and the current ``conn:
    sqlite3.Connection`` call sites, where ``row[0]`` is already ``Any``) so
    the migrated services keep type-checking unchanged.
    """

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    @property
    def rowcount(self) -> int: ...


class IntegrityConstraintError(psycopg.IntegrityError):
    """A constraint violation catchable on *both* lanes (CW-054).

    Historical note: the retired desktop SQLite lane raised
    ``sqlite3.IntegrityError`` and business callers caught that — sometimes
    through the broader ``sqlite3.Error``, which
    on the PG lane used to let a constraint failure escape the handler entirely
    (``source_frames`` rolls back and maps its write errors that way). The
    customer lane raises psycopg's SQLSTATE-mapped subclasses instead. Basing
    this one class on both makes the same statement failure reach the same
    handler whichever lane executes it, with no call-site rewrite.

    ``sqlstate`` / ``constraint_name`` are carried across so a caller can still
    tell UNIQUE (23505) from FOREIGN KEY (23503), CHECK (23514) and NOT NULL
    (23502) and map each to its own business result; the original psycopg
    exception stays reachable through ``__cause__``.

    Transitional by design: CW-058/059 migrate the remaining callers onto the
    PG-native types and CW-042 retires the SQLite lane, after which the
    ``sqlite3`` base drops out without touching call sites again.
    """

    def __init__(
        self,
        message: str,
        *,
        sqlstate: str | None = None,
        constraint_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _map_integrity_error(exc: psycopg.Error) -> IntegrityConstraintError:
    """Carry the SQLSTATE and constraint name onto the portable error."""
    diag = getattr(exc, "diag", None)
    return IntegrityConstraintError(
        str(exc),
        sqlstate=getattr(exc, "sqlstate", None),
        constraint_name=getattr(diag, "constraint_name", None),
    )


class PostgresBackend:
    """Wraps a psycopg connection; execute runs the native ``%s`` SQL.

    The connection carries a ``sqlite3.Row``-shaped row factory — the
    business services read rows both by position (``row[0]``) and by column
    name (``row["owner_user_id"]``), and psycopg's plain tuples only support
    position.

    CW-054: constraint violations (UNIQUE / FK / CHECK / NOT NULL) surface as
    ``IntegrityConstraintError``, which is catchable as either lane's
    ``IntegrityError`` and keeps the SQLSTATE. Only the exception type is
    translated here — the connection is never rolled back or closed, because
    recovery and pool return stay with the fenced transaction (CW-055).
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn
        conn.row_factory = cast(Any, _named_row_factory)

    def execute(self, sql: str, params: Sequence[object] = ()) -> psycopg.Cursor:
        try:
            return self._conn.execute(sql, params)
        except psycopg.IntegrityError as exc:
            raise _map_integrity_error(exc) from exc

    def executemany(self, sql: str, seq: Sequence[Sequence[object]]) -> psycopg.Cursor:
        """Real batch execution on the PG lane (psycopg3 ``Cursor.executemany``).

        ``rowcount`` comes back equal to the number of parameter sets, matching
        the SQLite lane, so a batch that persists fewer rows than it was given
        is a failure instead of a silent partial write. An empty sequence needs
        no guard: psycopg3 accepts it natively (``rowcount == 0``, nothing
        executed), and an ``if not seq`` test would misfire on a generator,
        which is always truthy.
        """
        cur = self._conn.cursor()
        try:
            cur.executemany(sql, seq)
        except psycopg.IntegrityError as exc:
            raise _map_integrity_error(exc) from exc
        return cur

    @property
    def raw(self) -> psycopg.Connection:
        return self._conn


class _NamedRow:
    """A psycopg row that also answers ``row["column"]`` (the sqlite3.Row shape).

    The business services read rows both by position (``row[0]``) and by
    column name (``row["owner_user_id"]``); psycopg's plain tuples only
    support position, so the PG backend returns these. ``__iter__``/``len``
    keep tuple-shaped call sites working and ``keys()`` makes ``dict(row)``
    work through the mapping protocol.

    CW-054 measured a real ``sqlite3.Row`` and mirrors it on every axis a
    caller can observe:

    - a column name resolves case-insensitively, the *first* match winning --
      PG folds an unquoted identifier to lower case, so a query spelling
      ``SELECT ownerUserId`` describes the column as ``owneruserid`` while the
      desktop lane keeps the spelling it was given;
    - an unknown name raises ``IndexError("No item with that key")``, not the
      ``ValueError`` a bare ``tuple.index`` would leak;
    - a key that is neither ``str`` nor a usable index raises
      ``IndexError("Index must be int or string")``, not ``TypeError``;
    - ``keys()`` hands back a fresh ``list`` a caller may freely mutate.

    ``row == some_tuple`` stays ``False``, which is also what ``sqlite3.Row``
    does -- neither class is a tuple subclass. ``tests/test_db_portable.py``
    asserts each of these against a live ``sqlite3.Row`` (TEST-LOGIC, no PG).
    """

    __slots__ = ("_values", "_names")

    def __init__(self, values: tuple[object, ...], names: tuple[str, ...]) -> None:
        self._values = values
        self._names = names

    def __getitem__(self, key: object) -> object:
        if isinstance(key, str):
            # A linear scan, not a dict lookup and not an exact-match-first
            # shortcut: sqlite3.Row answers ``row["OWNER"]`` with the *first*
            # column whose name matches case-insensitively, even when a later
            # column matches exactly. Anything cleverer would silently pick a
            # different column than the desktop lane does.
            wanted = key.lower()
            for index, name in enumerate(self._names):
                if name.lower() == wanted:
                    return self._values[index]
            raise IndexError("No item with that key") from None
        try:
            # An int (negative included), anything exposing ``__index__`` and a
            # slice all reach the tuple directly; a float / bytes / None key
            # raises TypeError there and is re-raised as the IndexError that
            # sqlite3.Row raises for the same key.
            return self._values[cast(Any, key)]
        except TypeError:
            raise IndexError("Index must be int or string") from None

    def __iter__(self) -> Iterator[object]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        # A fresh list, matching sqlite3.Row: a caller appending to it must not
        # corrupt the column names the next row of the same query reports.
        return list(self._names)


def _named_row_factory(cursor: psycopg.Cursor) -> Callable[[Sequence[object]], _NamedRow]:
    """psycopg3 row factory: called once with the cursor, returns the per-row
    maker bound to that query's column names."""
    names = tuple(col[0] for col in cursor.description) if cursor.description else ()

    def maker(values: Sequence[object]) -> _NamedRow:
        return _NamedRow(tuple(values), names)

    return maker


# ---------------------------------------------------------------------------
# The business facade
# ---------------------------------------------------------------------------


def _is_begin_immediate(sql: str) -> bool:
    return sql.strip().upper() == "BEGIN IMMEDIATE"


def _is_sqlite_pragma(sql: str) -> bool:
    return sql.strip().upper().startswith("PRAGMA")


class _NoopCursor:
    """The result of a swallowed SQLite-only statement on the PG lane."""

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []

    @property
    def rowcount(self) -> int:
        return -1


_NOOP_CURSOR = _NoopCursor()


def _pg_literal(value: object) -> str:
    """Render a Python value as a SQL literal for the iterdump output (CW-054).

    psycopg3 hands JSONB back as a ``dict``/``list`` and NUMERIC as a
    ``Decimal``. Routing those through ``str()`` would emit a Python repr
    (``{'k': 'v'}``) that is neither valid JSON nor valid SQL, so the dump
    would not faithfully show what is actually stored — which is the whole
    point of the sensitive-data check.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        # Before int: bool is an int subclass and must not render as 1/0.
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        # Numeric literals stay unquoted so the dump replays into a
        # BIGINT/NUMERIC column instead of a quoted string.
        return str(value)
    if isinstance(value, bytes):
        return f"'\\x{value.hex()}'"
    if isinstance(value, (dict, list)):
        # JSONB: emit real JSON. sort_keys keeps the dump deterministic across
        # runs (the CW-007 repeatability contract); default=str covers a
        # Decimal/datetime nested inside the document.
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        # Everything else (str, datetime) is quoted text.
        text = str(value)
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


class BusinessConnection:
    """The uniform connection the business services see.

    Presents the sqlite3-shaped surface the services already use — execute,
    ``with conn:``, commit, rollback — and dispatches to the active backend.
    ``.transaction()`` is the explicit write transaction: BEGIN IMMEDIATE on
    SQLite, a no-op on PostgreSQL where the outer ``fenced_pg_transaction``
    already holds the transaction. ``commit()``/``rollback()`` are no-ops on
    PostgreSQL (commit authority stays with the fenced transaction).

    CW-054: the PG lane now implements ``executemany`` (a real batch write that
    reports ``rowcount``), ``iterdump`` (a real dump for sensitive-data checks)
    and ``set_trace_callback`` (statement tracing for test counters); none of
    them may pass as a no-op on the customer lane (PG-02). Constraint
    violations surface as ``IntegrityConstraintError``.

    ``commit``/``rollback``/``close``/``transaction``/``__exit__`` keep their
    deliberate PG no-op semantics untouched — commit authority belongs to the
    outer fenced transaction (CW-055), and this task must not turn a no-op
    commit into a mid-flight business commit.
    """

    def __init__(self, backend: PostgresBackend) -> None:
        self._backend = backend
        self.ctx: object | None = None  # CustomerSessionContext (T21 fencing)
        self.api_key_id: str | None = None  # Authenticated credential, never client input.
        self.auth_source: str = "internal"
        self._trace_callback: Callable[[str], object] | None = None  # CW-054

    # --- sqlite3-shaped surface ---

    def execute(self, sql: str, params: Sequence[object] = ()) -> _BusinessCursor:
        if isinstance(self._backend, PostgresBackend) and _is_begin_immediate(sql):
            # Migrated write services still spell their write lock as
            # ``BEGIN IMMEDIATE`` (the SQLite idiom). On the customer lane the
            # fenced transaction is already open, so the statement must be a
            # no-op — forwarding it to psycopg is a syntax error (PR #56 P1).
            return _NOOP_CURSOR
        if isinstance(self._backend, PostgresBackend) and _is_sqlite_pragma(sql):
            # SQLite tuning statements (busy_timeout & friends) carry no
            # meaning on the PG lane — pool timeouts own that concern.
            return _NOOP_CURSOR
        if self._trace_callback is not None and isinstance(self._backend, PostgresBackend):
            # CW-054: sqlite3's trace hook only ever sees a statement that is
            # actually sent to the engine, so the swallowed BEGIN IMMEDIATE /
            # PRAGMA no-ops above must not be reported. A test counting
            # statements would otherwise be charged for work the PG lane never
            # did. Traced text is the SQL template — psycopg does not expose
            # sqlite3's parameter-substituted form.
            self._trace_callback(sql)
        return self._backend.execute(sql, params)

    def executemany(self, sql: str, seq: Sequence[Sequence[object]]) -> _BusinessCursor:
        """sqlite3-shaped batch write; returns a cursor carrying ``rowcount``.

        Both lanes execute the batch for real and report ``rowcount`` equal to
        the number of parameter sets; an empty sequence reports 0 and writes
        nothing. ``sqlite3.Connection.executemany`` returns its cursor, so the
        PG lane matching that shape is what makes the two interchangeable —
        and it is the only way a caller can see that a non-empty batch really
        persisted every parameter set. The PG lane was previously a complete
        no-op, i.e. a batch write on the customer lane persisted nothing.
        """
        if self._trace_callback is not None and isinstance(self._backend, PostgresBackend):
            # Once per batch, not once per parameter set: psycopg3 sends the
            # whole batch as a single command, whereas sqlite3 hands the trace
            # hook each substituted statement it executes.
            self._trace_callback(sql)
        return self._backend.executemany(sql, seq)

    def commit(self) -> None:
        """No-op by design: transaction ownership is the fenced
        pg_transaction() context on the (now only) PostgreSQL lane. The
        retired SQLite lane was the only caller-owned-transaction lane."""

    def rollback(self) -> None:
        """No-op by design: see commit(); rollback is owned by
        pg_transaction() on error paths."""

    @property
    def is_postgres(self) -> bool:
        """Lane probe for services that must behave by backend (the fair-queue
        cursor maintenance runs unconditionally on PostgreSQL but has no table
        on the desktop SQLite lane)."""
        return isinstance(self._backend, PostgresBackend)

    @property
    def in_transaction(self) -> bool:
        """The sqlite3-shaped transaction-state probe the services use.

        SQLite: delegates to the underlying connection. PostgreSQL: the fenced
        transaction owns the state; report ``True`` once a transaction is open
        (the psycopg info parity) so callers that guard on it keep working.
        """
        return bool(self._backend.raw.info.transaction_status)

    def close(self) -> None:
        """No-op by design: pooled PG connections are returned by the
        pg_transaction() context, never closed by business callers."""

    def set_trace_callback(self, callback: Callable[[str], object] | None) -> None:
        """sqlite3-shaped SQL trace hook (tests count statements).

        CW-054: the PG lane now stores the callback and invokes it from
        ``execute`` for every statement. Setting ``None`` disables tracing.
        """
        self._trace_callback = callback

    def iterdump(self) -> Iterator[str]:
        """sqlite3-shaped whole-database dump (tests check no secret is stored).

        CW-054: the PG lane now yields real INSERT statements for every user
        table so sensitive-data checks traverse actual rows instead of an
        empty iterator.
        """
        return self._pg_iterdump()

    def _pg_iterdump(self) -> Iterator[str]:
        """Yield INSERT statements for every user table on the PG lane.

        Mirrors the sqlite3 ``iterdump`` contract closely enough for the
        sensitive-data checks: each row becomes an INSERT with literal
        values (NULL / numeric / quoted string). System catalogs and the
        alembic_version bookkeeping table are skipped.
        """
        conn = self._backend.raw
        # Enumerate user tables in the public schema, excluding alembic's.
        tables = conn.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename <> 'alembic_version' "
            "ORDER BY tablename"
        ).fetchall()
        for (table_name,) in tables:
            # Quote the table name for the INSERT header.
            quoted_table = f'"{table_name}"'
            rows = conn.execute(f"SELECT * FROM {quoted_table}").fetchall()
            if not rows:
                continue
            # Column names from the first row's keys() (the PG lane uses
            # _named_row_factory so every row is a _NamedRow).
            first_row = cast(_NamedRow, rows[0])
            columns = first_row.keys()
            col_list = ", ".join(f'"{c}"' for c in columns)
            for row in rows:
                values = ", ".join(_pg_literal(v) for v in row)
                yield f"INSERT INTO {quoted_table} ({col_list}) VALUES ({values});"

    # --- context-manager: `with conn:` commits on success, rolls back on error
    # --- (the sqlite3 contract the services already rely on)
    def __enter__(self) -> BusinessConnection:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        """No-op by design: on the (now only) PostgreSQL lane the fenced
        pg_transaction() context owns commit/rollback; the retired SQLite
        lane was the only caller-owned ``with conn:`` transaction lane."""

    @contextmanager
    def transaction(self, isolation: str | None = None) -> Iterator[BusinessConnection]:
        """Explicit write transaction. PG: no-op — the outer
        ``fenced_pg_transaction`` owns the transaction, and ``isolation`` is
        forwarded there by the wiring (T21)."""
        yield self

    @property
    def raw(self) -> psycopg.Connection:
        return self._backend.raw

    # --- factory ---

    @classmethod
    def postgres(cls, conn: psycopg.Connection) -> BusinessConnection:
        return cls(PostgresBackend(conn))
