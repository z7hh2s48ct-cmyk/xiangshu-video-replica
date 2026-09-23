"""Request ID middleware for tracing and debugging.

This middleware generates unique request IDs that propagate through
the entire request lifecycle for log correlation.

Version: V1.0 (2026-09-20)
Author: AI Agent (Qoder)
Status: Ready for Integration
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Add request ID to all requests and responses."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request[Any]], Awaitable[Response]],
    ) -> Response:
        """Process request and add request ID header."""

        # Generate or extract request ID
        request_id = request.headers.get("x-request-id")

        if not request_id:
            request_id = str(uuid.uuid4())

        # Attach to ASGI scope for downstream access
        request.state.request_id = request_id

        # Add header to response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id

        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Store request context in ASGI scope."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request[Any]], Awaitable[Response]],
    ) -> Response:
        """Store request metadata in scope."""

        # Extract client info
        client_host = request.client.host if request.client else "unknown"
        client_port = request.client.port if request.client else 0

        # Store in scope
        request.scope["client_info"] = {
            "host": client_host,
            "port": client_port,
        }

        # Add user agent
        request.scope["user_agent"] = request.headers.get("user-agent", "")

        # Add method and path
        request.scope["method_path"] = f"{request.method} {request.url.path}"

        response = await call_next(request)

        return response
