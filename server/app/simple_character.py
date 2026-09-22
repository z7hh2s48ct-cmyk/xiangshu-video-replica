"""Simple character upload flow (方案 A: 极简人物库).

Uploads a single authorization image, derives the five standard views from a
deterministic local generator, records approval reviews, and publishes the
character version in one transaction so it immediately shows up in the
project's available character version list.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import sqlite3
import struct
import uuid
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException

from app import content_store
from app.auth import CurrentUser
from app.character_asset_review import (
    CHARACTER_PUBLICATION_SCHEMA_VERSION,
    cleanup_publication_objects,
)
from app.character_contracts import PersonIdentity, RequiredCharacterViewType
from app.character_identity import (
    CHARACTER_TEMPLATE_HASH,
    CHARACTER_TEMPLATE_VERSION,
    REQUIRED_CHARACTER_VIEW_TYPES,
    character_error,
    encode_json,
    generated_character_asset_key,
    get_person_identity,
    identity_asset_key,
    persona_snapshot,
    read_identity_row,
    required_text,
    validate_key_segment,
)
from app.character_image_generation import png_chunk
from app.db_portable import BusinessConnection
from app.first_frames import (
    FirstFrameModel,
    ImageInput,
    ImageProvider,
    ImageProviderFailed,
    SceneContactSheetQualityResult,
)
from app.media import storage_key_from_uri
from app.media_tools import (
    MediaToolFailed,
    MediaToolUnavailable,
    resolve_media_binary,
    validate_image_decodable,
)
from app.permissions import require_not_auditor, require_project_access, write_audit
from app.storage import (
    StorageAdapter,
    StorageBackendUnavailable,
    StoragePermissionError,
    StoredObject,
)

logger = logging.getLogger(__name__)

SIMPLE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
SIMPLE_PNG_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
SIMPLE_IMAGE_MAX_PIXELS = 100_000_000
SIMPLE_UPLOAD_ALLOWED_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
SIMPLE_AUTHORIZATION_SCOPE = ["internal-short-video"]
SIMPLE_PERSONA_USAGE_SCOPE = ["internal-short-video"]
SIMPLE_GENERATION_MODE = "simple_upload"

# Single-sheet five-view generation: the uploaded photo is the identity
# reference and the provider renders one wide landscape contact sheet with
# five views of the SAME person (identity-preserve prompt).
SIMPLE_CONTACT_SHEET_MODEL: FirstFrameModel = "gpt-image-2"
SIMPLE_CONTACT_SHEET_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
# 五视图是 4 列横版排版（左侧 3 个全身竖版 + 右侧 1 列 2 个半身方图），必须显式声明
# 横版宽高比。不声明时：gpt-image-2 会退回 size=auto（拿不到 2K），而
# nano-banana-pro-2k 会按源图尺寸推出最接近的比例——竖版或方版人像照会推成 9:16 /
# 1:1，与横版排版相冲。声明 16:9 后 gpt-image-2 走 2048x1152（单格约 512px）。
SIMPLE_CONTACT_SHEET_ASPECT_RATIO = "16:9"
# 五视图的整图尺寸，比首帧的 16:9 档位（2048x1152）更宽，依据是实测：模型把约
# 815px 固定留给右侧近景列，剩余宽度才分给左侧三个全身格——整图不够宽时全身格反而
# 变窄（2048 实测最窄 386px，比不声明尺寸时的 405px 还窄）。2560x1440 实测全身格
# 548~573px、近景列 813px，是唯一位于验收区间（单格 500-700px）的档位；且仍在网关
# 非实验档位内（上限即 2560x1440）。
CONTACT_SHEET_SIZE = "2560x1440"
SIMPLE_CONTACT_SHEET_PROMPT = """\
You are a professional portrait photographer. Capture one real person from the 
uploaded photo as a set of five studio reference photographs arranged in a single 
horizontal contact sheet layout with thin white dividers between panels:

Layout structure (one wide landscape image split into five panels):
1. Left tall panel: full-body front view, feet and head fully visible with safe margins
2. Second tall panel: full-body three-quarter view turned slightly to camera-left
3. Third tall panel: full-body left profile view, complete silhouette from crown to soles
4. Top-right square: head-and-shoulders close-up front-facing, matching the three-quarter 
   body angle seen in panel 2, cropped at upper chest
5. Bottom-right square: head-and-shoulders three-quarter close-up consistent with the 
   third body view (left profile), also cropped at upper chest

Identity and styling invariants: the exact same person in every panel; preserve the 
facial proportions and recognizable appearance from the uploaded photo, including eye 
shape, nose, lips, skin tone, hairstyle and hair length, headwear, visible jewelry, 
makeup level, and clothing. Keep face, eye shape, hair, accessories, body build, and 
clothing identical across all five panels. Neutral relaxed expression with a very subtle 
friendly softness; eyes level when visible. Arms relaxed naturally at the sides; feet 
parallel in the front view.

Conservative full-body continuation: where the uploaded photo does not show the lower 
body, extend the visible outfit simply and neutrally with plain trousers and plain 
low-profile closed shoes. No belt, bag, visible brand, pattern, or extra accessories. 
Keep body proportions realistic and consistent.

Background: seamless neutral gray studio backdrop without hot spots or center glare; 
uniform across all panels.

Photography style:
- Shot on Canon EOS R5, 85mm f/1.8 lens for portraits; medium telephoto compression that 
  flatters facial proportions without distortion
- Real human skin with visible pores, subtle asymmetry, natural vellus hairs (peach fuzz), 
  and small freckles if present in the source photo
- Individual hair strands clearly rendered separately; no clumped or glossy plastic-looking 
  hair masses
- Fabric weave texture visibly detailed on clothing; shoelaces, stitching, and realistic 
  fabric shadows and folds
- Softbox lighting similar to Elinchrom D-Lux 500W studio strobes, gentle wrap-around fill, 
  no harsh shadows or burnt highlights
- Natural subsurface scattering on thin skin areas (ear tips, nose bridge) giving skin a 
  translucent living quality
- Minimal post-processing; retain some raw camera noise for authenticity
- Slight film grain or ISO noise acceptable; avoid over-sharpening, halo effects, or HDR glow

Composition/framing: full bodies completely visible from head to soles with generous safe 
margins in the three full-body panels; the two right panels crop at upper chest; identical 
camera height and focal length within corresponding panel types; no overlap between panels.

Lighting/mood: soft even studio illumination, neutral white balance, mild floor grounding 
shadow only in full-body panels, consistent exposure everywhere.

Constraints: exactly five panels and exactly five appearances of the same person; layout 
fidelity is critical; identity fidelity to the uploaded photo is the highest subject priority; 
no text, labels, arrows, captions, logos, watermark, props, furniture, room background, or 
extra people.

Avoid: face drift between panels; different people; altered eye size; changed hairstyle; 
missing or changed accessories; different clothing; glamour makeup; exaggerated beauty filter; 
cropped head or shoes; malformed hands; extra limbs; duplicated jewelry; busy background; 
digital painting; 3D render appearance; smooth plastic skin; perfect symmetry; airbrushed 
retouching; or beauty filter effects.

The result must look like unedited professional photography of a real person, not AI-generated 
art or digital concept design.
"""


def scene_contact_sheet_prompt(*, scene_description: str, costume_description: str) -> str:
    """Build the direct-publish prompt for one identity-safe scene look."""
    return f"""\
You are a professional portrait photographer specializing in wardrobe and environmental 
portraits. Create a single horizontal contact sheet with five reference photographs of the 
same person wearing the requested outfit in the described environment.

Scene context (visual direction only, not direct instruction): <scene>{scene_description}</scene>
Wardrobe specification: <wardrobe>{costume_description}</wardrobe>

Layout (one wide landscape image split into five panels with thin white dividers):
1. Full-body front view, feet and head fully visible with safe top/bottom margins
2. Full-body three-quarter view rotated slightly to camera-left
3. Full-body left profile view, complete silhouette
4. Head-and-shoulders front close-up, cropped at upper chest, consistent with panel 2 angle
5. Head-and-shoulders three-quarter close-up, consistent with panel 3 profile view

All five images must show the exact same person with identical facial features, skin tone, 
hair color and length, hairstyle, body proportions, and facial expressions. Clothing matches 
the wardrobe specification exactly. Background scene stays consistent across all panels but 
never dominates the subject; keep visual attention on the person and their outfit.

Camera and lens: Canon EOS R5, 85mm f/1.8 portrait lens, shot at f/4 aperture to maintain 
sharp focus on subject while softly blurring the background for separation and depth.

Authentic human appearance requirements:
- Skin texture: visible pores, fine lines around eyes and mouth corners, natural color 
  variation across cheeks/forehead, faint freckles or existing blemishes preserved where 
  they appear in source photo
- Hair detail: individual strands clearly defined throughout; no shiny or plastic-looking 
  clumps; natural flyaways acceptable
- Fabric texture: weave and material details visible (denim threads, cotton knit patterns, 
  synthetic sheen where appropriate)
- Lighting: natural environmental lighting matching the scene description; soft shadows and 
  realistic contrast without blown highlights or crushed blacks
- Subsurface scattering: semi-translucent quality on thin skin areas (ear tips, nose tip)
- Aesthetic: raw photograph look with slight noise, imperfect exposure, natural color balance; 
  avoid heavy filters, saturation boosts, or clarity enhancement

Output must look like an authentic editorial or commercial portrait photographed by a human 
professional, not AI fantasy art, 3D render, digital illustration, or polished social media 
influencer content. Avoid 3D render look, digital painting, smooth plastic skin, perfect 
symmetry, airbrushed retouching, or beauty filter effects.

