"""Unit tests for the shared bearer-token parser (finding C5).

The parser consolidates four formerly duplicated ``_bearer_token`` helpers and
fixes the ``ops_metrics`` exact-case drift, so these tests pin the
case-insensitive behaviour that all lanes must now share.
"""

from __future__ import annotations

from starlette.requests import Request

from app.auth_headers import bearer_token


def _request(auth_header: str | None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if auth_header is not None:
        headers.append((b"authorization", auth_header.encode("latin-1")))
    return Request({"type": "http", "headers": headers})


def test_parses_standard_header() -> None:
    assert bearer_token(_request("Bearer abc123")) == "abc123"


def test_scheme_is_case_insensitive() -> None:
    # The former ops_metrics helper required exact-case "Bearer" and rejected
    # these; the shared parser must accept any case (RFC 7235).
    assert bearer_token(_request("bearer abc123")) == "abc123"
    assert bearer_token(_request("BEARER abc123")) == "abc123"
    assert bearer_token(_request("BeArEr abc123")) == "abc123"


def test_returns_none_when_header_absent_or_blank() -> None:
    assert bearer_token(_request(None)) is None
    assert bearer_token(_request("")) is None
    assert bearer_token(_request("   ")) is None


def test_returns_none_when_malformed() -> None:
    assert bearer_token(_request("Basic abc123")) is None
    assert bearer_token(_request("Bearer")) is None
    assert bearer_token(_request("abc123")) is None


def test_strips_surrounding_whitespace() -> None:
    assert bearer_token(_request("  Bearer   abc123  ")) == "abc123"


def test_returns_none_for_empty_token() -> None:
    assert bearer_token(_request("Bearer    ")) is None


def test_preserves_token_with_internal_structure() -> None:
    # split(None, 1) keeps everything after the first run of whitespace as the
    # token, so JWT-style values survive intact.
    assert bearer_token(_request("Bearer eyJhbGci.eyJzdWIi.sig")) == "eyJhbGci.eyJzdWIi.sig"
