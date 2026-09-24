"""Read-only workspace search over stored, caller-visible content."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.auth import CurrentUser
from app.db_portable import BusinessConnection
from app.materials import list_materials
from app.simple_character import list_simple_library_page
from app.sql_pagination import PAGE_CLAUSE

SearchKind = Literal["video", "script", "person", "material"]


class StudioSearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: SearchKind
    title: str
    description: str = ""
    platform: str | None = None
    person: dict[str, Any] | None = None


class StudioSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[StudioSearchItem]
    total: int
    page: int
    page_size: int


def search_studio(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    query: str,
    kind: SearchKind,
    page: int,
    page_size: int,
) -> StudioSearchResponse:
    query = query.strip()
    if not query:
        return StudioSearchResponse(items=[], total=0, page=page, page_size=page_size)
    items: list[StudioSearchItem] = []
    # Reuse material visibility, hidden-item rules and server pagination.
    if kind == "material":
        materials = list_materials(
            conn,
            actor=actor,
            media_type=None,
            source=None,
            query=query,
            page=page,
            page_size=page_size,
        )
        items = [
            StudioSearchItem(
                id=item.id,
                kind=kind,
                title=item.title,
                description=item.media_type,
            )
            for item in materials.items
        ]
        total = materials.total
    elif kind == "person":
        # The library owns publication/identity permissions and cursor scope.
        people = list_simple_library_page(
            conn,
            actor=actor,
            limit=page_size,
            cursor=None,
            query=query,
            offset=(page - 1) * page_size,
        )
        items = [
            StudioSearchItem(
                id=item.identity_id,
                kind=kind,
                title=item.display_name,
                description=item.role,
                person=asdict(item),
            )
            for item in people.items
        ]
        total = people.total
    else:
        # strpos treats % and _ as literal user input. Never interpolate search text.
        if kind == "script":
            source = "studio_saved_scripts AS content"
            predicate = (
                "content.user_id = %s AND strpos(lower(content.title || ' ' || "
                "content.text), lower(%s)) > 0"
            )
            parameters: tuple[object, ...] = (actor.id, query)
            columns = "content.script_id AS id, content.title, '' AS platform"
        else:
            source = "viral_videos AS content"
            predicate = """strpos(lower(content.title || ' ' || content.author), lower(%s)) > 0
                AND NOT EXISTS (SELECT 1 FROM viral_video_visibility AS visibility
                    WHERE visibility.platform = content.platform
                    AND visibility.video_id = content.video_id
                    AND visibility.status IN ('hidden', 'unavailable'))"""
            parameters = (query,)
            columns = "content.video_id AS id, content.title, content.platform"
        row = conn.execute(
            f"SELECT COUNT(*) AS total FROM {source} WHERE {predicate}",
            parameters,
        ).fetchone()
        total = int(row["total"])
        rows = conn.execute(
            f"SELECT {columns} FROM {source} WHERE {predicate} "
            f"ORDER BY content.updated_at DESC, id DESC, platform DESC {PAGE_CLAUSE}",
            (*parameters, page_size, (page - 1) * page_size),
        ).fetchall()
        items = [
            StudioSearchItem(
                id=str(item["id"]),
                kind=kind,
                title=str(item["title"]),
                platform=str(item["platform"]) or None,
            )
            for item in rows
        ]
    return StudioSearchResponse(items=items, total=total, page=page, page_size=page_size)
