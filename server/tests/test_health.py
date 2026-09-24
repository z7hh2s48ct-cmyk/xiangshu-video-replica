import asyncio

from httpx import ASGITransport, AsyncClient, Response

from app.main import app


def get(path: str, *, origin: str | None = None) -> Response:
    async def request() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Origin": origin} if origin else None
            return await client.get(path, headers=headers)

    return asyncio.run(request())


def test_health_returns_service_identity() -> None:
    response = get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "video-replica-api"}


def test_health_allows_packaged_app_origins() -> None:
    for origin in ("http://tauri.localhost", "tauri://localhost"):
        response = get("/health", origin=origin)

        assert response.headers["access-control-allow-origin"] == origin


def test_cors_allows_mutating_desktop_requests() -> None:
    async def request() -> Response:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.options(
                "/api/admin/settings/runtime",
                headers={
                    "Origin": "tauri://localhost",
                    "Access-Control-Request-Method": "PATCH",
                },
            )

    response = asyncio.run(request())

    assert response.status_code == 200
    assert "PATCH" in response.headers["access-control-allow-methods"]


def test_openapi_exposes_health_contract() -> None:
    schema = get("/openapi.json").json()

    assert schema["paths"]["/health"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/HealthResponse"}


def test_non_loopback_request_is_rejected() -> None:
    async def request() -> Response:
        transport = ASGITransport(app=app, client=("203.0.113.5", 54321))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health")

    response = asyncio.run(request())

    assert response.status_code == 403
    assert response.json()["code"] == "LOOPBACK_ONLY"


def test_loopback_request_is_allowed() -> None:
    response = get("/health")

    assert response.status_code == 200
