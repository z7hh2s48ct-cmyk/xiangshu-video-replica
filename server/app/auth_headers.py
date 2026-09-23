"""Shared bearer-token parsing for the customer request lanes.

Consolidates the four formerly duplicated ``_bearer_token`` helpers
(``customer_device_routes`` / ``customer_fence`` / ``customer_session_routes``
/ ``ops_metrics``) into a single case-insensitive parser.

This removes the ``ops_metrics`` drift (finding C5): that copy compared the
auth scheme with exact case (``parts[0] != "Bearer"``) and therefore rejected
a lowercase ``bearer`` prefix that every other lane accepted. RFC 7235 defines
the scheme as case-insensitive, so the unified parser lower-cases before
comparing.
"""

from __future__ import annotations

from fastapi import Request

AUTHORIZATION_HEADER = "Authorization"
BEARER_SCHEME = "bearer"


def bearer_token(request: Request) -> str | None:
    """Return the raw bearer token, or ``None`` when absent/malformed.

    The scheme comparison is case-insensitive (RFC 7235): ``Bearer``,
    ``bearer`` and ``BeArEr`` all parse identically. A header without exactly
    one space-separated scheme plus a non-empty token yields ``None``.
    """
    header = request.headers.get(AUTHORIZATION_HEADER, "").strip()
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != BEARER_SCHEME:
        return None
    token = parts[1].strip()
    return token or None