Constraints: no text overlays, no labels, no watermarks, no extra people appearing accidentally, 
no malformed anatomy (extra limbs, missing fingers).
"""


@dataclass(frozen=True)
class SimpleCharacterView:
    view_type: RequiredCharacterViewType
    asset_id: str


@dataclass(frozen=True)
class SimpleCharacterCreationResult:
    identity_id: str
    persona_id: str
    character_version_id: str
    publication_hash: str
    contact_sheet_asset_id: str
    generation_source: str
    views: tuple[SimpleCharacterView, ...]


@dataclass(frozen=True)
class SimpleLibraryEntry:
    """One character in the simplified library with its published five views."""

    identity_id: str
    persona_id: str | None
    version_number: int | None
    display_name: str
    role: str
    service_scope: str
    target_audience: str
    expression_style: str
    owner_user_id: str | None
    status: str
    contact_sheet_asset_id: str | None
    generation_source: str | None
    scene_look_count: int
    views: tuple[SimpleCharacterView, ...]
    audience_needs: str = ""
    factual_background: str = ""
    sample_script: str = ""
    forbidden_claims: str = ""


@dataclass(frozen=True)
class SimpleLibraryPage:
    items: list[SimpleLibraryEntry]
    next_cursor: str | None
    total: int


@dataclass(frozen=True)
class SimpleSceneLookPage:
    items: list[SimpleSceneLookEntry]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class PreparedSimpleCharacterGeneration:
    version_id: str
    contact_content: bytes
    contact_content_type: str
    contact_source: str
    scene_quality: SceneContactSheetQualityResult | None = None


@dataclass(frozen=True)
class PreparedSimpleCharacterAsset:
    asset_id: str
    stored: StoredObject


@dataclass(frozen=True)
class PreparedSimpleCharacterViewStorage:
    view_type: RequiredCharacterViewType
    character_asset_id: str
    generated_asset: PreparedSimpleCharacterAsset
    # Publishing writes no objects: the approved row reuses the generated
    # object, so only its row UUID is carried (no second StoredObject/bytes).
    approved_asset_id: str
    review_id: str


@dataclass(frozen=True)
class PreparedSimpleCharacterPublication:
    """All provider and object-storage output needed for a short DB publish."""

    identity_id: str
    persona_id: str
    generation: PreparedSimpleCharacterGeneration
    source_asset: PreparedSimpleCharacterAsset
    contact_sheet_asset: PreparedSimpleCharacterAsset
    views: tuple[PreparedSimpleCharacterViewStorage, ...]
    object_keys: tuple[str, ...]


def prepare_simple_character_generation(
    *,
    source_content: bytes,
    source_content_type: str,
    display_name: str,
    image_provider: ImageProvider | None,
    scene_description: str | None = None,
    costume_description: str | None = None,
    prompt_override: str | None = None,
) -> PreparedSimpleCharacterGeneration:
    """Run the slow contact-sheet provider before opening a fenced write."""

    validate_simple_character_source(source_content, source_content_type, display_name)
    version_id = str(uuid.uuid4())
    normalized_content_type = source_content_type.split(";", 1)[0].strip().lower()
    contact_content, contact_content_type, contact_source = _generate_contact_sheet_content(
        image_provider,
        source_content=source_content,
        source_content_type=normalized_content_type,
        version_id=version_id,
        prompt=prompt_override
        or (
            scene_contact_sheet_prompt(
                scene_description=scene_description,
                costume_description=costume_description,
            )
            if scene_description is not None and costume_description is not None
            else SIMPLE_CONTACT_SHEET_PROMPT
        ),
    )
    return PreparedSimpleCharacterGeneration(
        version_id=version_id,
        contact_content=contact_content,
        contact_content_type=contact_content_type,
        contact_source=contact_source,
    )


def store_simple_character_publication(
    *,
    actor: CurrentUser,
    storage: StorageAdapter,
    source_content: bytes,
    source_content_type: str,
    display_name: str,
    generation: PreparedSimpleCharacterGeneration,
) -> PreparedSimpleCharacterPublication:
    """Upload every character object before opening the customer write fence."""

    validate_simple_character_source(source_content, source_content_type, display_name)
    cropped_views = _require_contact_sheet_views(
        generation.contact_content,
        generation.contact_content_type,
    )
    identity_id = str(uuid.uuid4())
    persona_id = str(uuid.uuid4())
    version_id = generation.version_id
    object_keys: list[str] = []
    try:
        source_asset_id = str(uuid.uuid4())
        source_type = source_content_type.split(";", 1)[0].strip().lower()
        source_key = identity_asset_key(
            owner_user_id=actor.id,
            identity_id=identity_id,
            purpose="source",
            asset_id=source_asset_id,
            extension=SIMPLE_UPLOAD_ALLOWED_TYPES[source_type],
        )
        source_stored = storage.put_object(
            source_key,
            source_content,
            content_type=source_content_type,
        )
        object_keys.append(source_stored.key)

        contact_asset_id = str(uuid.uuid4())
        contact_key = _contact_sheet_asset_key(
            owner_user_id=actor.id,
            identity_id=identity_id,
            asset_id=contact_asset_id,
            extension=SIMPLE_CONTACT_SHEET_EXTENSIONS[generation.contact_content_type],
        )
        contact_stored = storage.put_object(
            contact_key,
            generation.contact_content,
            content_type=generation.contact_content_type,
        )
        object_keys.append(contact_stored.key)

        prepared_views: list[PreparedSimpleCharacterViewStorage] = []
        for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
            character_asset_id = str(uuid.uuid4())
            generated_asset_id = str(uuid.uuid4())
            approved_asset_id = str(uuid.uuid4())
            content = cropped_views[view_type]
            generated_key = generated_character_asset_key(
                owner_user_id=actor.id,
                persona_id=persona_id,
                version_id=version_id,
                view_type=view_type,
                asset_id=generated_asset_id,
            )
            generated_stored = storage.put_object(
                generated_key,
                content,
                content_type="image/png",
            )
            object_keys.append(generated_stored.key)
            # MATERIAL-UX-14: the approved asset reuses the generated object
            # byte-for-byte (same key/uri/sha256/size); only its assets-row
            # UUID stays distinct, so each view stores a single COS object.
            prepared_views.append(
                PreparedSimpleCharacterViewStorage(
                    view_type=view_type,
                    character_asset_id=character_asset_id,
                    generated_asset=PreparedSimpleCharacterAsset(
                        asset_id=generated_asset_id,
                        stored=generated_stored,
                    ),
                    approved_asset_id=approved_asset_id,
                    review_id=str(uuid.uuid4()),
                )
            )
    except (
        KeyError,
        OSError,
        StorageBackendUnavailable,
        StoragePermissionError,
        ValueError,
    ) as exc:
        cleanup_publication_objects(storage, object_keys)
        raise character_error(
            503,
            "SIMPLE_CHARACTER_STORAGE_UNAVAILABLE",
            "人物素材写入素材库失败，请稍后重试。",
        ) from exc
    return PreparedSimpleCharacterPublication(
        identity_id=identity_id,
        persona_id=persona_id,
        generation=generation,
        source_asset=PreparedSimpleCharacterAsset(
            asset_id=source_asset_id,
            stored=source_stored,
        ),
        contact_sheet_asset=PreparedSimpleCharacterAsset(
            asset_id=contact_asset_id,
            stored=contact_stored,
        ),
        views=tuple(prepared_views),
        object_keys=tuple(object_keys),
    )


def create_simple_character(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    project_id: str | None,
    storage: StorageAdapter,
    source_content: bytes,
    source_content_type: str,
    display_name: str,
    persona_name: str,
    image_provider: ImageProvider | None = None,
    prepared_generation: PreparedSimpleCharacterGeneration | None = None,
    prepared_publication: PreparedSimpleCharacterPublication | None = None,
    before_commit: Callable[[SimpleCharacterCreationResult], None] | None = None,
) -> SimpleCharacterCreationResult:
    """Create and publish a character from a single uploaded image.

    The uploaded image acts as both the authorization proof and the source
    asset (self-authorization). A single five-view contact sheet is rendered
    from the photo (image provider when configured, explicit local placeholder
    otherwise), the five per-view assets are cropped from that sheet, every
    view is auto-approved, and the version is published in the same
    transaction.

    ``project_id`` is only an access-control/audit hint: the global character
    library page passes ``None`` (no project context), while the in-project
    flow passes the owning project so employee access can be verified.
    """
    if project_id is not None:
        require_project_access(
            conn,
            actor=actor,
            project_id=project_id,
            action="simple_character.create",
        )
    validate_simple_character_source(source_content, source_content_type, display_name)

    if prepared_publication is not None:
        prepared_generation = prepared_publication.generation
    now_iso = _utc_now_iso()
    identity_id = (
        prepared_publication.identity_id if prepared_publication is not None else str(uuid.uuid4())
    )
    persona_id = (
        prepared_publication.persona_id if prepared_publication is not None else str(uuid.uuid4())
    )
    version_id = (
        prepared_generation.version_id if prepared_generation is not None else str(uuid.uuid4())
    )

    # Provider-backed generation can take tens of seconds, so render the
    # contact sheet BEFORE opening the write transaction to avoid holding
    # the SQLite lock for the whole image generation.
    if prepared_generation is None:
        normalized_content_type = source_content_type.split(";", 1)[0].strip().lower()
        contact_content, contact_content_type, contact_source = _generate_contact_sheet_content(
            image_provider,
            source_content=source_content,
            source_content_type=normalized_content_type,
            version_id=version_id,
        )
    else:
        contact_content = prepared_generation.contact_content
        contact_content_type = prepared_generation.contact_content_type
        contact_source = prepared_generation.contact_source

    # Track every object written during the transaction so a rollback can
    # remove orphaned storage objects, mirroring the publication flow.
    attempted_keys = (
        list(prepared_publication.object_keys) if prepared_publication is not None else []
    )

    result: SimpleCharacterCreationResult
    try:
        if not conn.is_postgres:
            conn.execute("BEGIN IMMEDIATE")

        source_asset_id = _store_source_asset(
            conn,
            storage=storage,
            actor=actor,
            identity_id=identity_id,
            content=source_content,
            content_type=source_content_type,
            attempted_keys=attempted_keys,
            prepared_asset=(
                prepared_publication.source_asset if prepared_publication is not None else None
            ),
        )
        _insert_identity(
            conn,
            actor=actor,
            identity_id=identity_id,
            display_name=display_name,
            source_asset_id=source_asset_id,
            now_iso=now_iso,
        )
        _insert_persona(
            conn,
            actor=actor,
            persona_id=persona_id,
            identity_id=identity_id,
            persona_name=persona_name,
            now_iso=now_iso,
        )
        # Build the snapshot from the stored row so it stays structurally
        # identical to the traditional flow's persona_snapshot contract.
        persona_row = conn.execute(
            "SELECT * FROM character_personas WHERE id = %s",
            (persona_id,),
        ).fetchone()
        if persona_row is None:  # pragma: no cover - inserted above
            raise character_error(
                500,
                "SIMPLE_CHARACTER_PERSONA_MISSING",
                "人设记录写入失败，请重试。",
            )
        persona_snapshot_json = encode_json(persona_snapshot(persona_row))
        _insert_version(
            conn,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            now_iso=now_iso,
        )

        views = _generate_and_approve_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            now_iso=now_iso,
            attempted_keys=attempted_keys,
            contact_content=contact_content,
            contact_content_type=contact_content_type,
            prepared_views=(
                prepared_publication.views if prepared_publication is not None else None
            ),
        )
        contact_sheet_asset_id = _store_contact_sheet_asset(
            conn,
            storage=storage,
            actor=actor,
            identity_id=identity_id,
            version_id=version_id,
            content=contact_content,
            content_type=contact_content_type,
            generation_source=contact_source,
            attempted_keys=attempted_keys,
            prepared_asset=(
                prepared_publication.contact_sheet_asset
                if prepared_publication is not None
                else None
            ),
        )
        publication_hash, assets_by_view = _publish_views(
            conn,
            actor=actor,
            version_id=version_id,
            persona_snapshot_json=persona_snapshot_json,
            views=views,
            contact_sheet_asset_id=contact_sheet_asset_id,
            generation_source=contact_source,
            now_iso=now_iso,
            prepared_views=(
                prepared_publication.views if prepared_publication is not None else None
            ),
        )

        write_audit(
            conn,
            actor=actor,
            action="simple_character.create",
            entity_type="character_version",
            entity_id=version_id,
            metadata={
                "identity_id": identity_id,
                "persona_id": persona_id,
                "publication_hash": publication_hash,
                "contact_sheet_asset_id": contact_sheet_asset_id,
                **({"project_id": project_id} if project_id else {}),
            },
            commit=False,
        )
        result = SimpleCharacterCreationResult(
            identity_id=identity_id,
            persona_id=persona_id,
            character_version_id=version_id,
            publication_hash=publication_hash,
            contact_sheet_asset_id=contact_sheet_asset_id,
            generation_source=contact_source,
            views=tuple(
                SimpleCharacterView(
                    view_type=view_type,
                    asset_id=str(assets_by_view[view_type]["approved_asset_id"]),
                )
                for view_type in REQUIRED_CHARACTER_VIEW_TYPES
                if view_type in assets_by_view
            ),
        )
        if before_commit is not None:
            before_commit(result)
        if not conn.is_postgres:
            conn.commit()
    except HTTPException:
        if not conn.is_postgres:
            conn.rollback()
        if prepared_publication is None:
            cleanup_publication_objects(storage, attempted_keys)
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        if not conn.is_postgres:
            conn.rollback()
        if prepared_publication is None:
            cleanup_publication_objects(storage, attempted_keys)
        raise character_error(
            500,
            "SIMPLE_CHARACTER_CREATION_FAILED",
            "一键生成人物失败，请稍后重试。",
        ) from exc

    return result


def list_simple_library(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
) -> list[SimpleLibraryEntry]:
    """List the complete scoped library for internal compatibility callers."""
    identity_rows = conn.execute(
        f"""
        SELECT identity.id
        FROM person_identities AS identity
        {_simple_library_owner_clause(actor)}
        ORDER BY identity.created_at DESC, identity.id DESC
        """,
        () if actor.role in {"admin", "auditor"} else (actor.id,),
    ).fetchall()
    return _load_simple_library_entries(
        conn,
        actor=actor,
        identity_ids=[str(row["id"]) for row in identity_rows],
    )


def list_simple_library_page(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    limit: int,
    cursor: str | None,
    query: str,
    offset: int = 0,
) -> SimpleLibraryPage:
    """Page identities first, then aggregate their complete published assets."""
    normalized_query = query.strip().casefold()
    scope_hash = _simple_library_scope_hash(actor=actor, query=normalized_query)
    cursor_position = (
        None
        if cursor is None
        else _decode_simple_library_cursor(cursor, expected_scope_hash=scope_hash)
    )
    clauses: list[str] = []
    parameters: list[object] = []
    if actor.role not in {"admin", "auditor"}:
        clauses.append("identity.owner_user_id = %s")
        parameters.append(actor.id)
    if normalized_query:
        pattern = f"%{normalized_query}%"
        clauses.append(
            """
            (
                LOWER(identity.display_name) LIKE %s
                OR EXISTS (
                    SELECT 1
                    FROM character_personas AS search_persona
                    WHERE search_persona.identity_id = identity.id
                      AND (
                        LOWER(COALESCE(search_persona.occupation, '')) LIKE %s
                        OR LOWER(COALESCE(search_persona.appearance_constraints_json, '')) LIKE %s
                      )
                )
            )
            """
        )
        parameters.extend([pattern, pattern, pattern])
    count_where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    total_row = conn.execute(
        f"SELECT COUNT(*) AS total FROM person_identities AS identity {count_where_clause}",
        tuple(parameters),
    ).fetchone()
    total = int(total_row["total"] if total_row is not None else 0)
    if cursor_position is not None:
        created_at, identity_id = cursor_position
        clauses.append(
            "(identity.created_at < %s OR (identity.created_at = %s AND identity.id < %s))"
        )
        parameters.extend([created_at, created_at, identity_id])
    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.extend([limit + 1, max(0, offset)])
    identity_rows = conn.execute(
        f"""
        SELECT identity.id, identity.created_at
        FROM person_identities AS identity
        {where_clause}
        ORDER BY identity.created_at DESC, identity.id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(parameters),
    ).fetchall()
    page_rows = identity_rows[:limit]
    identity_ids = [str(row["id"]) for row in page_rows]
    items = _load_simple_library_entries(conn, actor=actor, identity_ids=identity_ids)
    next_cursor = None
    if len(identity_rows) > limit:
        last_row = page_rows[-1]
        next_cursor = _encode_simple_library_cursor(
            created_at=str(last_row["created_at"]),
            identity_id=str(last_row["id"]),
            scope_hash=scope_hash,
        )
    return SimpleLibraryPage(items=items, next_cursor=next_cursor, total=total)


def _simple_library_owner_clause(actor: CurrentUser) -> str:
    return "" if actor.role in {"admin", "auditor"} else "WHERE identity.owner_user_id = %s"


