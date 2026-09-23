"""管理端计费报表导出的 CSV 公式注入防护。

export_controller 的 username（客户可控）、service_type 等单元格必须与其它
导出 lane 一样走 csv_export.spreadsheet_safe_cell 的六前缀防护（OWASP CSV 注入）。
不依赖数据库：用假 conn 直接测 generate_csv_content 的行写路径。
"""

from __future__ import annotations

import csv
import gzip
import io
from datetime import date

import pytest

from app.export_controller import ExportRequest, generate_csv_content


class _FakeResult:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows


class _FakeConn:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def execute(self, _sql: str, _params: object) -> _FakeResult:
        return _FakeResult(self._rows)


def _row(username: str | None) -> dict:
    return {
        "id": "tx-1",
        "user_id": "user-1",
        "username": username,
        "billing_date": date(2026, 9, 23),
        "service_type": "-replica-voice",
        "credits_delta": -100,
        "credits_used": 100,
        "unit_cost_fen": 10,
        "cost_fen": 10.0,
        "billing_round": 1,
    }


def _request() -> ExportRequest:
    return ExportRequest(start_date=date(2026, 9, 1), end_date=date(2026, 9, 2))


def _csv_rows(content: bytes) -> list[list[str]]:
    text = gzip.decompress(content).decode("utf-8")
    return list(csv.reader(io.StringIO(text)))


def test_username_with_formula_prefix_is_neutralised() -> None:
    content = generate_csv_content(_FakeConn([_row('=HYPERLINK("http://evil","x")')]), _request())

    data_rows = _csv_rows(content)[1:]
    assert len(data_rows) == 1
    assert data_rows[0][2].startswith("'=")


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_every_formula_prefix_neutralised_in_username(prefix: str) -> None:
    content = generate_csv_content(_FakeConn([_row(f"{prefix}CMD")]), _request())

    assert _csv_rows(content)[1][2] == f"'{prefix}CMD"


def test_service_type_with_formula_prefix_is_neutralised() -> None:
    content = generate_csv_content(_FakeConn([_row("alice")]), _request())

    assert _csv_rows(content)[1][4] == "'-replica-voice"


def test_plain_username_untouched() -> None:
    content = generate_csv_content(_FakeConn([_row("alice")]), _request())

    assert _csv_rows(content)[1][2] == "alice"


def test_none_username_renders_na() -> None:
    content = generate_csv_content(_FakeConn([_row(None)]), _request())

    assert _csv_rows(content)[1][2] == "N/A"
