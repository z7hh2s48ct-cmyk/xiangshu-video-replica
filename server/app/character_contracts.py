from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.json_schema import models_json_schema

IdentityStatus = Literal["DRAFT", "ACTIVE", "EXPIRED", "REVOKED", "ARCHIVED"]
AuthorizationStatus = Literal["PENDING", "AUTHORIZED", "EXPIRED", "REVOKED"]
SourceQualityStatus = Literal["PENDING", "PASSED", "FAILED", "IMPORTED"]
CharacterVersionStatus = Literal[
    "DRAFT",
    "GENERATING",
    "REVIEWING",
    "PUBLISHED",
    "FAILED",
    "ARCHIVED",
]
RequiredCharacterViewType = Literal[
    "FRONT_FACE",
    "FRONT_HALF",
    "FRONT_FULL",
    "LEFT_45",
    "LEFT_SIDE",
]
# CharacterAssetViewType 描述「库里能存在什么」：RIGHT_45/RIGHT_SIDE 是存量七资产
# 人物的历史行（镜像派生，已停止生成与发布），保留枚举值以兼容旧数据读取；
# 新人物只产生 RequiredCharacterViewType 的五个真视图。
CharacterAssetViewType = Literal[
    "FRONT_FACE",
    "FRONT_HALF",
    "FRONT_FULL",
    "LEFT_45",
    "LEFT_SIDE",
    "RIGHT_45",
    "RIGHT_SIDE",
    "IMPORTED_REFERENCE",
]
CharacterAssetReviewStatus = Literal["NOT_REVIEWED", "APPROVED", "REJECTED"]
CharacterAssetReviewDecision = Literal["APPROVED", "REJECTED"]
CharacterGenerationTaskStatus = Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED"]


class CharacterContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PersonIdentity(CharacterContract):
    id: str
    owner_user_id: str | None
    display_name: str
    authorization_status: AuthorizationStatus
    authorization_asset_id: str | None
    authorization_scope: list[str]
    authorization_expires_at: datetime | None
    source_asset_id: str | None
    source_quality_status: SourceQualityStatus
    status: IdentityStatus
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class CharacterPersona(CharacterContract):
    id: str
    identity_id: str
    name: str
    occupation: str | None
    scene_description: str | None
    appearance_constraints_json: dict[str, object]
    costume_description: str | None
    default_background: str | None
    positive_prompt: str | None
    negative_prompt: str | None
    usage_scope_json: list[str]
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class CharacterVersion(CharacterContract):
    id: str
    persona_id: str
    version_number: int
    status: CharacterVersionStatus
    source_asset_id: str | None
    source_sha256: str | None
    persona_snapshot_json: dict[str, object]
    provider: str | None
    model: str | None
    generation_params_json: dict[str, object]
    template_version: str | None
    template_hash: str | None
    required_view_types_json: list[RequiredCharacterViewType]
    published_by: str | None
    published_at: datetime | None
    publication_snapshot_json: dict[str, object] | None
    publication_hash: str | None
    created_by: str | None
    created_at: datetime


class CharacterAsset(CharacterContract):
    id: str
    character_version_id: str
    asset_id: str | None
    view_type: CharacterAssetViewType
    candidate_number: int
    generation_task_id: str | None
    auto_quality_json: dict[str, object]
    review_status: CharacterAssetReviewStatus
    is_published_selection: bool
    created_at: datetime


class CharacterAssetReview(CharacterContract):
    id: str
    character_asset_id: str
    reviewer_user_id: str | None
    decision: CharacterAssetReviewDecision
    issue_codes_json: list[str]
    comment: str | None
    created_at: datetime


class CharacterGenerationTask(CharacterContract):
    id: str
    character_version_id: str
    view_type: RequiredCharacterViewType
    provider: str
    model: str
    idempotency_key: str
    request_hash: str
    candidate_number: int
    request_snapshot_json: dict[str, object]
    status: CharacterGenerationTaskStatus
    provider_task_id: str | None
    attempt: int
    max_attempts: int
    error_code: str | None
    error_message_redacted: str | None
    cost_amount: float | None
    next_poll_at: datetime | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class CharacterReferenceSelection(CharacterContract):
    id: str
    project_id: str
    source_frame_version_id: str
    character_version_id: str
    recommended_asset_ids_json: list[str]
    selected_asset_ids_json: list[str]
    recommendation_reason_json: dict[str, object]
    character_version_snapshot_json: dict[str, object]
    selected_by: str | None
    selected_at: datetime


class ProjectCharacterAssetOption(CharacterContract):
    character_asset_id: str
    asset_id: str
    view_type: RequiredCharacterViewType


class CharacterReferenceRecommendation(CharacterContract):
    source_frame_version_id: str
    character_version_id: str
    recommended_asset_ids_json: list[str]
    candidate_assets: list[ProjectCharacterAssetOption]
    recommendation_reason_json: dict[str, object]
    character_version_snapshot_json: dict[str, object]


class ProjectCharacterVersionOption(CharacterContract):
    character_version_id: str
    version_number: int
    identity_id: str
    identity_name: str
    authorization_expires_at: datetime | None
    persona_id: str
    persona_snapshot_json: dict[str, object]
    provider: str | None
    model: str | None
    template_version: str | None
    template_hash: str | None
    published_at: datetime
    publication_hash: str
    assets: list[ProjectCharacterAssetOption]


CHARACTER_DOMAIN_MODELS = (
    PersonIdentity,
    CharacterPersona,
    CharacterVersion,
    CharacterAsset,
    CharacterAssetReview,
    CharacterGenerationTask,
    CharacterReferenceSelection,
)


def character_domain_openapi_schemas() -> dict[str, object]:
    """Publish domain contracts before their write endpoints arrive in later slices."""
    _, schema = models_json_schema(
        [(model, "validation") for model in CHARACTER_DOMAIN_MODELS],
        ref_template="#/components/schemas/{model}",
    )
    definitions = schema.get("$defs", {})
    return {str(name): value for name, value in definitions.items()}