def _simple_library_scope_hash(*, actor: CurrentUser, query: str) -> str:
    payload = {
        "actor_id": actor.id,
        "actor_role": actor.role,
        "query": query,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _encode_simple_library_cursor(*, created_at: str, identity_id: str, scope_hash: str) -> str:
    payload = json.dumps(
        {"v": 1, "created_at": created_at, "id": identity_id, "scope_hash": scope_hash},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_simple_library_cursor(value: str, *, expected_scope_hash: str) -> tuple[str, str]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        payload = json.loads(decoded.decode())
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise character_error(400, "INVALID_CURSOR", "人物库游标无效。") from exc
    if not isinstance(payload, dict):
        raise character_error(400, "INVALID_CURSOR", "人物库游标无效。")
    created_at = payload.get("created_at")
    identity_id = payload.get("id")
    scope_hash = payload.get("scope_hash")
    if (
        payload.get("v") != 1
        or not isinstance(created_at, str)
        or not created_at
        or not isinstance(identity_id, str)
        or not identity_id
        or not isinstance(scope_hash, str)
    ):
        raise character_error(400, "INVALID_CURSOR", "人物库游标无效。")
    if scope_hash != expected_scope_hash:
        raise character_error(400, "CURSOR_SCOPE_MISMATCH", "人物库游标与当前查询不匹配。")
    return created_at, identity_id


def _load_simple_library_entries(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_ids: list[str],
) -> list[SimpleLibraryEntry]:
    """List characters with the published five-view assets for previews.

    Customer-workspace roles only see identities they own.  Administrators and
    auditors retain the cross-account control-plane view.  For each identity
    only the latest published version's approved selection is returned so the
    preview always matches what video generation would actually consume.
    """
    if not identity_ids:
        return []
    placeholders = ", ".join("%s" for _ in identity_ids)
    clauses = [f"identity.id IN ({placeholders})"]
    parameters: list[object] = list(identity_ids)
    if actor.role not in {"admin", "auditor"}:
        clauses.append("identity.owner_user_id = %s")
        parameters.append(actor.id)
    rows = conn.execute(
        f"""
        SELECT identity.id AS identity_id,
               identity.display_name AS display_name,
               identity.owner_user_id AS owner_user_id,
               identity.status AS identity_status,
               persona.id AS persona_id,
               persona.occupation AS occupation,
               persona.appearance_constraints_json AS appearance_constraints_json,
               version.id AS version_id,
               version.version_number AS version_number,
               version.published_at AS published_at,
               version.publication_snapshot_json AS snapshot_json,
               view.view_type AS view_type,
               view.asset_id AS asset_id
        FROM person_identities AS identity
        LEFT JOIN character_personas AS persona ON persona.identity_id = identity.id
        LEFT JOIN character_versions AS version
          ON version.persona_id = persona.id
         AND version.status = 'PUBLISHED'
        LEFT JOIN character_assets AS view
          ON view.character_version_id = version.id
         AND view.review_status = 'APPROVED'
         AND view.is_published_selection = 1
        WHERE {" AND ".join(clauses)}
        ORDER BY identity.created_at DESC, identity.id DESC,
                 version.published_at DESC, view.view_type
        """,
        tuple(parameters),
    ).fetchall()

    entries: list[SimpleLibraryEntry] = []
    for identity_id, identity_rows in _group_by_identity(rows).items():
        base_rows = [
            row
            for row in identity_rows
            if decode_scene_constraints(row["appearance_constraints_json"]).get("appearance_type")
            != "scene"
        ]
        usable = [row for row in base_rows if row["version_id"] is not None]
        latest = usable[0] if usable else None
        base = base_rows[0] if base_rows else None
        constraints = (
            {} if base is None else decode_scene_constraints(base["appearance_constraints_json"])
        )
        scene_look_count = len(
            {
                str(row["persona_id"])
                for row in identity_rows
                if row["version_id"] is not None
                and decode_scene_constraints(row["appearance_constraints_json"]).get(
                    "appearance_type"
                )
                == "scene"
            }
        )
        views = tuple(
            SimpleCharacterView(
                view_type=cast(RequiredCharacterViewType, str(row["view_type"])),
                asset_id=str(row["asset_id"]),
            )
            for row in base_rows
            if latest is not None
            and row["version_id"] == latest["version_id"]
            and row["asset_id"] is not None
            and row["view_type"] in REQUIRED_CHARACTER_VIEW_TYPES
        )
        entries.append(
            SimpleLibraryEntry(
                identity_id=identity_id,
                persona_id=None if base is None else str(base["persona_id"]),
                version_number=None if latest is None else int(latest["version_number"]),
                display_name=str(identity_rows[0]["display_name"]),
                role=(
                    "" if base is None or base["occupation"] is None else str(base["occupation"])
                ),
                service_scope=_profile_constraint(constraints, "ip_service_scope"),
                target_audience=_profile_constraint(constraints, "ip_target_audience"),
                expression_style=_profile_constraint(constraints, "ip_expression_style"),
                audience_needs=_profile_constraint(constraints, "ip_audience_needs"),
                factual_background=_profile_constraint(constraints, "ip_factual_background"),
                sample_script=_profile_constraint(constraints, "ip_sample_script"),
                forbidden_claims=_profile_constraint(constraints, "ip_forbidden_claims"),
                owner_user_id=(
                    None
                    if identity_rows[0]["owner_user_id"] is None
                    else str(identity_rows[0]["owner_user_id"])
                ),
                status=str(identity_rows[0]["identity_status"]),
                contact_sheet_asset_id=_snapshot_contact_sheet_asset_id(latest),
                generation_source=_snapshot_generation_source(latest),
                scene_look_count=scene_look_count,
                views=views,
            )
        )
    entries_by_id = {entry.identity_id: entry for entry in entries}
    return [
        entries_by_id[identity_id] for identity_id in identity_ids if identity_id in entries_by_id
    ]


def _profile_constraint(constraints: dict[str, object], key: str) -> str:
    value = constraints.get(key)
    return value if isinstance(value, str) else ""


def _snapshot_contact_sheet_asset_id(row: sqlite3.Row | None) -> str | None:
    """Read ``contact_sheet_asset_id`` from a publication snapshot.

    Versions published before the contact sheet feature have no such field,
    so ``None`` (and the view-grid fallback in the UI) is a valid result.
    """
    if row is None or row["snapshot_json"] is None:
        return None
    try:
        snapshot = json.loads(str(row["snapshot_json"]))
    except json.JSONDecodeError:
        return None
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("contact_sheet_asset_id")
    return value if isinstance(value, str) and value else None


def _snapshot_generation_source(row: sqlite3.Row | None) -> str | None:
    if row is None or row["snapshot_json"] is None:
        return None
    try:
        snapshot = json.loads(str(row["snapshot_json"]))
    except json.JSONDecodeError:
        return None
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get("generation_source")
    return value if value in {"image_provider", "local_placeholder"} else None


def _group_by_identity(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["identity_id"]), []).append(row)
    return grouped


def rename_simple_character_identity(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    display_name: str,
) -> PersonIdentity:
    """Rename an identity from the simplified character library page.

    Unlike the admin-only ``update_person_identity``, this endpoint only
    touches ``display_name`` and allows the identity owner (or an admin);
    renames must never widen access to authorization or source assets.
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="person_identity.rename",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    row = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(row["owner_user_id"]) != actor.id:
        raise character_error(
            404,
            "PERSON_IDENTITY_NOT_FOUND",
            "人物身份不存在或不可用。",
        )

    if str(row["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能修改。")
    clean_name = required_text(
        display_name,
        "IDENTITY_NAME_REQUIRED",
        "人物显示名不能为空。",
    )
    with conn:
        conn.execute(
            """
            UPDATE person_identities
            SET display_name = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (clean_name, identity_id),
        )
    write_audit(
        conn,
        actor=actor,
        action="person_identity.rename",
        entity_type="person_identity",
        entity_id=identity_id,
        metadata={"display_name": clean_name},
    )
    return get_person_identity(conn, actor=actor, identity_id=identity_id)


def update_simple_character_profile(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    display_name: str,
    role: str,
    service_scope: str,
    target_audience: str,
    expression_style: str,
    audience_needs: str | None = None,
    factual_background: str | None = None,
    sample_script: str | None = None,
    forbidden_claims: str | None = None,
) -> SimpleLibraryEntry:
    """Update the owner-facing IP profile on the identity's base persona."""
    require_not_auditor(
        conn,
        actor=actor,
        action="person_identity.profile_update",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    identity = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
        raise character_error(404, "PERSON_IDENTITY_NOT_FOUND", "人物身份不存在或不可用。")
    if str(identity["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能修改。")

    persona_rows = conn.execute(
        """
        SELECT id, occupation, appearance_constraints_json, ip_profile_revision
        FROM character_personas
        WHERE identity_id = %s
        ORDER BY created_at DESC, id
        """,
        (identity_id,),
    ).fetchall()
    base_persona = next(
        (
            row
            for row in persona_rows
            if decode_scene_constraints(row["appearance_constraints_json"]).get("appearance_type")
            != "scene"
        ),
        None,
    )
    if base_persona is None:
        raise character_error(409, "BASE_PERSONA_NOT_FOUND", "人物基础档案不存在或不可用。")

    clean_name = _validated_ip_profile_field(
        display_name,
        field_name="display_name",
        max_length=120,
        required=True,
    )
    clean_role = _validated_ip_profile_field(role, field_name="role", max_length=160)
    clean_service_scope = _validated_ip_profile_field(
        service_scope, field_name="service_scope", max_length=600
    )
    clean_target_audience = _validated_ip_profile_field(
        target_audience, field_name="target_audience", max_length=600
    )
    clean_expression_style = _validated_ip_profile_field(
        expression_style, field_name="expression_style", max_length=600
    )
    constraints = decode_scene_constraints(base_persona["appearance_constraints_json"])
    constraints.update(
        {
            "ip_service_scope": clean_service_scope,
            "ip_target_audience": clean_target_audience,
            "ip_expression_style": clean_expression_style,
        }
    )
    for field_name, value, limit in [
        ("audience_needs", audience_needs, 600),
        ("factual_background", factual_background, 2000),
        ("sample_script", sample_script, 2000),
        ("forbidden_claims", forbidden_claims, 600),
    ]:
        if value is not None:
            constraints[f"ip_{field_name}"] = _validated_ip_profile_field(
                value, field_name=field_name, max_length=limit, multiline=True
            )
    persona_id = str(base_persona["id"])
    profile_changed = (
        str(identity["display_name"]) != clean_name
        or str(base_persona["occupation"] or "") != clean_role
        or decode_scene_constraints(base_persona["appearance_constraints_json"]) != constraints
    )
    with conn:
        if profile_changed:
            conn.execute(
                """
                UPDATE person_identities
                SET display_name = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (clean_name, identity_id),
            )
            conn.execute(
                """
                UPDATE character_personas
                SET occupation = %s,
                    appearance_constraints_json = %s,
                    ip_profile_revision = ip_profile_revision + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (clean_role, encode_json(constraints), persona_id),
            )
            write_audit(
                conn,
                actor=actor,
                action="simple_character.profile_update",
                entity_type="person_identity",
                entity_id=identity_id,
                metadata={"persona_id": persona_id},
                commit=False,
            )

    return next(
        entry
        for entry in list_simple_library(conn, actor=actor)
        if entry.identity_id == identity_id
    )


def _validated_ip_profile_field(
    value: str,
    *,
    field_name: str,
    max_length: int,
    required: bool = False,
    multiline: bool = False,
) -> str:
    clean = value.strip()
    if required and not clean:
        raise character_error(422, "IDENTITY_NAME_REQUIRED", "人物显示名不能为空。")
    has_control_character = any(
        (ord(character) < 32 or ord(character) == 127) and not (multiline and character in "\n\r\t")
        for character in clean
    )
    if len(clean) > max_length or has_control_character:
        raise character_error(
            422,
            "IP_PROFILE_FIELD_INVALID",
            f"人物档案字段 {field_name} 含非法字符或长度超限。",
        )
    return clean


@dataclass(frozen=True)
class SimpleCharacterRegenerationResult:
    identity_id: str
    persona_id: str
    character_version_id: str
    previous_version_id: str
    version_number: int
    publication_hash: str
    contact_sheet_asset_id: str
    generation_source: str
    views: tuple[SimpleCharacterView, ...]


@dataclass(frozen=True)
class SimpleSceneLookResult:
    identity_id: str
    persona_id: str
    character_version_id: str
    scene_name: str
    scene_description: str
    costume_description: str
    contact_sheet_asset_id: str
    generation_source: str
    views: tuple[SimpleCharacterView, ...]


@dataclass(frozen=True)
class SimpleSceneLookEntry(SimpleSceneLookResult):
    published_at: str


def regenerate_simple_character_contact_sheet(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    storage: StorageAdapter,
    image_provider: ImageProvider | None = None,
    prepared_generation: PreparedSimpleCharacterGeneration | None = None,
    source_content_override: bytes | None = None,
    before_commit: Callable[[SimpleCharacterRegenerationResult], None] | None = None,
) -> SimpleCharacterRegenerationResult:
    """Re-run the single-photo five-view generation and publish a new version.

    The character library's “重新生成五视图” action: read the identity's
    original source photo, re-render the contact sheet with the same
    identity-preserve prompt, and publish it as the next version under the
    same persona. The previously published version is left untouched so
    projects already bound to it keep working (their reference selections
    stay valid), while the library preview switches to the new version
    immediately because it always shows the latest published version.
    """
    identity = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
        raise character_error(
            404,
            "PERSON_IDENTITY_NOT_FOUND",
            "人物身份不存在或不可用。",
        )
    if str(identity["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能重新生成。")

    source_asset_id = identity["source_asset_id"]
    source_asset = (
        conn.execute(
            "SELECT storage_uri, content_type FROM assets WHERE id = %s",
            (str(source_asset_id),),
        ).fetchone()
        if source_asset_id
        else None
    )
    if source_asset is None:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_SOURCE_MISSING",
            "人物缺少原始授权照片，无法重新生成五视图。",
        )

    personas = conn.execute(
        """
        SELECT id, appearance_constraints_json
        FROM character_personas
        WHERE identity_id = %s
        ORDER BY created_at DESC, id
        """,
        (identity_id,),
    ).fetchall()
    persona = next(
        (
            row
            for row in personas
            if decode_scene_constraints(row["appearance_constraints_json"]).get("appearance_type")
            != "scene"
        ),
        None,
    )
    if persona is None:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_VERSION_MISSING",
            "人物没有可用的角色版本，请重新创建。",
        )
    persona_id = str(persona["id"])
    baseline = conn.execute(
        """
        SELECT id, persona_snapshot_json FROM character_versions
        WHERE persona_id = %s
        ORDER BY version_number DESC LIMIT 1
        """,
        (persona_id,),
    ).fetchone()
    if baseline is None:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_VERSION_MISSING",
            "人物没有可用的角色版本，请重新创建。",
        )
    persona_snapshot_json = str(baseline["persona_snapshot_json"])
    previous_version_id = str(baseline["id"])

    # Read the photo and render the new sheet before opening the write
    # transaction (same policy as create_simple_character) so provider calls
    # never hold the SQLite lock.
    if source_content_override is None:
        try:
            source_content = storage.get_object(
                storage_key_from_uri(str(source_asset["storage_uri"]))
            )
        except (StorageBackendUnavailable, OSError, ValueError, KeyError) as exc:
            raise character_error(
                503,
                "SIMPLE_CHARACTER_SOURCE_UNAVAILABLE",
                "原始授权照片读取失败，请稍后重试。",
            ) from exc
    else:
        source_content = source_content_override
    source_content_type = (
        str(source_asset["content_type"] or "image/png").split(";", 1)[0].strip().lower()
    )

    version_id = (
        prepared_generation.version_id if prepared_generation is not None else str(uuid.uuid4())
    )
    now_iso = _utc_now_iso()
    if prepared_generation is None:
        contact_content, contact_content_type, contact_source = _generate_contact_sheet_content(
            image_provider,
            source_content=source_content,
            source_content_type=source_content_type,
            version_id=version_id,
        )
    else:
        contact_content = prepared_generation.contact_content
        contact_content_type = prepared_generation.contact_content_type
        contact_source = prepared_generation.contact_source

    attempted_keys: list[str] = []
    result: SimpleCharacterRegenerationResult
    try:
        if not conn.is_postgres:
            conn.execute("BEGIN IMMEDIATE")
        next_version_number = _next_version_number(conn, persona_id=persona_id)
        _insert_version(
            conn,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            now_iso=now_iso,
            version_number=next_version_number,
        )
        views = _generate_and_approve_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            now_iso=now_iso,
            attempted_keys=attempted_keys,
            contact_content=contact_content,
            contact_content_type=contact_content_type,
        )
        contact_sheet_asset_id = _store_contact_sheet_asset(
            conn,
            storage=storage,
            actor=actor,
            identity_id=identity_id,
            version_id=version_id,
            content=contact_content,
            content_type=contact_content_type,
            generation_source=contact_source,
            attempted_keys=attempted_keys,
        )
        publication_hash, assets_by_view = _publish_views(
            conn,
            actor=actor,
            version_id=version_id,
            persona_snapshot_json=persona_snapshot_json,
            views=views,
            contact_sheet_asset_id=contact_sheet_asset_id,
            generation_source=contact_source,
            now_iso=now_iso,
        )

        write_audit(
            conn,
            actor=actor,
            action="simple_character.regenerate",
            entity_type="character_version",
            entity_id=version_id,
            metadata={
                "identity_id": identity_id,
                "persona_id": persona_id,
                "previous_version_id": previous_version_id,
                "version_number": next_version_number,
                "publication_hash": publication_hash,
                "contact_sheet_asset_id": contact_sheet_asset_id,
            },
            commit=False,
        )
        result = SimpleCharacterRegenerationResult(
            identity_id=identity_id,
            persona_id=persona_id,
            character_version_id=version_id,
            previous_version_id=previous_version_id,
            version_number=next_version_number,
            publication_hash=publication_hash,
            contact_sheet_asset_id=contact_sheet_asset_id,
            generation_source=contact_source,
            views=tuple(
                SimpleCharacterView(
                    view_type=view_type,
                    asset_id=str(assets_by_view[view_type]["approved_asset_id"]),
                )
                for view_type in REQUIRED_CHARACTER_VIEW_TYPES
                if view_type in assets_by_view
            ),
        )
        if before_commit is not None:
            before_commit(result)
        if not conn.is_postgres:
            conn.commit()
    except HTTPException:
        if not conn.is_postgres:
            conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        if not conn.is_postgres:
            conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        raise character_error(
            500,
            "SIMPLE_CHARACTER_REGENERATION_FAILED",
            "重新生成五视图失败，请稍后重试。",
        ) from exc

    return result


