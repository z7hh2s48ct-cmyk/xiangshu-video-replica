from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app import character_identity_routes, material_routes, media_routes
from app.character_identity import CreatedIdentityUploadIntent
from app.materials import MaterialUploadIntentRequest, MaterialUploadIntentResponse
from app.media import CreatedUploadIntent


class BusinessDb:
    @contextmanager
    def write(self):
        yield None, SimpleNamespace(role="admin")


@pytest.mark.parametrize("provider", ["local", "cos"])
def test_managed_uploads_use_api_relative_paths_and_cloud_urls_are_preserved(monkeypatch, provider):
    cloud_url = "https://cloud.example/upload?test=preserved"
    storage = SimpleNamespace(provider=provider)
    common = dict(
        asset_id="asset-1",
        storage_key="test/portrait image.png",
        method="PUT",
        url=cloud_url,
        headers={"Content-Type": "image/png"},
        expires_at="2030-01-01T00:00:00Z",
    )
    monkeypatch.setattr(
        media_routes,
        "create_upload_intent",
        lambda *a, **kw: CreatedUploadIntent(
            **common, project_id="project-1", upload_required=True
        ),
    )
    video = media_routes.create_asset_upload_intent(
        media_routes.UploadIntentRequest(
            project_id="project-1", filename="test.mp4", content_type="video/mp4", size_bytes=100
        ),
        BusinessDb(),
        storage,
    )
    identity = character_identity_routes.local_upload_intent(
        CreatedIdentityUploadIntent(**common, identity_id="person-1", purpose="source"),
        storage=storage,
    )
    monkeypatch.setattr(
        material_routes,
        "create_material_upload_intent",
        lambda *a, **kw: MaterialUploadIntentResponse(**common, material_id="material-1"),
    )
    material = material_routes.create_upload_intent(
        MaterialUploadIntentRequest(filename="test.mp3", content_type="audio/mpeg", size_bytes=100),
        BusinessDb(),
        storage,
    )
    if provider == "local":
        assert video.url == identity.url == "/api/assets/local-objects/test/portrait%20image.png"
        assert material.url == "/api/studio/materials/uploads/asset-1/content"
    else:
        assert video.url == identity.url == material.url == cloud_url
    assert video.headers == identity.headers == material.headers == common["headers"]
