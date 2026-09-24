"""Shared CSV / spreadsheet export helpers.

Consolidates the spreadsheet-injection guard that was previously duplicated
across the CSV export lanes (``control_routes`` / ``admin_customer_routes`` /
``billing_routes``), each with a different level of coverage.

Finding C15: ``admin_customer_routes`` wrote user-controlled cells (``username``)
with *no* escaping at all, and ``billing_routes`` escaped only ``= + - @``
(missing the ``\\t`` / ``\\r`` formula-injection vectors). Every lane now routes
through this single six-prefix guard, matching the most complete
``control_routes`` implementation so the exports behave identically.
"""

from __future__ import annotations

# The leading characters a spreadsheet may interpret as a formula, plus the
# tab / carriage-return injection vectors (OWASP CSV-injection guidance).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe_cell(value: object) -> object:
    """Prefix a leading-formula string with ``'`` so spreadsheets treat it as text.

    Non-string values (numbers, ``None``, dates already rendered to ``str``)
    are returned unchanged. Only ``str`` cells whose *first* character is one of
    ``= + - @ \\t \\r`` are neutralised with a leading single quote; a dangerous
    character that is not leading never triggers escaping.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return f"'{value}"
    return value