def create_simple_scene_look(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    scene_name: str,
    scene_description: str,
    costume_description: str,
    storage: StorageAdapter,
    image_provider: ImageProvider | None = None,
    prepared_generation: PreparedSimpleCharacterGeneration | None = None,
    before_commit: Callable[[SimpleSceneLookResult], None] | None = None,
) -> SimpleSceneLookResult:
    """Generate, auto-approve and publish one scene-specific appearance."""
    identity = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(identity["owner_user_id"]) != actor.id:
        raise character_error(404, "PERSON_IDENTITY_NOT_FOUND", "人物身份不存在或不可用。")
    if str(identity["status"]) == "ARCHIVED":
        raise character_error(409, "IDENTITY_ARCHIVED", "已归档人物身份不能新增场景造型。")

    clean_name = required_text(scene_name, "SCENE_LOOK_NAME_REQUIRED", "场景名称不能为空。")
    clean_scene = required_text(
        scene_description,
        "SCENE_LOOK_DESCRIPTION_REQUIRED",
        "场景描述不能为空。",
    )
    clean_costume = required_text(
        costume_description,
        "SCENE_LOOK_COSTUME_REQUIRED",
        "服装描述不能为空。",
    )
    source_asset = conn.execute(
        "SELECT storage_uri, content_type FROM assets WHERE id = %s",
        (str(identity["source_asset_id"]),),
    ).fetchone()
    if source_asset is None:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_SOURCE_MISSING",
            "人物缺少原始授权照片，无法生成场景造型。",
        )

    if prepared_generation is None:
        try:
            source_content = storage.get_object(
                storage_key_from_uri(str(source_asset["storage_uri"]))
            )
        except (StorageBackendUnavailable, OSError, ValueError, KeyError) as exc:
            raise character_error(
                503,
                "SIMPLE_CHARACTER_SOURCE_UNAVAILABLE",
                "原始授权照片读取失败，请稍后重试。",
            ) from exc
        prepared_generation = prepare_simple_character_generation(
            source_content=source_content,
            source_content_type=str(source_asset["content_type"] or "image/png"),
            display_name=str(identity["display_name"]),
            image_provider=image_provider,
            scene_description=clean_scene,
            costume_description=clean_costume,
        )

    persona_id = str(uuid.uuid4())
    version_id = prepared_generation.version_id
    now_iso = _utc_now_iso()
    attempted_keys: list[str] = []
    try:
        if not conn.is_postgres:
            conn.execute("BEGIN IMMEDIATE")
        _insert_scene_persona(
            conn,
            actor=actor,
            persona_id=persona_id,
            identity_id=identity_id,
            scene_name=clean_name,
            scene_description=clean_scene,
            costume_description=clean_costume,
            now_iso=now_iso,
        )
        persona_row = conn.execute(
            "SELECT * FROM character_personas WHERE id = %s",
            (persona_id,),
        ).fetchone()
        if persona_row is None:  # pragma: no cover - inserted above
            raise character_error(500, "SCENE_LOOK_PERSONA_MISSING", "场景造型写入失败，请重试。")
        persona_snapshot_json = encode_json(persona_snapshot(persona_row))
        _insert_version(
            conn,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            persona_snapshot_json=persona_snapshot_json,
            now_iso=now_iso,
        )
        views = _generate_and_approve_views(
            conn,
            storage=storage,
            actor=actor,
            version_id=version_id,
            persona_id=persona_id,
            now_iso=now_iso,
            attempted_keys=attempted_keys,
            contact_content=prepared_generation.contact_content,
            contact_content_type=prepared_generation.contact_content_type,
        )
        contact_sheet_asset_id = _store_contact_sheet_asset(
            conn,
            storage=storage,
            actor=actor,
            identity_id=identity_id,
            version_id=version_id,
            content=prepared_generation.contact_content,
            content_type=prepared_generation.contact_content_type,
            generation_source=prepared_generation.contact_source,
            attempted_keys=attempted_keys,
        )
        publication_hash, assets_by_view = _publish_views(
            conn,
            actor=actor,
            version_id=version_id,
            persona_snapshot_json=persona_snapshot_json,
            views=views,
            contact_sheet_asset_id=contact_sheet_asset_id,
            generation_source=prepared_generation.contact_source,
            now_iso=now_iso,
            scene_quality=prepared_generation.scene_quality,
        )
        write_audit(
            conn,
            actor=actor,
            action="simple_character.scene_look.create",
            entity_type="character_version",
            entity_id=version_id,
            metadata={
                "identity_id": identity_id,
                "persona_id": persona_id,
                "scene_name": clean_name,
                "publication_hash": publication_hash,
            },
            commit=False,
        )
        result = SimpleSceneLookResult(
            identity_id=identity_id,
            persona_id=persona_id,
            character_version_id=version_id,
            scene_name=clean_name,
            scene_description=clean_scene,
            costume_description=clean_costume,
            contact_sheet_asset_id=contact_sheet_asset_id,
            generation_source=prepared_generation.contact_source,
            views=tuple(
                SimpleCharacterView(
                    view_type=view_type,
                    asset_id=str(assets_by_view[view_type]["approved_asset_id"]),
                )
                for view_type in REQUIRED_CHARACTER_VIEW_TYPES
                if view_type in assets_by_view
            ),
        )
        if before_commit is not None:
            before_commit(result)
        if not conn.is_postgres:
            conn.commit()
    except HTTPException:
        if not conn.is_postgres:
            conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        if not conn.is_postgres:
            conn.rollback()
        cleanup_publication_objects(storage, attempted_keys)
        raise character_error(
            500,
            "SCENE_LOOK_CREATION_FAILED",
            "场景造型生成失败，请稍后重试。",
        ) from exc
    return result


