"""Shanghai calendar filters over UTC text timestamps, independent of session TZ."""

from __future__ import annotations

import re
from collections.abc import MutableSequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException

SHANGHAI = ZoneInfo("Asia/Shanghai")


def admin_date_bounds(start: str | None, end: str | None) -> list[tuple[str, str]]:
    bounds: list[tuple[str, str]] = []
    try:
        for value, is_end in ((start, False), (end, True)):
            if not value or not value.strip():
                continue
            value = value.strip()
            date_only = re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is not None
            if date_only:
                day = date.fromisoformat(value)
                if is_end:
                    day += timedelta(days=1)
                instant = datetime.combine(day, time.min, SHANGHAI)
            else:
                instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if instant.tzinfo is None:
                    instant = instant.replace(tzinfo=SHANGHAI)
            operator = ("<" if date_only else "<=") if is_end else ">="
            bounds.append((operator, instant.astimezone(UTC).isoformat()))
        if len(bounds) == 2 and (
            bounds[0][1] > bounds[1][1] or (bounds[0][1] == bounds[1][1] and bounds[1][0] == "<")
        ):
            raise ValueError("reversed range")
    except (ValueError, OverflowError) as exc:
        raise HTTPException(
            422, detail={"code": "INVALID_DATE_RANGE", "message": "日期范围无效。"}
        ) from exc
    return bounds


def utc_timestamp_sql(column: str, *, postgres: bool = True) -> str:
    # Only source-controlled identifiers reach this helper; guard future callers.
    if re.fullmatch(r"[a-z_][a-z_0-9]*(\.[a-z_][a-z_0-9]*)?", column) is None:
        raise ValueError("invalid timestamp column")
    if not postgres:
        return f"datetime({column})"
    return (
        f"(CASE WHEN {column}::text ~ '[Tt ][0-9]{{2}}:[0-9]{{2}}.*"
        f"([Zz]|[+-][0-9]{{2}}(:?[0-9]{{2}})?)$' THEN {column}::timestamptz "
        f"ELSE {column}::timestamp AT TIME ZONE 'UTC' END)"
    )


def append_admin_date_filters(
    clauses: list[str],
    params: MutableSequence[Any],
    *,
    column: str,
    created_from: str | None,
    created_to: str | None,
    postgres: bool = True,
) -> None:
    expression = utc_timestamp_sql(column, postgres=postgres)
    parameter = "%s::timestamptz" if postgres else "datetime(%s)"
    for operator, bound in admin_date_bounds(created_from, created_to):
        clauses.append(f"{expression} {operator} {parameter}")
        params.append(bound)
