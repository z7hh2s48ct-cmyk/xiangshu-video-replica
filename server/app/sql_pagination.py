"""Shared pagination boilerplate for the ``LIMIT %s OFFSET %s`` lanes (A11).

The admin review (2026-09-12) recorded "8 duplicated pagination boilerplates";
the audit behind this module found the clause literal itself in **26** places
across **16** modules of ``server/app`` (``admin_customer_routes`` /
``control_routes`` / ``recharge_routes`` three times each, ``admin_session_routes``
/ ``billing_reports`` / ``simple_character`` / ``viral_collection_billing``
twice). All 26 are the same trailing clause: two positional placeholders, page
size bound first and offset second. That text and that argument order now live
here once::

    rows = conn.execute(
        f"SELECT ... ORDER BY created_at DESC, id DESC {PAGE_CLAUSE}",
        (*params, bounded_limit, bounded_offset),
    )

    bounded_limit, bounded_offset = page_bounds(limit, offset, max_limit=MAX_LIST_LIMIT)

What deliberately stays at the call site: how the total is counted
(``COUNT(*)`` in a second statement vs ``count(*) OVER ()`` in the paged one),
the response envelope, and each endpoint's page-size ceiling and floor — those
differ per lane, and unifying them would change behaviour rather than remove
duplication. The sibling cap-only shape (``LIMIT %s`` with no ``OFFSET``, 27
sites in 13 modules of ``server/app``) is a top-N cap, not offset pagination, and
keeps its own literal.
"""

from __future__ import annotations

#: Trailing clause every offset-paginated statement ends with. The statement
#: must bind the page size first and the offset second, in that order.
PAGE_CLAUSE = "LIMIT %s OFFSET %s"


def page_bounds(limit: int, offset: int, *, max_limit: int, min_limit: int = 0) -> tuple[int, int]:
    """Clamp a caller-supplied window to ``(bounded_limit, bounded_offset)``.

    ``min_limit`` carries the only policy difference these lanes genuinely have:
    the admin/audit list endpoints hand ``min_limit=0`` (an explicit ``limit=0``
    asking for an empty page is honoured), while the customer-account list and
    the customer-side oral task list use ``min_limit=1``. Offsets are clamped to
    zero everywhere.
    """
    return max(min_limit, min(limit, max_limit)), max(0, offset)