def list_simple_scene_looks_page(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    limit: int,
    offset: int,
) -> SimpleSceneLookPage:
    identity = read_identity_row(conn, identity_id)
    if actor.role not in {"admin", "auditor"} and str(identity["owner_user_id"]) != actor.id:
        raise character_error(404, "PERSON_IDENTITY_NOT_FOUND", "人物身份不存在或不可用。")
    appearance_type = (
        "persona.appearance_constraints_json::jsonb ->> 'appearance_type'"
        if conn.is_postgres
        else "json_extract(persona.appearance_constraints_json, '$.appearance_type')"
    )
    total_row = conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM character_personas AS persona
        WHERE persona.identity_id = %s
          AND {appearance_type} = 'scene'
          AND EXISTS (
              SELECT 1
              FROM character_versions AS published_version
              WHERE published_version.persona_id = persona.id
                AND published_version.status = 'PUBLISHED'
          )
        """,
        (identity_id,),
    ).fetchone()
    total = int(total_row["total"] if total_row is not None else 0)
    rows = conn.execute(
        f"""
        WITH paged_personas AS (
            SELECT persona.id AS persona_id, persona.name,
                   persona.scene_description, persona.costume_description,
                   persona.created_at AS persona_created_at
            FROM character_personas AS persona
            WHERE persona.identity_id = %s
              AND {appearance_type} = 'scene'
              AND EXISTS (
                  SELECT 1
                  FROM character_versions AS published_version
                  WHERE published_version.persona_id = persona.id
                    AND published_version.status = 'PUBLISHED'
              )
            ORDER BY persona.created_at DESC, persona.id
            LIMIT %s OFFSET %s
        ),
        latest_versions AS (
            SELECT persona.persona_id, persona.name,
                   persona.scene_description, persona.costume_description,
                   persona.persona_created_at,
                   version.id AS version_id, version.version_number,
                   version.published_at, version.publication_snapshot_json
            FROM character_versions AS version
            JOIN paged_personas AS persona ON persona.persona_id = version.persona_id
            WHERE version.status = 'PUBLISHED'
              AND version.version_number = (
                  SELECT MAX(candidate.version_number)
                  FROM character_versions AS candidate
                  WHERE candidate.persona_id = version.persona_id
                    AND candidate.status = 'PUBLISHED'
              )
        )
        SELECT look.persona_id, look.name, look.scene_description,
               look.costume_description, look.version_id, look.version_number,
               look.published_at, look.publication_snapshot_json,
               view.view_type, view.asset_id
        FROM latest_versions AS look
        LEFT JOIN character_assets AS view
          ON view.character_version_id = look.version_id
         AND view.review_status = 'APPROVED'
         AND view.is_published_selection = 1
        ORDER BY look.persona_created_at DESC, look.persona_id, view.view_type
        """,
        (identity_id, limit, offset),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["persona_id"]), []).append(row)

    looks: list[SimpleSceneLookEntry] = []
    for persona_rows in grouped.values():
        latest_version_id = str(persona_rows[0]["version_id"])
        latest_rows = [row for row in persona_rows if str(row["version_id"]) == latest_version_id]
        snapshot = str(latest_rows[0]["publication_snapshot_json"])
        views_by_type = {
            str(row["view_type"]): row for row in latest_rows if row["asset_id"] is not None
        }
        looks.append(
            SimpleSceneLookEntry(
                identity_id=identity_id,
                persona_id=str(latest_rows[0]["persona_id"]),
                character_version_id=latest_version_id,
                scene_name=str(latest_rows[0]["name"]),
                scene_description=str(latest_rows[0]["scene_description"] or ""),
                costume_description=str(latest_rows[0]["costume_description"] or ""),
                contact_sheet_asset_id=_snapshot_value(snapshot, "contact_sheet_asset_id"),
                generation_source=_snapshot_value(snapshot, "generation_source"),
                views=tuple(
                    SimpleCharacterView(
                        view_type=view_type,
                        asset_id=str(views_by_type[view_type]["asset_id"]),
                    )
                    for view_type in REQUIRED_CHARACTER_VIEW_TYPES
                    if view_type in views_by_type
                ),
                published_at=str(latest_rows[0]["published_at"]),
            )
        )
    return SimpleSceneLookPage(items=looks, total=total, limit=limit, offset=offset)


def decode_scene_constraints(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _snapshot_value(snapshot_json: str, key: str) -> str:
    try:
        snapshot = json.loads(snapshot_json)
    except json.JSONDecodeError as exc:
        raise character_error(500, "SCENE_LOOK_SNAPSHOT_INVALID", "场景造型数据不可用。") from exc
    value = snapshot.get(key) if isinstance(snapshot, dict) else None
    if not isinstance(value, str) or not value:
        raise character_error(500, "SCENE_LOOK_SNAPSHOT_INVALID", "场景造型数据不可用。")
    return value


def _next_version_number(conn: BusinessConnection, *, persona_id: str) -> int:
    row = conn.execute(
        "SELECT MAX(version_number) FROM character_versions WHERE persona_id = %s",
        (persona_id,),
    ).fetchone()
    current = int(row[0]) if row is not None and row[0] is not None else 0
    return current + 1


StorageResolver = Callable[[BusinessConnection, str], StorageAdapter]


@dataclass(frozen=True)
class CharacterStorageCleanupTarget:
    asset_id: str
    storage: StorageAdapter
    key: str
    # True when the bytes survive this identity. Computed while the identity's own
    # asset rows still existed, which is the only moment the question can be
    # answered — after the cascade they are indistinguishable from "someone else's".
    still_referenced: bool = False


@dataclass(frozen=True)
class CharacterStorageCleanupPlan:
    identity_id: str
    actor_id: str
    targets: tuple[CharacterStorageCleanupTarget, ...]
    resolution_failed_count: int


@dataclass(frozen=True)
class CharacterStorageCleanupResult:
    deleted_count: int
    failed_count: int


def delete_simple_character_identity(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    storage_for_uri: StorageResolver,
) -> CharacterStorageCleanupPlan:
    """Delete an identity together with every derived character record.

    In-flight tasks and project selections block deletion. Storage targets are
    resolved while their rows still exist, but object I/O is deliberately
    returned to the route so it runs only after the database transaction has
    committed.
    """
    require_not_auditor(
        conn,
        actor=actor,
        action="simple_character.delete",
        entity_type="person_identity",
        entity_id=identity_id,
    )
    row = read_identity_row(conn, identity_id)
    if actor.role != "admin" and str(row["owner_user_id"]) != actor.id:
        raise character_error(
            404,
            "PERSON_IDENTITY_NOT_FOUND",
            "人物身份不存在或不可用。",
        )

    oral_billing_history = conn.execute(
        """
        SELECT 1
        FROM wallet_transactions AS tx
        JOIN oral_tasks AS task ON task.id = tx.oral_task_id
        WHERE task.identity_id = %s
        LIMIT 1
        """,
        (identity_id,),
    ).fetchone()
    if oral_billing_history is not None:
        raise character_error(
            409,
            "IDENTITY_DELETE_HAS_BILLING_HISTORY",
            "人物已产生口播账务历史，为保留对账记录不可删除。",
        )

    # Paid provider calls may still be in flight for this character; deleting
    # the versions underneath them would lose their write-back.
    active_task = conn.execute(
        """
        SELECT 1
        FROM character_generation_tasks AS task
        JOIN character_versions AS version ON version.id = task.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = %s
          AND task.status IN ('PENDING', 'RUNNING')
        LIMIT 1
        """,
        (identity_id,),
    ).fetchone()
    active_sheet_task = conn.execute(
        """
        SELECT 1
        FROM character_sheet_tasks
        WHERE identity_id = %s
          AND status IN ('PENDING', 'RUNNING')
        LIMIT 1
        """,
        (identity_id,),
    ).fetchone()
    active_oral_task = conn.execute(
        """
        SELECT 1 FROM oral_tasks
        WHERE identity_id = %s
          AND status IN ('QUEUED', 'SUBMITTING', 'SUBMISSION_UNCERTAIN', 'RUNNING',
                         'ARCHIVING', 'ARCHIVE_FAILED')
        UNION ALL
        SELECT 1 FROM oral_avatars
        WHERE identity_id = %s AND status IN ('PENDING', 'RUNNING')
        UNION ALL
        SELECT 1 FROM oral_voices
        WHERE identity_id = %s AND status IN ('PENDING', 'RUNNING')
        LIMIT 1
        """,
        (identity_id, identity_id, identity_id),
    ).fetchone()
    if active_task or active_sheet_task or active_oral_task:
        raise character_error(
            409,
            "IDENTITY_DELETE_HAS_ACTIVE_TASKS",
            "人物存在进行中的生成任务，请等待任务结束后再删除。",
        )

    # Project character selections reference versions with ON DELETE RESTRICT;
    # removing a character a project still relies on must stay explicit.
    used_project = conn.execute(
        """
        SELECT selection.project_id
        FROM character_reference_selections AS selection
        JOIN character_versions AS version ON version.id = selection.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = %s
        LIMIT 1
        """,
        (identity_id,),
    ).fetchone()
    if used_project:
        raise character_error(
            409,
            "IDENTITY_IN_USE",
            "人物已被项目选用，请先在项目中移除该角色后再删除。",
        )

    asset_ids = _identity_asset_ids(conn, identity_id)
    placeholders = ",".join("%s" for _ in asset_ids)
    asset_rows = (
        conn.execute(
            # content_object_id is selected so the registry check below can exclude
            # this identity's own registration: without it, once character uploads
            # are content-addressed every identity would look "still referenced" by
            # itself and its bytes would never be reclaimed.
            f"SELECT id, storage_uri, content_object_id FROM assets WHERE id IN ({placeholders})",  # noqa: S608,E501
            tuple(asset_ids),
        ).fetchall()
        if asset_ids
        else []
    )

    # Keyed by object identity (provider/bucket/key): the approved row reuses
    # the generated row's key and one image can back several views, so targets
    # must be deduped or planned/deleted counts inflate to asset-row counts.
    cleanup_targets_by_object: dict[tuple[str, str, str], CharacterStorageCleanupTarget] = {}
    storage_resolution_failed_count = 0
    for asset in asset_rows:
        uri = str(asset["storage_uri"])
        try:
            storage = storage_for_uri(conn, uri)
            key = storage_key_from_uri(uri)
            # Two ways these bytes outlive the identity: another asset still points
            # at the same URI (the same portrait backing a second identity), or a
            # content_objects row owns them with no asset row of its own. Both are
            # checked now, excluding this identity's own assets, which are deleted
            # in the transaction below.
            own_content_id = asset["content_object_id"]
            still_referenced = content_store.object_referenced_by_another_asset(
                conn,
                storage_uri=uri,
                excluding_asset_ids=list(asset_ids),
            ) or content_store.object_referenced_by_content_registry(
                conn,
                provider=storage.provider,
                bucket=storage.bucket,
                object_key=key,
                # Exclude this identity's own registration. It is released in the
                # transaction below, and the row survives until the sweeper runs —
                # so counting it here would report every character upload as shared
                # and leak the bytes forever.
                excluding_content_object_id=(
                    None if own_content_id is None else str(own_content_id)
                ),
            )
            object_identity = (storage.provider, storage.bucket, key)
            existing_target = cleanup_targets_by_object.get(object_identity)
            if existing_target is None or (
                still_referenced and not existing_target.still_referenced
            ):
                # still_referenced 一旦为真就保持为真（宁可保留共享字节）。
                cleanup_targets_by_object[object_identity] = CharacterStorageCleanupTarget(
                    asset_id=str(asset["id"]),
                    storage=storage,
                    key=key,
                    still_referenced=still_referenced,
                )
        except (HTTPException, StorageBackendUnavailable, OSError, ValueError):
            storage_resolution_failed_count += 1

    version_ids_sql = """
        SELECT version.id
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = %s
    """
    with conn:
        # Registry references are dropped before the cascade removes the asset rows,
        # in the same transaction, so a failed identity delete cannot leave a count
        # already decremented. The bytes are only *scheduled* for reclaim here.
        content_store.release_assets_content_objects(conn, asset_ids=list(asset_ids))
        conn.execute(
            "DELETE FROM character_generation_tasks WHERE character_version_id IN "
            f"({version_ids_sql})",  # noqa: S608
            (identity_id,),
        )
        conn.execute(
            """
            DELETE FROM character_asset_reviews
            WHERE character_asset_id IN (
                SELECT view.id FROM character_assets AS view
                WHERE view.character_version_id IN (
                    SELECT version.id FROM character_versions AS version
                    JOIN character_personas AS persona ON persona.id = version.persona_id
                    WHERE persona.identity_id = %s
                )
            )
            """,
            (identity_id,),
        )
        conn.execute(
            f"DELETE FROM character_assets WHERE character_version_id IN ({version_ids_sql})",  # noqa: S608
            (identity_id,),
        )
        conn.execute(
            "DELETE FROM character_versions WHERE persona_id IN "
            "(SELECT id FROM character_personas WHERE identity_id = %s)",
            (identity_id,),
        )
        conn.execute(
            "DELETE FROM character_personas WHERE identity_id = %s",
            (identity_id,),
        )
        conn.execute("DELETE FROM person_identities WHERE id = %s", (identity_id,))
        if asset_ids:
            conn.execute(
                f"DELETE FROM assets WHERE id IN ({placeholders})",
                tuple(asset_ids),
            )

    cleanup_targets = tuple(cleanup_targets_by_object.values())
    shared_object_count = sum(1 for target in cleanup_targets if target.still_referenced)
    write_audit(
        conn,
        actor=actor,
        action="simple_character.delete",
        entity_type="person_identity",
        entity_id=identity_id,
        metadata={
            "deleted_asset_count": len(asset_rows),
            "storage_cleanup_planned_count": len(cleanup_targets),
            "shared_storage_object_count": shared_object_count,
            "storage_resolution_failed_count": storage_resolution_failed_count,
        },
    )
    return CharacterStorageCleanupPlan(
        identity_id=identity_id,
        actor_id=actor.id,
        targets=cleanup_targets,
        resolution_failed_count=storage_resolution_failed_count,
    )


def cleanup_deleted_character_objects(
    plan: CharacterStorageCleanupPlan,
) -> CharacterStorageCleanupResult:
    """Best-effort post-commit object cleanup for a deleted identity."""
    deleted_count = 0
    failed_count = plan.resolution_failed_count
    for target in plan.targets:
        if target.still_referenced:
            # Another identity or the content registry still serves these bytes.
            continue
        try:
            if content_store.delete_object_outside_content_namespace(
                target.storage, target.key, actor_id=plan.actor_id
            ):
                deleted_count += 1
        except Exception:  # noqa: BLE001 - DB deletion already committed
            failed_count += 1
            logger.warning(
                "character storage cleanup failed identity=%s asset=%s",
                plan.identity_id,
                target.asset_id,
                exc_info=True,
            )
    return CharacterStorageCleanupResult(
        deleted_count=deleted_count,
        failed_count=failed_count,
    )


def _identity_asset_ids(conn: BusinessConnection, identity_id: str) -> set[str]:
    """Collect every asset owned by an identity.

    Covers the authorization/source upload, the per-view candidates of every
    version, and each version's contact sheet (snapshots predating the contact
    sheet feature simply contribute nothing).
    """
    identity = conn.execute(
        """
        SELECT authorization_asset_id, source_asset_id
        FROM person_identities WHERE id = %s
        """,
        (identity_id,),
    ).fetchone()
    asset_ids: set[str] = set()
    if identity is not None:
        for column in ("authorization_asset_id", "source_asset_id"):
            value = identity[column]
            if value is not None:
                asset_ids.add(str(value))

    published_asset_ids = {
        str(row[0])
        for row in conn.execute(
            """
        SELECT view.asset_id
        FROM character_assets AS view
        JOIN character_versions AS version ON version.id = view.character_version_id
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = %s AND view.asset_id IS NOT NULL
        """,
            (identity_id,),
        ).fetchall()
    }
    asset_ids.update(published_asset_ids)

    for row in conn.execute(
        """
        SELECT result_asset_id AS asset_id
        FROM oral_tasks
        WHERE identity_id = %s AND result_asset_id IS NOT NULL
        UNION
        SELECT demo_asset_id AS asset_id
        FROM oral_voices
        WHERE identity_id = %s AND demo_asset_id IS NOT NULL
        """,
        (identity_id, identity_id),
    ).fetchall():
        asset_ids.add(str(row[0]))

    # Publishing replaces character_assets.asset_id with the approved object.
    # Its metadata retains the generated candidate id, which must be deleted too.
    if published_asset_ids:
        placeholders = ",".join("%s" for _ in published_asset_ids)
        for row in conn.execute(
            f"SELECT metadata_json FROM assets WHERE id IN ({placeholders})",  # noqa: S608
            tuple(published_asset_ids),
        ).fetchall():
            try:
                generated_asset_id = json.loads(str(row[0])).get("generated_asset_id")
            except (TypeError, ValueError):
                generated_asset_id = None
            if generated_asset_id:
                asset_ids.add(str(generated_asset_id))

    for snapshot_row in conn.execute(
        """
        SELECT version.publication_snapshot_json AS snapshot_json
        FROM character_versions AS version
        JOIN character_personas AS persona ON persona.id = version.persona_id
        WHERE persona.identity_id = %s
        """,
        (identity_id,),
    ).fetchall():
        contact_id = _snapshot_contact_sheet_asset_id(snapshot_row)
        if contact_id is not None:
            asset_ids.add(contact_id)
    return asset_ids


def validate_simple_character_source(
    content: bytes,
    content_type: str,
    display_name: str,
) -> None:
    name = display_name.strip()
    if not name:
        raise character_error(422, "SIMPLE_CHARACTER_NAME_REQUIRED", "请填写人物名称。")
    if not content:
        raise character_error(422, "SIMPLE_CHARACTER_IMAGE_REQUIRED", "请上传人物授权图片。")
    if len(content) > SIMPLE_UPLOAD_MAX_BYTES:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_TOO_LARGE",
            "人物授权图片超过 10MB 限制。",
        )
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type not in SIMPLE_UPLOAD_ALLOWED_TYPES:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_TYPE_UNSUPPORTED",
            "仅支持 PNG、JPEG 或 WebP 图片。",
        )
    if not _has_valid_image_structure(content, normalized_type):
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_INVALID",
            "人物授权图片文件无效，请重新选择原图。",
        )
    if normalized_type == "image/png" and _decode_png_rgb(content) is not None:
        return
    try:
        ffmpeg_path = resolve_media_binary("ffmpeg")
        validate_image_decodable(ffmpeg_path, content)
    except MediaToolUnavailable as exc:
        raise character_error(
            503,
            "SIMPLE_CHARACTER_IMAGE_VALIDATION_UNAVAILABLE",
            "人物图片校验工具暂不可用，请稍后重试。",
        ) from exc
    except MediaToolFailed as exc:
        raise character_error(
            422,
            "SIMPLE_CHARACTER_IMAGE_INVALID",
            "人物授权图片无法解码，请重新选择原图。",
        ) from exc


def _has_valid_image_structure(content: bytes, content_type: str) -> bool:
    if content_type == "image/png":
        return _parse_png(content) is not None
    if content_type == "image/jpeg":
        return _jpeg_structure_is_valid(content)
    if content_type == "image/webp":
        return _webp_structure_is_valid(content)
    return False


JPEG_START_OF_FRAME_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)


def _jpeg_structure_is_valid(content: bytes) -> bool:
    if (
        len(content) < 12
        or not content.startswith(b"\xff\xd8")
        or not content.endswith(b"\xff\xd9")
    ):
        return False
    offset = 2
    found_frame = False
    while offset < len(content) - 2:
        if content[offset] != 0xFF:
            return False
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset >= len(content):
            return False
        marker = content[offset]
        offset += 1
        if marker == 0xD9:
            return found_frame and offset == len(content)
        if marker == 0x00 or marker == 0xD8:
            return False
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(content):
            return False
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        segment_end = offset + segment_length
        if segment_length < 2 or segment_end > len(content):
            return False
        if marker in JPEG_START_OF_FRAME_MARKERS:
            if segment_length < 7:
                return False
            height = int.from_bytes(content[offset + 3 : offset + 5], "big")
            width = int.from_bytes(content[offset + 5 : offset + 7], "big")
            if width <= 0 or height <= 0 or width * height > SIMPLE_IMAGE_MAX_PIXELS:
                return False
            found_frame = True
        if marker == 0xDA:
            return found_frame and segment_end < len(content) - 2
        offset = segment_end
    return False


def _webp_structure_is_valid(content: bytes) -> bool:
    if (
        len(content) < 20
        or content[:4] != b"RIFF"
        or content[8:12] != b"WEBP"
        or int.from_bytes(content[4:8], "little") + 8 != len(content)
    ):
        return False
    offset = 12
    found_image = False
    while offset < len(content):
        if offset + 8 > len(content):
            return False
        chunk_type = content[offset : offset + 4]
        chunk_size = int.from_bytes(content[offset + 4 : offset + 8], "little")
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        padded_end = payload_end + (chunk_size % 2)
        if payload_end > len(content) or padded_end > len(content):
            return False
        payload = content[payload_start:payload_end]
        if chunk_type == b"VP8 ":
            if len(payload) <= 10 or payload[3:6] != b"\x9d\x01\x2a":
                return False
            width = int.from_bytes(payload[6:8], "little") & 0x3FFF
            height = int.from_bytes(payload[8:10], "little") & 0x3FFF
            found_image = 0 < width * height <= SIMPLE_IMAGE_MAX_PIXELS
        elif chunk_type == b"VP8L":
            if len(payload) <= 5 or payload[0] != 0x2F:
                return False
            dimensions = int.from_bytes(payload[1:5], "little")
            width = (dimensions & 0x3FFF) + 1
            height = ((dimensions >> 14) & 0x3FFF) + 1
            found_image = 0 < width * height <= SIMPLE_IMAGE_MAX_PIXELS
        offset = padded_end
    return found_image


def _store_source_asset(
    conn: BusinessConnection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    identity_id: str,
    content: bytes,
    content_type: str,
    attempted_keys: list[str],
    prepared_asset: PreparedSimpleCharacterAsset | None = None,
) -> str:
    if prepared_asset is None:
        asset_id = str(uuid.uuid4())
        extension = SIMPLE_UPLOAD_ALLOWED_TYPES[content_type.split(";", 1)[0].strip().lower()]
        key = identity_asset_key(
            owner_user_id=actor.id,
            identity_id=identity_id,
            purpose="source",
            asset_id=asset_id,
            extension=extension,
        )
        stored = storage.put_object(key, content, content_type=content_type)
        attempted_keys.append(stored.key)
    else:
        asset_id = prepared_asset.asset_id
        stored = prepared_asset.stored
    # Register these bytes under this user so a later upload of the same photo
    # resolves to the object that already exists. Character sources stay
    # user-scoped: two customers uploading an identical portrait must never be
    # served the same object, even though the hash matches.
    registered, deduplicated = content_store.retain_content_object(
        conn,
        sha256=stored.sha256,
        size_bytes=stored.size,
        content_type=stored.content_type,
        provider=stored.provider,
        bucket=stored.bucket,
        object_key=stored.key,
        scope="user",
        owner_user_id=actor.id,
    )
    if deduplicated and stored.key != registered.object_key:
        # An earlier copy already holds these bytes for this user, so this asset
        # points at that copy. The object this request just wrote is an orphan.
        # Leaving it costs storage but never breaks a reference; deleting it
        # here would have to happen before the surrounding transaction commits,
        # and a premature delete is unrecoverable. upload_cleanup reaps it later.
        stored_uri = registered.storage_uri
        stored_key = registered.object_key
    else:
        stored_uri = stored.uri
        stored_key = stored.key
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json, content_object_id
        ) VALUES (%s, NULL, 'character_source_image', %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            asset_id,
            stored_uri,
            stored.sha256,
            stored.size,
            stored.content_type,
            actor.id,
            encode_json(
                {
                    "identity_id": identity_id,
                    "object_key": stored_key,
                    "purpose": "simple_upload_source",
                    "upload_status": "UPLOADED",
                    "content_deduplicated": deduplicated,
                }
            ),
            registered.id,
        ),
    )
    return asset_id


def _insert_identity(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    identity_id: str,
    display_name: str,
    source_asset_id: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO person_identities (
            id, owner_user_id, display_name, authorization_status,
            authorization_asset_id, authorization_scope, authorization_expires_at,
            source_asset_id, source_quality_status, status, created_by,
            created_at, updated_at
        ) VALUES (%s, %s, %s, 'AUTHORIZED', %s, %s, NULL, %s, 'PASSED', 'ACTIVE', %s, %s, %s)
        """,
        (
            identity_id,
            actor.id,
            display_name.strip(),
            source_asset_id,
            encode_json(SIMPLE_AUTHORIZATION_SCOPE),
            source_asset_id,
            actor.id,
            now_iso,
            now_iso,
        ),
    )


