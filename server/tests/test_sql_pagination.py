from __future__ import annotations

from app.sql_pagination import PAGE_CLAUSE, page_bounds


def test_page_clause_is_the_literal_the_lanes_used_before_the_a11_convergence() -> None:
    """The shared clause must stay byte-identical to the 26 inlined copies it replaced."""
    assert PAGE_CLAUSE == "LIMIT %s OFFSET %s"


def test_page_bounds_keeps_an_in_range_window_unchanged() -> None:
    assert page_bounds(50, 100, max_limit=200) == (50, 100)
    assert page_bounds(200, 0, max_limit=200) == (200, 0)


def test_page_bounds_caps_the_page_size_at_max_limit() -> None:
    assert page_bounds(500, 0, max_limit=200) == (200, 0)


def test_page_bounds_allows_a_zero_page_by_default() -> None:
    # The admin/audit lanes honour an explicit limit=0 as an empty page.
    assert page_bounds(0, 40, max_limit=200) == (0, 40)
    assert page_bounds(-5, 0, max_limit=200) == (0, 0)


def test_page_bounds_min_limit_floor_keeps_at_least_one_row() -> None:
    # The customer-account list and the customer oral task list never page to 0.
    assert page_bounds(0, 0, max_limit=200, min_limit=1) == (1, 0)
    assert page_bounds(-5, 0, max_limit=100, min_limit=1) == (1, 0)


def test_page_bounds_clamps_a_negative_offset() -> None:
    assert page_bounds(20, -1, max_limit=100) == (20, 0)
