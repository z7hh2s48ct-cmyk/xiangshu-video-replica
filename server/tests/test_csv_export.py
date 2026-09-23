from __future__ import annotations

import pytest

from app.csv_export import spreadsheet_safe_cell


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_neutralises_every_formula_prefix(prefix: str) -> None:
    """All six OWASP formula-injection vectors get a leading single quote."""
    assert spreadsheet_safe_cell(f"{prefix}CMD") == f"'{prefix}CMD"


def test_leaves_plain_strings_untouched() -> None:
    assert spreadsheet_safe_cell("alice") == "alice"
    assert spreadsheet_safe_cell("2026-09-17") == "2026-09-17"
    assert spreadsheet_safe_cell("ACTIVE") == "ACTIVE"


def test_leaves_non_strings_untouched() -> None:
    assert spreadsheet_safe_cell(1500) == 1500
    assert spreadsheet_safe_cell(None) is None
    assert spreadsheet_safe_cell(3.5) == 3.5


def test_only_a_leading_dangerous_character_triggers() -> None:
    # A dangerous character that is not leading must not trigger escaping.
    assert spreadsheet_safe_cell("a=b") == "a=b"
    assert spreadsheet_safe_cell("user@host") == "user@host"
    assert spreadsheet_safe_cell("1-2") == "1-2"


def test_empty_string_untouched() -> None:
    assert spreadsheet_safe_cell("") == ""