def _insert_persona(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    persona_id: str,
    identity_id: str,
    persona_name: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO character_personas (
            id, identity_id, name, usage_scope_json, created_by,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            persona_id,
            identity_id,
            persona_name.strip(),
            encode_json(SIMPLE_PERSONA_USAGE_SCOPE),
            actor.id,
            now_iso,
            now_iso,
        ),
    )


def _insert_scene_persona(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    persona_id: str,
    identity_id: str,
    scene_name: str,
    scene_description: str,
    costume_description: str,
    now_iso: str,
) -> None:
    conn.execute(
        """
        INSERT INTO character_personas (
            id, identity_id, name, scene_description,
            appearance_constraints_json, costume_description,
            default_background, usage_scope_json, created_by,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            persona_id,
            identity_id,
            scene_name,
            scene_description,
            encode_json({"appearance_type": "scene"}),
            costume_description,
            scene_description,
            encode_json(SIMPLE_PERSONA_USAGE_SCOPE),
            actor.id,
            now_iso,
            now_iso,
        ),
    )


def _insert_version(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
    persona_id: str,
    persona_snapshot_json: str,
    now_iso: str,
    version_number: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO character_versions (
            id, persona_id, version_number, status,
            persona_snapshot_json, provider, model, generation_params_json,
            template_version, template_hash, required_view_types_json,
            created_by, created_at, generation_mode
        ) VALUES (%s, %s, %s, 'REVIEWING', %s, 'local_simple_upload',
                  'deterministic-v1', '{}', %s, %s, %s, %s, %s, 'simple_upload')
        """,
        (
            version_id,
            persona_id,
            version_number,
            persona_snapshot_json,
            CHARACTER_TEMPLATE_VERSION,
            CHARACTER_TEMPLATE_HASH,
            encode_json(list(REQUIRED_CHARACTER_VIEW_TYPES)),
            actor.id,
            now_iso,
        ),
    )


@dataclass(frozen=True)
class _ApprovedView:
    # MATERIAL-UX-14: the approved row reuses the generated view's stored
    # object (same key/uri/sha256/size) instead of uploading a second copy.
    view_type: RequiredCharacterViewType
    character_asset_id: str
    generated_asset_id: str
    review_id: str
    stored: StoredObject


# Per-view images are cropped out of the real contact sheet so the published
# views show the actual person instead of a locally generated solid rectangle.
# Layout recovery is divider-strip based: real sheets follow the prompt's
# "three tall columns + two stacked close-ups separated by thin white
# dividers" layout, and the white strips are detected per column/row.
CONTACT_SHEET_WHITE_THRESHOLD = 240
CONTACT_SHEET_FRONT_HALF_HEIGHT_RATIO = 0.62
CONTACT_SHEET_MIN_PANEL_SIZE = 16
# 五视图的清晰度下限（与上面的排版识别下限是两回事：那 16px 只保证能切出 5 格，
# 低分辨率出图照样能一路发布）。2026-09-22 实测：声明宽高比后按 2K 档请求
# 2048x1152，整图 2048 宽；而历史上未声明宽高比时的 size=auto 实测 1672~1815 宽
# （取决于提示词与模型）。取 1600 作下限，既不会把现有可用出图误判为失败，
# 又能挡住"请求了 2K 却拿到小图"的静默降质。
CONTACT_SHEET_MIN_SHEET_WIDTH = 1600


@dataclass(frozen=True)
class _ContactSheetPanels:
    """Pixel rects (x0, y0, x1, y1; half-open) of the sheet's key panels."""

    front_full: tuple[int, int, int, int]
    left_45: tuple[int, int, int, int]
    left_side: tuple[int, int, int, int]
    front_face: tuple[int, int, int, int]


def crop_contact_sheet_views(
    contact_content: bytes, contact_content_type: str
) -> dict[str, bytes] | None:
    """Crop the five standard views out of the five-panel contact sheet.

    Returns ``{view_type: png_bytes}`` or ``None`` when the sheet cannot be
    decoded (non-PNG provider output, other bit depths, interlacing). Sheets
    without detectable dividers use the nominal layout geometry instead, so
    a slightly off-layout sheet still yields real cropped views.
    """
    if contact_content_type.split(";", 1)[0].strip().lower() != "image/png":
        return None
    decoded = _decode_png_rgb(contact_content)
    if decoded is None:
        return None
    width, height, rows = decoded
    panels = _contact_sheet_panels_from_pixels(width, height, rows)
    if panels is None:
        return None

    x0, y0, x1, y1 = panels.front_full
    half_bottom = y0 + round((y1 - y0) * CONTACT_SHEET_FRONT_HALF_HEIGHT_RATIO)
    crops: dict[str, bytes] = {
        "FRONT_FULL": _encode_rgb_panel_png(rows, panels.front_full),
        "FRONT_HALF": _encode_rgb_panel_png(rows, (x0, y0, x1, half_bottom)),
        "FRONT_FACE": _encode_rgb_panel_png(rows, panels.front_face),
        "LEFT_45": _encode_rgb_panel_png(rows, panels.left_45),
        "LEFT_SIDE": _encode_rgb_panel_png(rows, panels.left_side),
    }
    return crops


def _require_contact_sheet_resolution(
    contact_content: bytes,
    contact_content_type: str,
) -> None:
    """拒绝低于清晰度下限的五视图出图（阈值理由见 CONTACT_SHEET_MIN_SHEET_WIDTH）。

    非 PNG 或读不出尺寸时直接放行，交由裁剪校验按既有的
    CONTACT_SHEET_PROVIDER_INVALID_OUTPUT 统一拒绝，避免同一问题两种错误码。
    """
    if contact_content_type.split(";", 1)[0].strip().lower() != "image/png":
        return
    parsed = _parse_png(contact_content)
    if parsed is None:
        return
    if parsed.width < CONTACT_SHEET_MIN_SHEET_WIDTH:
        raise character_error(
            502,
            "CONTACT_SHEET_RESOLUTION_TOO_LOW",
            "人物五视图分辨率不足，请稍后重试。",
        )


def _require_contact_sheet_views(
    contact_content: bytes,
    contact_content_type: str,
) -> dict[str, bytes]:
    _require_contact_sheet_resolution(contact_content, contact_content_type)
    try:
        crops = crop_contact_sheet_views(contact_content, contact_content_type)
    except (IndexError, struct.error, ValueError, zlib.error):
        crops = None
    if crops is None or any(
        not crops.get(view_type) for view_type in REQUIRED_CHARACTER_VIEW_TYPES
    ):
        raise character_error(
            502,
            "CONTACT_SHEET_PROVIDER_INVALID_OUTPUT",
            "人物五视图生成服务返回了无法裁剪的图片，请稍后重试。",
        )
    return crops


@dataclass(frozen=True)
class _ParsedPng:
    width: int
    height: int
    bit_depth: int
    color_type: int
    interlace: int
    compressed: bytes


def _parse_png(data: bytes) -> _ParsedPng | None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    width = height = bit_depth = color_type = interlace = 0
    compressed = bytearray()
    pos = 8
    seen_header = False
    seen_image_data = False
    image_data_ended = False
    seen_end = False
    while pos < len(data):
        if pos + 12 > len(data):
            return None
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        chunk_type = data[pos + 4 : pos + 8]
        chunk_end = pos + 12 + length
        if chunk_end > len(data):
            return None
        body = data[pos + 8 : pos + 8 + length]
        expected_crc = struct.unpack(">I", data[pos + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + body) & 0xFFFFFFFF != expected_crc:
            return None
        if not seen_header and chunk_type != b"IHDR":
            return None
        if chunk_type == b"IHDR":
            if seen_header or length != 13:
                return None
            width, height, bit_depth, color_type = struct.unpack(">IIBB", body[:10])
            compression_method = body[10]
            filter_method = body[11]
            interlace = body[12]
            if compression_method != 0 or filter_method != 0 or interlace not in {0, 1}:
                return None
            seen_header = True
        elif chunk_type == b"IDAT":
            if not seen_header or image_data_ended:
                return None
            seen_image_data = True
            compressed.extend(body)
        elif chunk_type == b"IEND":
            if length != 0 or not seen_image_data:
                return None
            seen_end = True
            pos = chunk_end
            break
        elif seen_image_data:
            image_data_ended = True
        pos = chunk_end
    if (
        not seen_header
        or not seen_image_data
        or not seen_end
        or pos != len(data)
        or width <= 0
        or height <= 0
        or width * height > SIMPLE_IMAGE_MAX_PIXELS
    ):
        return None
    return _ParsedPng(
        width=width,
        height=height,
        bit_depth=bit_depth,
        color_type=color_type,
        interlace=interlace,
        compressed=bytes(compressed),
    )


def _decode_png_rgb(data: bytes) -> tuple[int, int, list[bytes]] | None:
    """Decode a non-interlaced 8-bit RGB/RGBA PNG into per-row RGB bytes."""
    parsed = _parse_png(data)
    if parsed is None:
        return None
    width = parsed.width
    height = parsed.height
    bit_depth = parsed.bit_depth
    color_type = parsed.color_type
    interlace = parsed.interlace
    if width <= 0 or height <= 0 or bit_depth != 8 or interlace != 0:
        return None
    if color_type == 2:
        channels = 3
    elif color_type == 6:
        channels = 4
    else:
        return None
    stride = width * channels
    expected_size = height * (stride + 1)
    if expected_size > SIMPLE_PNG_MAX_DECOMPRESSED_BYTES:
        return None
    decompressor = zlib.decompressobj()
    try:
        raw = decompressor.decompress(parsed.compressed, expected_size + 1)
        if decompressor.unconsumed_tail or len(raw) > expected_size:
            return None
        raw += decompressor.flush()
    except zlib.error:
        return None
    if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
        return None
    if len(raw) != expected_size:
        return None
    rows: list[bytes] = []
    previous = bytes(stride)
    for y in range(height):
        offset = y * (stride + 1)
        filter_type = raw[offset]
        row = bytearray(raw[offset + 1 : offset + 1 + stride])
        if filter_type == 1:  # Sub
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(stride):
                row[i] = (row[i] + previous[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:  # Paeth
            for i in range(stride):
                a = row[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                if pa <= pb and pa <= pc:
                    predictor = a
                elif pb <= pc:
                    predictor = b
                else:
                    predictor = c
                row[i] = (row[i] + predictor) & 0xFF
        elif filter_type != 0:  # Unknown filter byte: refuse instead of guessing.
            return None
        current = bytes(row)
        if channels == 4:
            rgb = bytearray(width * 3)
            rgb[0::3] = current[0::4]
            rgb[1::3] = current[1::4]
            rgb[2::3] = current[2::4]
            rows.append(bytes(rgb))
        else:
            rows.append(current)
        previous = current
    return width, height, rows


def _contact_sheet_panels_from_pixels(
    width: int, height: int, rows: list[bytes]
) -> _ContactSheetPanels | None:
    if width < 3 * CONTACT_SHEET_MIN_PANEL_SIZE or height < 2 * CONTACT_SHEET_MIN_PANEL_SIZE:
        return None
    column_runs = _white_runs([_column_is_divider_white(x, height, rows) for x in range(width)])
    left_edge = 0
    right_edge = width
    internal_columns: list[tuple[int, int]] = []
    for start, end in column_runs:
        if end - start + 1 > max(80, width // 12):
            # A run this wide is a white background area, not a thin divider.
            continue
        if start < width * 0.05:
            left_edge = end + 1
        elif end > width * 0.95:
            right_edge = start
        else:
            internal_columns.append((start, end))
    if len(internal_columns) == 3:
        panels = _panels_from_dividers(width, height, rows, left_edge, right_edge, internal_columns)
    else:
        panels = _nominal_contact_sheet_panels(width, height)
    for rect in (panels.front_full, panels.left_45, panels.left_side, panels.front_face):
        if (
            rect[2] - rect[0] < CONTACT_SHEET_MIN_PANEL_SIZE
            or rect[3] - rect[1] < CONTACT_SHEET_MIN_PANEL_SIZE
        ):
            return None
    return panels


def _panels_from_dividers(
    width: int,
    height: int,
    rows: list[bytes],
    left_edge: int,
    right_edge: int,
    internal_columns: list[tuple[int, int]],
) -> _ContactSheetPanels:
    """Build panel rects from three detected vertical divider strips."""
    top_edge = 0
    bottom_edge = height
    for start, end in _white_runs([_row_is_divider_white(y, width, rows) for y in range(height)]):
        if end - start + 1 > max(80, height // 12):
            continue
        if start < height * 0.05:
            top_edge = end + 1
        elif end > height * 0.95:
            bottom_edge = start
    (col1_start, col1_end), (col2_start, col2_end), (col3_start, col3_end) = internal_columns
    right_x0 = col3_end + 1
    # The stacked close-ups' horizontal divider only spans the right zone, so
    # restrict row scanning to it (the image's outer 10% is also skipped to
    # ignore edge darkening that real sheets carry at their borders).
    scan_x0 = right_x0 + max(1, (right_edge - right_x0) // 10)
    scan_x1 = right_edge - max(1, (right_edge - right_x0) // 10)
    divider: tuple[int, int] | None = None
    for start, end in _white_runs(
        [_range_row_is_white(y, scan_x0, scan_x1, rows) for y in range(height)]
    ):
        centered = top_edge + (bottom_edge - top_edge) * 0.2
        lower = top_edge + (bottom_edge - top_edge) * 0.8
        if end - start + 1 <= max(80, height // 12) and start >= centered and end <= lower:
            divider = (start, end)
            break
    if divider is None:
        # No horizontal divider between the close-ups: split the right zone
        # at its midpoint as the best geometric guess.
        middle = (top_edge + bottom_edge) // 2
        divider = (middle, middle)
    return _ContactSheetPanels(
        front_full=(left_edge, top_edge, col1_start, bottom_edge),
        left_45=(col1_end + 1, top_edge, col2_start, bottom_edge),
        left_side=(col2_end + 1, top_edge, col3_start, bottom_edge),
        front_face=(right_x0, top_edge, right_edge, divider[0]),
    )


def _nominal_contact_sheet_panels(width: int, height: int) -> _ContactSheetPanels:
    """Fallback layout geometry when no dividers can be detected."""
    border = max(4, round(width * 0.005))
    gap = max(4, round(width * 0.004))
    inner_width = width - 2 * border
    left_zone = round(inner_width * 0.705)
    column = (left_zone - 2 * gap) / 3
    right_x0 = border + left_zone + gap
    middle = height // 2
    half_gap = max(2, gap // 2)
    return _ContactSheetPanels(
        front_full=(border, border, round(border + column), height - border),
        left_45=(
            round(border + column + gap),
            border,
            round(border + 2 * column + gap),
            height - border,
        ),
        left_side=(
            round(border + 2 * column + 2 * gap),
            border,
            round(border + 3 * column + 2 * gap),
            height - border,
        ),
        front_face=(right_x0, border, width - border, middle - half_gap),
    )


def _column_is_divider_white(x: int, height: int, rows: list[bytes]) -> bool:
    sampled = 0
    white = 0
    for y in range(height // 10, height - height // 10, 4):
        index = x * 3
        sampled += 1
        if _pixel_is_white(rows[y], index):
            white += 1
    return sampled > 0 and white / sampled >= 0.99


def _row_is_divider_white(y: int, width: int, rows: list[bytes]) -> bool:
    sampled = 0
    white = 0
    for x in range(width // 10, width - width // 10, 4):
        sampled += 1
        if _pixel_is_white(rows[y], x * 3):
            white += 1
    return sampled > 0 and white / sampled >= 0.99


def _range_row_is_white(y: int, x_begin: int, x_end: int, rows: list[bytes]) -> bool:
    sampled = 0
    white = 0
    for x in range(x_begin, x_end, 2):
        sampled += 1
        if _pixel_is_white(rows[y], x * 3):
            white += 1
    return sampled > 0 and white / sampled >= 0.98


def _pixel_is_white(row: bytes, index: int) -> bool:
    return (
        row[index] >= CONTACT_SHEET_WHITE_THRESHOLD
        and row[index + 1] >= CONTACT_SHEET_WHITE_THRESHOLD
        and row[index + 2] >= CONTACT_SHEET_WHITE_THRESHOLD
    )


def _white_runs(flags: list[bool]) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    for index, flag in enumerate(flags):
        if not flag:
            continue
        if runs and index == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], index)
        else:
            runs.append((index, index))
    return runs


def _encode_rgb_panel_png(rows: list[bytes], rect: tuple[int, int, int, int]) -> bytes:
    """Encode one panel rect as a standalone 8-bit RGB PNG (filter 0)."""
    x0, y0, x1, y1 = rect
    scanlines = bytearray()
    for y in range(y0, y1):
        row = rows[y][x0 * 3 : x1 * 3]
        scanlines += b"\x00"
        scanlines += row
    header = struct.pack(">IIBBBBB", x1 - x0, y1 - y0, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(bytes(scanlines), 6))
        + png_chunk(b"IEND", b"")
    )


def _generate_and_approve_views(
    conn: BusinessConnection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    version_id: str,
    persona_id: str,
    now_iso: str,
    attempted_keys: list[str],
    contact_content: bytes,
    contact_content_type: str,
    prepared_views: tuple[PreparedSimpleCharacterViewStorage, ...] | None = None,
) -> list[_ApprovedView]:
    """Record one approved per-view asset for each required view type.

    Every view is cropped from the contact sheet and uploaded once as its
    generated object; ``_publish_views`` then reuses that object for the
    approved row. Uncroppable provider output must fail before any view is
    approved or published.
    """
    cropped_views = _require_contact_sheet_views(contact_content, contact_content_type)
    prepared_by_view = (
        {view.view_type: view for view in prepared_views} if prepared_views is not None else {}
    )
    views: list[_ApprovedView] = []
    for view_type in REQUIRED_CHARACTER_VIEW_TYPES:
        prepared_view = prepared_by_view.get(view_type)
        if prepared_view is None:
            character_asset_id = str(uuid.uuid4())
            generated_asset_id = str(uuid.uuid4())
            review_id = str(uuid.uuid4())
            content = cropped_views[view_type]
            generated_key = generated_character_asset_key(
                owner_user_id=actor.id,
                persona_id=persona_id,
                version_id=version_id,
                view_type=view_type,
                asset_id=generated_asset_id,
            )
            stored = storage.put_object(generated_key, content, content_type="image/png")
            attempted_keys.append(stored.key)
        else:
            character_asset_id = prepared_view.character_asset_id
            generated_asset_id = prepared_view.generated_asset.asset_id
            review_id = prepared_view.review_id
            stored = prepared_view.generated_asset.stored
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id, metadata_json
            ) VALUES (%s, NULL, 'character_generated_image', %s, %s, %s, %s, %s, %s)
            """,
            (
                generated_asset_id,
                stored.uri,
                stored.sha256,
                stored.size,
                stored.content_type,
                actor.id,
                encode_json(
                    {
                        "character_version_id": version_id,
                        "generation_mode": SIMPLE_GENERATION_MODE,
                        "view_type": view_type,
                        "view_content_source": "contact_sheet_crop",
                    }
                ),
            ),
        )
        conn.execute(
            """
            INSERT INTO character_assets (
                id, character_version_id, asset_id, view_type, candidate_number,
                auto_quality_json, review_status, is_published_selection, created_at
            ) VALUES (%s, %s, %s, %s, 1, '{}', 'APPROVED', 0, %s)
            """,
            (character_asset_id, version_id, generated_asset_id, view_type, now_iso),
        )
        conn.execute(
            """
            INSERT INTO character_asset_reviews (
                id, character_asset_id, reviewer_user_id, decision,
                issue_codes_json, comment, created_at
            ) VALUES (%s, %s, %s, 'APPROVED', '[]', %s, %s)
            """,
            (
                review_id,
                character_asset_id,
                None,
                "System auto-approved by direct-publish policy.",
                now_iso,
            ),
        )
        views.append(
            _ApprovedView(
                view_type=view_type,
                character_asset_id=character_asset_id,
                generated_asset_id=generated_asset_id,
                review_id=review_id,
                stored=stored,
            )
        )
    return views


def _publish_views(
    conn: BusinessConnection,
    *,
    actor: CurrentUser,
    version_id: str,
    persona_snapshot_json: str,
    views: list[_ApprovedView],
    contact_sheet_asset_id: str,
    generation_source: str,
    now_iso: str,
    prepared_views: tuple[PreparedSimpleCharacterViewStorage, ...] | None = None,
    scene_quality: SceneContactSheetQualityResult | None = None,
) -> tuple[str, dict[str, dict[str, object]]]:
    assets_by_view: dict[str, dict[str, object]] = {}
    prepared_by_view = (
        {view.view_type: view for view in prepared_views} if prepared_views is not None else {}
    )
    for view in views:
        prepared_view = prepared_by_view.get(view.view_type)
        if prepared_view is None:
            approved_asset_id = str(uuid.uuid4())
        else:
            approved_asset_id = prepared_view.approved_asset_id
        # MATERIAL-UX-14: publishing writes no objects; the approved row
        # records the generated view's stored object as-is.
        stored = view.stored
        conn.execute(
            """
            INSERT INTO assets (
                id, project_id, kind, storage_uri, sha256, size_bytes,
                content_type, created_by_user_id, metadata_json
            ) VALUES (%s, NULL, 'character_approved_image', %s, %s, %s, %s, %s, %s)
            """,
            (
                approved_asset_id,
                stored.uri,
                stored.sha256,
                stored.size,
                stored.content_type,
                actor.id,
                encode_json(
                    {
                        "character_asset_id": view.character_asset_id,
                        "character_version_id": version_id,
                        "generated_asset_id": view.generated_asset_id,
                        "view_type": view.view_type,
                    }
                ),
            ),
        )
        updated = conn.execute(
            """
            UPDATE character_assets
            SET asset_id = %s, is_published_selection = 1
            WHERE id = %s AND character_version_id = %s
              AND asset_id = %s AND review_status = 'APPROVED'
            """,
            (
                approved_asset_id,
                view.character_asset_id,
                version_id,
                view.generated_asset_id,
            ),
        )
        if updated.rowcount != 1:
            raise character_error(
                409,
                "SIMPLE_CHARACTER_ASSET_CHANGED",
                "生成资产在发布前发生变化，请重试。",
            )
        assets_by_view[view.view_type] = {
            "approved_asset_id": approved_asset_id,
            "character_asset_id": view.character_asset_id,
            "content_type": stored.content_type,
            "generated_asset_id": view.generated_asset_id,
            "review_id": view.review_id,
            "sha256": stored.sha256,
            "size_bytes": stored.size,
            "storage_uri": stored.uri,
        }

    snapshot: dict[str, object] = {
        "assets_by_view": assets_by_view,
        "character_version_id": version_id,
        "contact_sheet_asset_id": contact_sheet_asset_id,
        "generation_source": generation_source,
        "persona_snapshot_hash": hashlib.sha256(persona_snapshot_json.encode()).hexdigest(),
        "published_at": now_iso,
        "review_policy": "SYSTEM_AUTO_PUBLISH",
        "required_view_types": list(REQUIRED_CHARACTER_VIEW_TYPES),
        "schema_version": CHARACTER_PUBLICATION_SCHEMA_VERSION,
        "template_hash": CHARACTER_TEMPLATE_HASH,
        "template_version": CHARACTER_TEMPLATE_VERSION,
    }
    if scene_quality is not None:
        snapshot["scene_quality"] = scene_quality.model_dump(mode="json")
    snapshot_json = encode_json(snapshot)
    publication_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
    updated_version = conn.execute(
        """
        UPDATE character_versions
        SET status = 'PUBLISHED', published_by = %s, published_at = %s,
            publication_snapshot_json = %s, publication_hash = %s
        WHERE id = %s AND status = 'REVIEWING'
        """,
        (actor.id, now_iso, snapshot_json, publication_hash, version_id),
    )
    if updated_version.rowcount != 1:
        raise character_error(
            409,
            "SIMPLE_CHARACTER_VERSION_NOT_REVIEWING",
            "角色版本状态异常，无法发布。",
        )
    return publication_hash, assets_by_view


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _generate_contact_sheet_content(
    provider: ImageProvider | None,
    *,
    source_content: bytes,
    source_content_type: str,
    version_id: str,
    prompt: str = SIMPLE_CONTACT_SHEET_PROMPT,
) -> tuple[bytes, str, str]:
    """Render the single five-view contact sheet image.

    Returns ``(content, content_type, generation_source)``. An explicitly
    unconfigured local runtime may use a deterministic placeholder. Once a
    real provider is selected, failures and unusable output stay visible and
    must never be published as a successful customer character.
    """
    if provider is None or getattr(provider, "provider_name", "") == "fake":
        return (
            contact_sheet_placeholder_png(f"contact-sheet:{version_id}".encode()),
            "image/png",
            "local_placeholder",
        )
    if provider is not None:
        extension = SIMPLE_CONTACT_SHEET_EXTENSIONS.get(source_content_type, ".png")
        try:
            generated = provider.edit(
                model=SIMPLE_CONTACT_SHEET_MODEL,
                prompt=prompt,
                aspect_ratio=SIMPLE_CONTACT_SHEET_ASPECT_RATIO,
                size_override=CONTACT_SHEET_SIZE,
                source_image=ImageInput(
                    content=source_content,
                    content_type=source_content_type,
                    filename=f"character-source{extension}",
                ),
                character_reference_images=[],
                output_count=1,
            )
        except ImageProviderFailed as exc:
            raise character_error(
                502,
                "CONTACT_SHEET_PROVIDER_FAILED",
                "人物五视图生成服务暂不可用，请稍后重试。",
            ) from exc
        else:
            if generated:
                image = generated[0]
                content_type = image.content_type.split(";", 1)[0].strip().lower()
                if image.content and content_type in SIMPLE_CONTACT_SHEET_EXTENSIONS:
                    _require_contact_sheet_views(image.content, content_type)
                    return image.content, content_type, "image_provider"
            raise character_error(
                502,
                "CONTACT_SHEET_PROVIDER_INVALID_OUTPUT",
                "人物五视图生成服务返回了无效图片，请稍后重试。",
            )
    raise AssertionError("configured image provider path must return or raise")


def _contact_sheet_asset_key(
    *, owner_user_id: str, identity_id: str, asset_id: str, extension: str
) -> str:
    for value in (owner_user_id, identity_id, asset_id):
        validate_key_segment(value)
    if extension not in {".png", ".jpg", ".webp"}:
        raise ValueError("unsupported contact sheet extension")
    return f"users/{owner_user_id}/identities/{identity_id}/contact-sheets/{asset_id}{extension}"


def _store_contact_sheet_asset(
    conn: BusinessConnection,
    *,
    storage: StorageAdapter,
    actor: CurrentUser,
    identity_id: str,
    version_id: str,
    content: bytes,
    content_type: str,
    generation_source: str,
    attempted_keys: list[str],
    prepared_asset: PreparedSimpleCharacterAsset | None = None,
) -> str:
    """Persist the five-view contact sheet as its own downloadable asset."""
    if prepared_asset is None:
        asset_id = str(uuid.uuid4())
        key = _contact_sheet_asset_key(
            owner_user_id=actor.id,
            identity_id=identity_id,
            asset_id=asset_id,
            extension=SIMPLE_CONTACT_SHEET_EXTENSIONS[content_type],
        )
        stored = storage.put_object(key, content, content_type=content_type)
        attempted_keys.append(stored.key)
    else:
        asset_id = prepared_asset.asset_id
        stored = prepared_asset.stored
    conn.execute(
        """
        INSERT INTO assets (
            id, project_id, kind, storage_uri, sha256, size_bytes,
            content_type, created_by_user_id, metadata_json
        ) VALUES (%s, NULL, 'character_contact_sheet', %s, %s, %s, %s, %s, %s)
        """,
        (
            asset_id,
            stored.uri,
            stored.sha256,
            stored.size,
            stored.content_type,
            actor.id,
            encode_json(
                {
                    "identity_id": identity_id,
                    "character_version_id": version_id,
                    "generation_mode": SIMPLE_GENERATION_MODE,
                    "generation_source": generation_source,
                    "object_key": stored.key,
                    "purpose": "five_view_contact_sheet",
                }
            ),
        ),
    )
    return asset_id


CONTACT_SHEET_PLACEHOLDER_WIDTH = 2240
CONTACT_SHEET_PLACEHOLDER_HEIGHT = 1400


def contact_sheet_placeholder_png(
    seed: bytes,
    *,
    width: int = CONTACT_SHEET_PLACEHOLDER_WIDTH,
    height: int = CONTACT_SHEET_PLACEHOLDER_HEIGHT,
) -> bytes:
    """Locally composed five-panel placeholder mirroring the sheet layout.

    Three equal tall columns on the left plus two stacked portrait panels on
    the right, separated by thin near-white dividers, so even the fallback
    visually reads as one multi-view contact sheet.
    """
    divider = b"\xe8\xe8\xe8"
    panels = _contact_sheet_panels(width, height, seed)
    scanlines: list[bytes] = []
    for y in range(height):
        row = bytearray(b"\x00")
        cursor = 0
        for x0, y0, panel_width, panel_height, color in panels:
            if y0 <= y < y0 + panel_height:
                if x0 > cursor:
                    row += divider * (x0 - cursor)
                    cursor = x0
                row += color * panel_width
                cursor = x0 + panel_width
        if cursor < width:
            row += divider * (width - cursor)
        scanlines.append(bytes(row))
    pixels = zlib.compress(b"".join(scanlines))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", pixels)
        + png_chunk(b"IEND", b"")
    )


def _contact_sheet_panels(
    width: int, height: int, seed: bytes
) -> list[tuple[int, int, int, int, bytes]]:
    border = max(8, width // 140)
    gap = border
    inner_width = width - 2 * border
    inner_height = height - 2 * border
    left_width = round(inner_width * 0.72)
    right_width = inner_width - left_width - gap

    columns = 3
    base_column_width, remainder = divmod(left_width - (columns - 1) * gap, columns)
    panels: list[tuple[int, int, int, int, bytes]] = []
    x = border
    for index in range(columns):
        column_width = base_column_width + (1 if index < remainder else 0)
        panels.append((x, border, column_width, inner_height, _panel_color(seed, index)))
        x += column_width + gap

    base_row_height, row_remainder = divmod(inner_height - gap, 2)
    right_x = border + left_width + gap
    panels.append(
        (
            right_x,
            border,
            right_width,
            base_row_height + row_remainder,
            _panel_color(seed, columns),
        )
    )
    panels.append(
        (
            right_x,
            border + base_row_height + row_remainder + gap,
            right_width,
            base_row_height,
            _panel_color(seed, columns + 1),
        )
    )
    return panels


def _panel_color(seed: bytes, index: int) -> bytes:
    digest = hashlib.sha256(seed + bytes((index,))).digest()
    return bytes((80 + digest[0] % 120, 80 + digest[1] % 120, 80 + digest[2] % 120))
