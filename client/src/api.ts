import { invoke, isTauri } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

import { apiBaseUrl, resolveManagedMediaUrl } from "./apiBase";
import type { components } from "./generated/api";

// MATERIAL-PERF-C（P0-6）：启动扇出约 20–40 个请求，5 秒硬超时会让任何一片
// 慢请求把对应数据降级为空（首开缺内容）。默认放宽到 10 秒，配合 loadStudioData
// 的失败切片单次重试。
export const REQUEST_TIMEOUT_MS = 10_000;
// Cloud/storage operations (diagnostics, presigned URLs, archive prechecks)
// may legitimately take much longer than a normal API round-trip.
const CLOUD_OP_TIMEOUT_MS = 60_000;
export const SESSION_EXPIRED_EVENT = "video-replica:session-expired";
let internalAccessToken: string | null = null;
let customerSessionToken: string | null = null;
let customerSessionOwner: symbol | null = null;
let workspaceCredentialEpoch = 0;
// The customer-production admin session exchanges its CSRF value once and
// keeps it in memory only.  Control-plane writes share this value so the
// existing account/billing screens stay behind the same per-operator session
// rather than a legacy proxy identity.  It is deliberately never persisted.
let adminCsrfToken: string | null = null;

export function setAdminCsrfToken(token: string): void {
  adminCsrfToken = token;
}

export function getAdminCsrfToken(): string | null {
  return adminCsrfToken;
}

export function clearAdminCsrfToken(): void {
  adminCsrfToken = null;
}

// 地址解析与站内媒体地址绝对化见 ./apiBase；此处 re-export 保持既有导入路径。
export { resolveApiBaseUrl, resolveManagedMediaUrl } from "./apiBase";

type HealthResponse = components["schemas"]["HealthResponse"];
export type UserRole = "employee" | "admin" | "auditor" | "customer";

export type CurrentUser = {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
};

// Task #7 / PR #65 (Codex P1): the wallet and recharge adapter types derive
// from the regenerated OpenAPI contract instead of handwritten shapes, so the
// schema-drift gate (tsc) catches a later server response change. The internal
// and customer lanes share these aliases; the Control* views extend them.
type OpenApiWalletSnapshot = components["schemas"]["WalletResponse"];
export type WalletSnapshot = Omit<
  OpenApiWalletSnapshot,
  "internal_unit_price_fen" | "min_recharge_fen" | "recharge_step_fen"
> & {
  internal_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
};
export type WalletTransaction =
  components["schemas"]["WalletTransactionResponse"];
export type WalletTransactionPage =
  components["schemas"]["WalletTransactionPage"];
export type RechargeOrderStatus =
  components["schemas"]["RechargeOrderStatusResponse"]["status"];
export type RechargeOrder =
  components["schemas"]["RechargeOrderStatusResponse"];
export type RechargeOrderPage = components["schemas"]["RechargeOrderPage"];
export type CreatedRechargeOrder =
  components["schemas"]["RechargeOrderResponse"];

export type ControlAccount = {
  id: string;
  username: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  available_credits: number;
  reserved_credits: number;
  active_token_count: number;
};

export type ControlAccountPage = {
  items: ControlAccount[];
  total: number;
  limit: number;
  offset: number;
};

export type ControlRechargeOrder = RechargeOrder & {
  id: string;
  user_id: string;
  username: string;
  display_name: string;
  provider: string;
  /** ZPay settles here; a WeChat Native order keeps it null by constraint. */
  provider_trade_no: string | null;
  /** WeChat Native's trade reference; null for every other provider. */
  transaction_id: string | null;
};

export type ControlRechargeOrderPage = {
  items: ControlRechargeOrder[];
  total: number;
  limit: number;
  offset: number;
};

export type ControlWalletTransaction = WalletTransaction & {
  username: string;
  source_id?: string | null;
};

export type ControlWalletTransactionPage = {
  items: ControlWalletTransaction[];
  total: number;
  limit: number;
  offset: number;
};

export type BillingSettings = {
  internal_base_unit_price_fen: number;
  charged_unit_price_fen: number;
  oral_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
};

export type ControlSettings = {
  providers: Record<ProviderName, ProviderSettings>;
  runtime: RuntimeSettings;
  billing: BillingSettings;
  zpay: {
    provider: "zpay";
    configured: boolean;
    config: Record<string, string>;
  };
  deployment: {
    gateway_url: string;
    notify_url: string;
    return_url: string;
  };
};

export type ControlReconciliation = {
  wallet_count: number;
  wallet_mismatch_count: number;
  paid_order_without_charge_count: number;
  charge_without_paid_order_count: number;
  pending_order_count: number;
};

export type GenerationVersion = Omit<
  components["schemas"]["VersionResult"],
  "payload"
> & { payload: Record<string, unknown> };
export type GenerationVersionState = Omit<
  components["schemas"]["VersionState"],
  "version"
> & { version: GenerationVersion | null };
export type ScriptVersionInput = components["schemas"]["ScriptRequest"];
export type PromptCompileInput = Omit<
  components["schemas"]["PromptCompileRequest"],
  "timeline_policy" | "opening_action"
> & {
  ratio?: GenerationRatio;
  timeline_policy?: "preserve" | "scale_confirmed";
  opening_action?: string;
};
export type PromptRevisionInput =
  components["schemas"]["PromptRevisionRequest"];
export type PromptContext = {
  final_prompt_version_id?: string | null;
  source?: "analysis" | "manual" | "ai" | "imported";
  analysis_version_id?: string | null;
  shot_card_version_id?: string | null;
  script_version_id?: string | null;
  optimization_task_id?: string | null;
  context_hash?: string | null;
};
export type GenerationBatchInput = Omit<
  components["schemas"]["GenerationBatchRequest"],
  "prompt_version_id"
> & {
  prompt_version_id?: string;
  prompt_text?: string;
  prompt_context?: PromptContext;
  ratio?: GenerationRatio;
};
export type GenerationRuntimeLimits =
  components["schemas"]["GenerationRuntimeLimits"];
export type GenerationRatio =
  | "adaptive"
  | "21:9"
  | "16:9"
  | "4:3"
  | "1:1"
  | "3:4"
  | "9:16";
export type GenerationPriceQuote = {
  resolution: "768P" | "2K";
  duration_seconds: number;
  quantity: number;
  unit_price_fen_per_second: number;
  estimated_seconds: number;
  estimated_price_fen: number;
  unit_credits?: number;
  estimated_credits?: number;
  credit_price_version?: number;
  /** 客户套餐折扣率 4 位小数字符串（如 "0.9000"）；None/缺省 = 无折扣。 */
  discount_rate?: string | null;
  /** 折扣来源 token（"recharge_package" / "manual"）；仅当 discount_rate 非空时有意义。 */
  discount_source?: string | null;
};
export type SavedPromptInput = {
  generation_context?: PromptGenerationContext;
  name: string;
  prompt_text: string;
  base_prompt_version_id?: string;
};
export type GenerationTask = Omit<
  components["schemas"]["TaskResult"],
  "prompt_snapshot"
> & { prompt_snapshot: Record<string, unknown> | null };
export type BatchProgress = components["schemas"]["BatchProgress"];
export type GenerationBatch = Omit<
  components["schemas"]["BatchResult"],
  "tasks"
> & { tasks: GenerationTask[] };
export type GenerationTaskSummary = components["schemas"]["TaskSummary"];
export type GenerationTaskRetryInput =
  components["schemas"]["GenerationTaskRetryRequest"];
export type ConfirmNotChargedInput =
  components["schemas"]["ConfirmNotChargedRequest"];
export type ReconcileGenerationTaskInput =
  components["schemas"]["ReconcileGenerationTaskRequest"];
export type GenerationReconcileOperation = {
  id: string;
  task_id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  attempt: number;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};
export type PaidRegenerationInput =
  components["schemas"]["PaidRegenerationRequest"];
export type GenerationBatchListItem = Omit<
  components["schemas"]["GenerationBatchListItem"],
  "tasks"
> & { tasks: GenerationTaskSummary[] };
export type GenerationBatchListPage = Omit<
  components["schemas"]["GenerationBatchListPage"],
  "items" | "total"
> & { items: GenerationBatchListItem[]; total?: number };
export type GenerationBatchListFilters = {
  projectId?: string;
  createdByUserId?: string;
  status?:
    | "PENDING"
    | "QUEUED"
    | "NEEDS_ATTENTION"
    | "SUCCEEDED"
    | "COMPLETED_WITH_FAILURES";
  needsAttention?: boolean;
  limit?: number;
  cursor?: string;
};

export type ProviderName =
  | "metaso"
  | "apilio"
  | "cos"
  | "deepseek"
  | "hifly"
  | "tikhub"
  | "dashscope"
  | "douyidou";

export type ProviderSettings = {
  provider: ProviderName;
  configured: boolean;
  config: Record<string, string>;
};

export type RuntimeSettings = {
  max_generation_count_per_batch: number;
  max_concurrent_h3_tasks: number;
  active_storage_provider: "cos" | "local";
  /** M4/M5 review M2: PostgreSQL-only rollout switch (SQLite lane keeps the
   * legacy global FIFO and rejects a provided value with 422). Absent on the
   * desktop lane and when the caller leaves it unchanged. */
  fair_queue_enabled?: boolean;
};

export type SettingsSnapshot = {
  providers: Record<ProviderName, ProviderSettings>;
  runtime: RuntimeSettings;
  billing: BillingSettings;
};

export type DiagnosticProviderResult = {
  provider: ProviderName;
  status: "ok" | "not_configured" | "configured_only" | "error";
  configured_fields: string[];
  adapter_capability: "configuration_only" | "connection_test";
  test_kind: string;
  http_status?: number | null;
  error_code?: string | null;
  failure_phase?: string | null;
  cleanup_failed?: boolean | null;
  latency_ms?: number | null;
  message: string;
};

export type SettingsDiagnosticReport = {
  id: string;
  status: "ok" | "attention";
  providers: DiagnosticProviderResult[];
  download_url: string;
};

export type Project = {
  id: string;
  owner_user_id: string;
  name: string;
  status: string;
  reference_asset_id: string | null;
  reference_upload_status: "NOT_STARTED" | "UPLOAD_PENDING" | "READY";
  analysis_status: "NOT_READY" | "PENDING" | "READY" | "FAILED";
  analysis_task_id?: string | null;
  analysis_error_message?: string | null;
  analysis_retryable?: boolean;
};

export type UploadIntent = {
  asset_id: string;
  project_id: string;
  storage_key?: string | null;
  method: "PUT" | null;
  url: string | null;
  headers: Record<string, string>;
  expires_at: string | null;
  upload_required?: boolean;
};

export type MaterialItem = components["schemas"]["MaterialItem"];
export type MaterialPage = components["schemas"]["MaterialPage"];
export type MaterialResolveResponse =
  components["schemas"]["MaterialResolveResponse"];
export type MaterialUpdate = components["schemas"]["MaterialUpdateRequest"];
export type MaterialUploadIntent =
  components["schemas"]["MaterialUploadIntentResponse"];
export type MaterialGroupItem = components["schemas"]["MaterialGroupItem"];
export type MaterialGroupsResponse =
  components["schemas"]["MaterialGroupsResponse"];
export type MaterialBulkUpdate = components["schemas"]["MaterialBulkUpdate"];
export type MaterialBulkRequest = components["schemas"]["MaterialBulkRequest"];
export type MaterialBulkResult = components["schemas"]["MaterialBulkResult"];

export type CompletedUpload = {
  asset_id: string;
  project_id: string;
  status: string;
  storage_uri?: string | null;
  sha256: string;
  size_bytes: number;
  content_type: string;
  metadata: { duration_seconds: number };
  analysis_task_id: string | null;
  analysis_task_status: AnalysisTask["status"] | null;
};

export type AnalysisVersion = {
  id: string;
  project_id: string;
  asset_id: string | null;
  kind: string;
  version_number: number;
  payload: Record<string, unknown>;
  created_by_user_id: string | null;
  created_at: string;
};

export type AnalysisTask = {
  id: string;
  project_id: string;
  asset_id: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  attempt: number;
  result_version_id: string | null;
  error_code: string | null;
  error_message: string | null;
  failure_phase: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};

const analysisTaskWaiters = new Map<string, Promise<AnalysisTask>>();
const analysisTaskObservers = new Map<
  string,
  Set<(task: AnalysisTask) => void>
>();
export type AnalysisProvider = "apilio_gemini" | "fake_gemini";

export type ShotMotion = {
  subject_motion_state:
    | "STATIC"
    | "WALKING"
    | "RUNNING"
    | "TURNING"
    | "GESTURING_ONLY"
    | "OBJECT_MOTION"
    | "NO_PERSON";
  subject_direction:
    | "toward_camera"
    | "away_from_camera"
    | "left"
    | "right"
    | "lateral"
    | "in_place"
    | "none";
  subject_displacement: string;
  hand_action: string;
  camera_motion:
    | "STATIC"
    | "PUSH_IN"
    | "PULL_BACK"
    | "HANDHELD_TRACKING"
    | "PAN"
    | "TILT"
    | "FOLLOW";
  relative_motion: string;
};

export type ShotCard = {
  shot_id: string;
  start_time: number;
  end_time: number;
  shot_type: string;
  composition: string;
  camera_motion: string;
  subject: string;
  person_count?: number | null;
  action: string;
  scene: string;
  spoken_text: string;
  transition: string;
  motion?: ShotMotion | null;
  segment_kind?: "SHOT_CUT" | "ACTION_BEAT" | null;
  boundary_reason?: string | null;
};

export type AnalysisPayload = {
  summary: string;
  duration_seconds: number;
  original_script: string;
  shots: ShotCard[];
};

export type ShotCardPayload = {
  source_analysis_version_id: string;
  duration_seconds: number;
  shots: ShotCard[];
};

export type Character = {
  id: string;
  name: string;
  reference_asset_ids: string[];
  authorization_project_ids: string[];
  authorization_expires_at: string | null;
  is_active: boolean;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
};

export type ProjectMainCharacter = Omit<
  components["schemas"]["ProjectMainCharacterResponse"],
  "character_snapshot"
> & {
  character_snapshot: ProjectCharacterSnapshot;
};

export type ProjectCharacterAssetOption =
  components["schemas"]["ProjectCharacterAssetOption"];

export type ProjectCharacterVersionOption = Omit<
  components["schemas"]["ProjectCharacterVersionOption"],
  "persona_snapshot_json"
> & {
  persona_snapshot_json: Record<string, unknown>;
};

export type ProjectCharacterSnapshot = {
  name?: string;
  schema_version?: string;
  character_version_id?: string;
  character_version_number?: number;
  identity?: {
    id?: string;
    display_name?: string;
    authorization_expires_at?: string | null;
  };
  persona_id?: string;
  persona_snapshot_json?: Record<string, unknown>;
  provider?: string | null;
  model?: string | null;
  template_version?: string | null;
  template_hash?: string | null;
  published_at?: string;
  publication_hash?: string;
  published_assets?: ProjectCharacterAssetOption[];
};

export type SourceFrameCandidate = {
  asset_id: string;
  timestamp_seconds: number;
  score: number | null;
  technical_score?: number | null;
  semantic_score?: number | null;
  selection_reason?: string;
};

export type SourceFrameCandidates = {
  requested_timestamps_seconds: number[];
  semantic_quality_status: "VERIFIED" | "UNAVAILABLE" | "NOT_REQUESTED";
  candidates: SourceFrameCandidate[];
};

export type SourceFrameSelectionState = {
  version: AnalysisVersion | null;
  stale: boolean;
};

export type SourceFrameTask = {
  id: string;
  project_id: string;
  asset_id: string;
  timestamps_seconds: number[];
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  attempt: number;
  result_version_id: string | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export class SourceFrameTaskFailedError extends Error {
  readonly task: SourceFrameTask;

  constructor(task: SourceFrameTask) {
    super(task.error_message || "候选源画面提取失败，请重新提交。");
    this.name = "SourceFrameTaskFailedError";
    this.task = task;
  }
}

const sourceFrameTaskWaiters = new Map<string, Promise<SourceFrameTask>>();

export type SourceFrameCharacterFeatures =
  components["schemas"]["SourceFrameCharacterFeatures"];

export type CharacterReferenceRecommendation = Omit<
  components["schemas"]["CharacterReferenceRecommendation"],
  "character_version_snapshot_json" | "recommendation_reason_json"
> & {
  character_version_snapshot_json: Record<string, unknown>;
  recommendation_reason_json: Record<string, unknown>;
};

export type CharacterReferenceSelection = Omit<
  components["schemas"]["CharacterReferenceSelection"],
  "character_version_snapshot_json" | "recommendation_reason_json"
> & {
  character_version_snapshot_json: Record<string, unknown>;
  recommendation_reason_json: Record<string, unknown>;
};

export type SelectCharacterReferencesInput =
  components["schemas"]["SelectCharacterReferencesRequest"];

export type FirstFrameModel = "gpt-image-2" | "nano-banana-pro-2k";

export type GenerateFirstFramesInput =
  components["schemas"]["GenerateFirstFramesRequest"];

export type FirstFrameTaskStage =
  components["schemas"]["FirstFrameTaskResponse"]["stage"];

export interface FirstFrameTask {
  id: string;
  project_id: string;
  status: DurableImageTaskStatus;
  stage: FirstFrameTaskStage;
  attempt: number;
  result_version_id: string | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export type FirstFrameCandidate = {
  asset_id: string;
  storage_key: string;
  storage_uri: string;
  sha256: string;
  size_bytes: number;
  content_type: string;
  quality?: {
    passed: boolean;
    attempt: number;
    issue_codes: string[];
    inspection: Record<string, unknown>;
  } | null;
};

export type FirstFrameCandidates = {
  replace_scene?: boolean;
  aspect_ratio?: GenerateFirstFramesInput["aspect_ratio"];
  review_mode?: "HUMAN_CONFIRMATION" | "AUTOMATIC_QUALITY";
  source_frame_asset_id?: string;
  character_reference_asset_ids?: string[];
  provider: string;
  model: FirstFrameModel;
  prompt: string;
  candidates: FirstFrameCandidate[];
  reconstruction_mode: string | null;
  character_contract: Record<string, unknown> | null;
  project_character_appearance_version_id: string | null;
  project_appearance: ProjectAppearanceSpec | null;
};

export type ProjectAppearanceSpec = {
  category: string;
  scene: string;
  subject: string;
  outfit_description: string;
  selection_reason: string;
};

export type FirstFrameSelectionState = {
  version: AnalysisVersion | null;
  stale: boolean;
};

export type FirstFrameSelectionPayload = {
  first_frame_candidates_version_id: string;
  first_frame_asset_id: string;
};

export type DownloadUrl = { url: string };

export type PersonIdentity = components["schemas"]["PersonIdentity"];
export type CharacterPersona = Omit<
  components["schemas"]["CharacterPersona"],
  "appearance_constraints_json"
> & { appearance_constraints_json: Record<string, unknown> };
export type CharacterVersion = Omit<
  components["schemas"]["CharacterVersion"],
  | "generation_params_json"
  | "persona_snapshot_json"
  | "publication_snapshot_json"
> & {
  generation_params_json: Record<string, unknown>;
  persona_snapshot_json: Record<string, unknown>;
  publication_snapshot_json: Record<string, unknown> | null;
};
export type CharacterAsset = Omit<
  components["schemas"]["CharacterAsset"],
  "auto_quality_json"
> & { auto_quality_json: Record<string, unknown> };
export type CharacterAssetReview =
  components["schemas"]["CharacterAssetReview"];
export type CharacterGenerationTask = Omit<
  components["schemas"]["CharacterGenerationTask"],
  "request_snapshot_json"
> & { request_snapshot_json: Record<string, unknown> };
export type IdentityUploadPurpose = "authorization" | "source";
export type IdentityUploadIntent =
  components["schemas"]["CreatedIdentityUploadIntent"];
export type CompletedIdentitySource =
  components["schemas"]["CompletedSourceImage"];
export type RequiredCharacterViewType =
  components["schemas"]["CharacterGenerationTask"]["view_type"];
export type CharacterReviewDecision = "APPROVED" | "REJECTED";

export type CharacterPersonaInput = {
  name: string;
  occupation?: string | null;
  scene_description?: string | null;
  appearance_constraints_json?: Record<string, unknown>;
  costume_description?: string | null;
  default_background?: string | null;
  positive_prompt?: string | null;
  negative_prompt?: string | null;
  usage_scope_json?: string[];
};

export type CharacterVersionInput = {
  provider: string;
  model: string;
  generation_params_json: Record<string, unknown>;
};

export async function getHealth(): Promise<HealthResponse> {
  return requestJson<HealthResponse>("/health", "本地服务暂不可用");
}

export type StudioStats = {
  today_completed: number;
  running: number;
  queued: number;
  needs_attention: number;
  total_completed: number;
};

/** 平台侧真实工作台统计（C6/C10a）：GET /api/studio/stats。
 * 统计可见普通生成和口播任务；播放/互动等外部平台数据不在其中。 */
export async function getStudioStats(): Promise<StudioStats> {
  return requestApiJson<StudioStats>("/api/studio/stats", "读取工作台统计失败");
}

export type StudioAnalyticsDay = {
  day: string;
  completed: number;
  failed: number;
};
export type StudioAnalyticsKindCount = { kind: string; completed: number };
export type StudioAnalyticsWorkItem = {
  task_id: string;
  task_kind: "generation" | "oral";
  batch_id: string | null;
  project_id: string | null;
  title: string;
  creation_kind: string;
  completed_at: string;
  /** 按秒计费实际消耗（秒=积分）；无计费记录（历史数据/未启计费）为 null。 */
  cost_credits: number | null;
};

/** 平台侧真实成片聚合（C6 数据看板）：GET /api/studio/analytics。
 * 窗口内按北京日界分桶的普通生成/口播产出项，并单列去重普通生成批次；
 * 播放/互动等外部平台数据不在其中。 */
export type StudioAnalytics = {
  range_days: number;
  generated_at: string;
  today_completed: number;
  range_completed: number;
  total_completed: number;
  today_generation_batches: number;
  range_generation_batches: number;
  total_generation_batches: number;
  range_generation_outputs: number;
  range_oral_outputs: number;
  daily: StudioAnalyticsDay[];
  kind_breakdown: StudioAnalyticsKindCount[];
  recent_works: StudioAnalyticsWorkItem[];
};

export async function getStudioAnalytics(
  days: 7 | 30,
): Promise<StudioAnalytics> {
  return requestApiJson<StudioAnalytics>(
    `/api/studio/analytics?days=${days}`,
    "读取数据看板统计失败",
  );
}

export type StudioNotificationPreferences = { enabled: boolean };

/** 通知偏好（C10b）：GET /api/studio/notification-preferences。
 * 未保存过的用户按服务端默认（开启）返回。 */
export async function getStudioNotificationPreferences(): Promise<StudioNotificationPreferences> {
  return requestApiJson<StudioNotificationPreferences>(
    "/api/studio/notification-preferences",
    "读取通知偏好失败",
  );
}

export async function updateStudioNotificationPreferences(
  enabled: boolean,
): Promise<StudioNotificationPreferences> {
  return requestApiJson<StudioNotificationPreferences>(
    "/api/studio/notification-preferences",
    "保存通知偏好失败",
    {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    },
  );
}

export type StudioDraftKind = "copy" | "oral" | "replica" | "publishing";

export type StudioDraftCloudRecord = {
  draft_kind: StudioDraftKind;
  payload: Record<string, unknown>;
  script_confirmed: boolean;
  revision: number;
  updated_at: string;
};

export type StudioSavedScriptRecord = {
  script_id: string;
  title: string;
  text: string;
  original: string | null;
  version: number;
  ip_id: string | null;
  source_project_id: string | null;
  source_kind: string | null;
  created_at: string;
  updated_at: string;
};

export type StudioSavedScriptInput = {
  script_id: string;
  title: string;
  text: string;
  original?: string | null;
  version: number;
  ip_id?: string | null;
  source_project_id?: string | null;
  source_kind?: string | null;
};

/** 云端工作草稿（C7）：GET /api/studio/drafts/{kind}，404 表示尚无草稿。 */
export async function getStudioDraft(
  kind: StudioDraftKind,
): Promise<StudioDraftCloudRecord> {
  return requestApiJson<StudioDraftCloudRecord>(
    `/api/studio/drafts/${encodeURIComponent(kind)}`,
    "读取云端草稿失败",
  );
}

/** 云端工作草稿自动保存（C7）：PUT /api/studio/drafts/{kind}，last-write-wins。 */
export async function saveStudioDraft(
  kind: StudioDraftKind,
  payload: Record<string, unknown>,
  scriptConfirmed: boolean,
  expectedRevision?: number,
): Promise<StudioDraftCloudRecord> {
  return requestApiJson<StudioDraftCloudRecord>(
    `/api/studio/drafts/${encodeURIComponent(kind)}`,
    "保存云端草稿失败",
    {
      method: "PUT",
      body: JSON.stringify({
        payload,
        script_confirmed: scriptConfirmed,
        expected_revision: expectedRevision,
      }),
    },
  );
}

/** 放弃云端工作草稿：DELETE /api/studio/drafts/{kind}。 */
export async function deleteStudioDraft(kind: StudioDraftKind): Promise<void> {
  await requestApiJson<{ deleted: boolean }>(
    `/api/studio/drafts/${encodeURIComponent(kind)}`,
    "删除云端草稿失败",
    { method: "DELETE" },
  );
}

/** 我的文案列表（C7）：GET /api/studio/saved-scripts，最新在前，上限 50。 */
export async function listStudioSavedScripts(): Promise<
  StudioSavedScriptRecord[]
> {
  const page = await requestApiJson<{ items: StudioSavedScriptRecord[] }>(
    "/api/studio/saved-scripts",
    "读取我的文案失败",
  );
  return page.items;
}

export async function getStudioSavedScript(
  scriptId: string,
): Promise<StudioSavedScriptRecord> {
  return requestApiJson<StudioSavedScriptRecord>(
    `/api/studio/saved-scripts/${encodeURIComponent(scriptId)}`,
    "读取保存文案失败",
  );
}

/** 保存/覆盖一条我的文案（按 script_id 幂等）。 */
export async function saveStudioSavedScript(
  input: StudioSavedScriptInput,
): Promise<StudioSavedScriptRecord> {
  return requestApiJson<StudioSavedScriptRecord>(
    "/api/studio/saved-scripts",
    "保存我的文案失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

/** 删除一条我的文案。 */
export async function deleteStudioSavedScript(scriptId: string): Promise<void> {
  await requestApiJson<{ deleted: boolean }>(
    `/api/studio/saved-scripts/${encodeURIComponent(scriptId)}`,
    "删除我的文案失败",
    { method: "DELETE" },
  );
}

export type ScriptFromAudioTask = Omit<
  components["schemas"]["ScriptFromAudioTaskResponse"],
  "status"
> & {
  status:
    | "PENDING"
    | "RUNNING"
    | "SUCCEEDED"
    | "FAILED"
    | "SUBMISSION_UNCERTAIN";
};

/** 提交"提取文案"异步任务（202）：上传视频 → 抽音轨 → ASR 转写。 */
export async function createScriptFromAudioTask(
  projectId: string,
  sourceAssetId: string,
  idempotencyKey: string,
): Promise<ScriptFromAudioTask> {
  return requestApiJson<ScriptFromAudioTask>(
    `/api/projects/${encodeURIComponent(projectId)}/script-from-audio`,
    "提交文案提取任务失败",
    {
      method: "POST",
      body: JSON.stringify({
        source_asset_id: sourceAssetId,
        idempotency_key: idempotencyKey,
      }),
    },
  );
}

/** 按提交回执中的任务 ID 读取转写，避免项目 latest 被其他任务替换。 */
export async function getScriptFromAudioTask(
  taskId: string,
): Promise<ScriptFromAudioTask> {
  return requestApiJson<ScriptFromAudioTask>(
    `/api/script-from-audio-tasks/${encodeURIComponent(taskId)}`,
    "读取文案提取任务失败",
  );
}

/** 读取项目最近的提取文案任务；尚无任务返回 null。 */
export async function getLatestScriptFromAudioTask(
  projectId: string,
): Promise<ScriptFromAudioTask | null> {
  return requestApiJson<ScriptFromAudioTask | null>(
    `/api/projects/${encodeURIComponent(projectId)}/script-from-audio-tasks/latest`,
    "读取文案提取任务失败",
  );
}

export type OralPrice = {
  budget_seconds?: number;
  estimated_credits?: number;
  unit_price_fen: number;
  unit_credits?: number;
  credit_price_version?: number;
};

/** 按秒报价；预算与提交使用相同的服务端计算。 */
export async function getOralPrice(input?: {
  script_text?: string;
  audio_asset_id?: string;
}): Promise<OralPrice> {
  return input
    ? requestApiJson<OralPrice>("/api/oral/quote", "读取口播报价失败", {
        method: "POST",
        body: JSON.stringify(input),
        headers: { "Content-Type": "application/json" },
      })
    : requestApiJson<OralPrice>("/api/oral/price", "读取口播报价失败");
}

export type OralAvatarRecord = {
  id: string;
  identity_id: string;
  title: string;
  status: "PENDING" | "RUNNING" | "READY" | "FAILED";
  submission_state:
    | "LOCAL_PENDING"
    | "SUBMITTING"
    | "SUBMITTED"
    | "SUBMISSION_UNKNOWN"
    | "FAILED";
  source_kind: "VIDEO" | "IMAGE";
  source_asset_id: string;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type OralVoiceRecord = {
  id: string;
  identity_id: string;
  title: string;
  status: "PENDING" | "RUNNING" | "READY" | "FAILED";
  submission_state:
    | "LOCAL_PENDING"
    | "SUBMITTING"
    | "SUBMITTED"
    | "SUBMISSION_UNKNOWN"
    | "FAILED";
  source_asset_id: string;
  demo_asset_id: string | null;
  confirmed: number | boolean;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type OralCloneCreated = { id: string; status: string };
export type OralConsentCreated = { id: string };

export async function createOralConsent(input: {
  identityId: string;
  sourceAssetId: string;
  purpose: "AVATAR" | "VOICE";
}): Promise<OralConsentCreated> {
  return requestApiJson<OralConsentCreated>(
    "/api/oral/consents",
    "保存人物素材授权失败",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        identity_id: input.identityId,
        source_asset_id: input.sourceAssetId,
        purpose: input.purpose,
      }),
    },
  );
}

/** 读取指定人物的口播分身。 */
export async function listOralAvatars(
  identityId: string,
): Promise<OralAvatarRecord[]> {
  return requestApiJson<OralAvatarRecord[]>(
    `/api/oral/avatars?identity_id=${encodeURIComponent(identityId)}`,
    "读取口播分身失败",
  );
}

/** 读取指定人物的声音档案。 */
export async function listOralVoices(
  identityId: string,
): Promise<OralVoiceRecord[]> {
  return requestApiJson<OralVoiceRecord[]>(
    `/api/oral/voices?identity_id=${encodeURIComponent(identityId)}`,
    "读取声音档案失败",
  );
}

export async function createOralAvatarClone(input: {
  identityId: string;
  title: string;
  sourceAssetId: string;
  sourceKind: "VIDEO";
  consentId: string;
  idempotencyKey: string;
}): Promise<OralCloneCreated> {
  return requestApiJson<OralCloneCreated>(
    "/api/oral/avatars",
    "提交口播分身制作失败",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        identity_id: input.identityId,
        title: input.title,
        source_asset_id: input.sourceAssetId,
        source_kind: input.sourceKind,
        consent_id: input.consentId,
        idempotency_key: input.idempotencyKey,
      }),
    },
  );
}

export async function createOralVoiceClone(input: {
  identityId: string;
  title: string;
  sourceAssetId: string;
  consentId: string;
  idempotencyKey: string;
}): Promise<OralCloneCreated> {
  return requestApiJson<OralCloneCreated>(
    "/api/oral/voices",
    "提交声音克隆失败",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        identity_id: input.identityId,
        title: input.title,
        source_asset_id: input.sourceAssetId,
        consent_id: input.consentId,
        idempotency_key: input.idempotencyKey,
      }),
    },
  );
}

export async function refreshOralAvatar(
  avatarId: string,
): Promise<OralAvatarRecord> {
  return requestApiJson<OralAvatarRecord>(
    `/api/oral/avatars/${encodeURIComponent(avatarId)}/refresh`,
    "刷新口播分身状态失败",
    { method: "POST" },
  );
}

export async function refreshOralVoice(
  voiceId: string,
): Promise<OralVoiceRecord> {
  return requestApiJson<OralVoiceRecord>(
    `/api/oral/voices/${encodeURIComponent(voiceId)}/refresh`,
    "刷新声音克隆状态失败",
    { method: "POST" },
  );
}

export async function confirmOralVoice(
  voiceId: string,
): Promise<OralVoiceRecord> {
  return requestApiJson<OralVoiceRecord>(
    `/api/oral/voices/${encodeURIComponent(voiceId)}/confirm`,
    "确认声音失败",
    { method: "POST" },
  );
}

export type OralCloneDeleted = { id: string; deleted_at: string };

/**
 * 软删除口播分身。后端仅本地隐藏（deleted_at 标记），源视频资产保留；
 * 分身仍在制作中或被进行中的口播任务引用时返回 409 ORAL_RESOURCE_IN_USE，
 * 其 message 已是可直接展示的中文原因。
 */
export async function deleteOralAvatar(
  avatarId: string,
): Promise<OralCloneDeleted> {
  return requestApiJson<OralCloneDeleted>(
    `/api/oral/avatars/${encodeURIComponent(avatarId)}`,
    "删除口播分身失败",
    { method: "DELETE" },
  );
}

/** 软删除声音档案。后端仅本地隐藏，源音频与试听资产保留；冲突时同 409。 */
export async function deleteOralVoice(
  voiceId: string,
): Promise<OralCloneDeleted> {
  return requestApiJson<OralCloneDeleted>(
    `/api/oral/voices/${encodeURIComponent(voiceId)}`,
    "删除声音失败",
    { method: "DELETE" },
  );
}

export type OralTaskRequest = {
  identityId: string;
  avatarId: string;
  voiceId?: string;
  mode: "TTS" | "AUDIO";
  title: string;
  scriptText?: string;
  audioAssetId?: string;
  subtitle?: Record<string, unknown>;
  idempotencyKey: string;
};

export type OralTaskCreated = {
  id: string;
  status: string;
  estimated_cost_fen: number;
  replayed: boolean;
};

/** 创建数字人口播任务（POST /api/oral/tasks）。 */
export async function createOralTask(
  input: OralTaskRequest,
): Promise<OralTaskCreated> {
  return requestApiJson<OralTaskCreated>(
    "/api/oral/tasks",
    "口播任务提交失败",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        identity_id: input.identityId,
        avatar_id: input.avatarId,
        voice_id: input.voiceId ?? null,
        mode: input.mode,
        title: input.title,
        script_text: input.scriptText ?? null,
        audio_asset_id: input.audioAssetId ?? null,
        subtitle: input.subtitle ?? null,
        idempotency_key: input.idempotencyKey,
      }),
    },
  );
}

export type OralTaskRecord = components["schemas"]["OralTaskResponse"];

export type OffsetPage<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};

/** 数字人口播任务分页（GET /api/oral/tasks）。 */
export async function listOralTasksPage({
  limit = 20,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<components["schemas"]["OralTaskPageResponse"]> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  return requestApiJson<components["schemas"]["OralTaskPageResponse"]>(
    `/api/oral/tasks?${query.toString()}`,
    "读取口播任务失败",
  );
}

export async function listOralTasks(limit = 20): Promise<OralTaskRecord[]> {
  return (await listOralTasksPage({ limit })).items;
}

export function getOralTask(taskId: string): Promise<OralTaskRecord> {
  return requestApiJson<OralTaskRecord>(
    `/api/oral/tasks/${encodeURIComponent(taskId)}`,
    "读取口播任务失败",
  );
}

function mutateOralTask(taskId: string, action: string, error: string) {
  return requestApiJson<OralTaskRecord>(
    `/api/oral/tasks/${encodeURIComponent(taskId)}/${action}`,
    error,
    { method: "POST" },
  );
}

export function cancelOralTask(taskId: string): Promise<OralTaskRecord> {
  return mutateOralTask(taskId, "cancel", "取消口播任务失败");
}

export function retryOralTaskArchive(taskId: string): Promise<OralTaskRecord> {
  return mutateOralTask(taskId, "archive-retry", "重试归档口播成片失败");
}

export async function getCurrentUser(): Promise<CurrentUser> {
  const user = await requestApiJson<unknown>("/api/auth/me", "身份验证失败");
  if (!isCurrentUser(user)) {
    throw new Error("身份验证失败：本地服务返回的用户信息无效");
  }
  return user;
}

export function setInternalAccessToken(token: string | null): void {
  workspaceCredentialEpoch += 1;
  bumpDownloadUrlCacheEpoch();
  const normalized = token?.trim() ?? "";
  internalAccessToken = normalized || null;
}

/** Keep the active customer workspace credential in memory only.  The Tauri
 * vault remains the persistent source; this bridge exists solely so the
 * shared project/analysis/generation API adapter can authenticate requests. */
export function setCustomerSessionToken(token: string | null): void {
  workspaceCredentialEpoch += 1;
  bumpDownloadUrlCacheEpoch();
  const normalized = token?.trim() ?? "";
  customerSessionToken = normalized || null;
  customerSessionOwner = null;
}

/** Attach a workspace-owned credential and return an ownership-aware cleanup.
 * A stale React tree may unmount after a newer tree has already attached its
 * token; its cleanup must not clear the newer session. */
export function attachCustomerSessionToken(token: string): () => void {
  const normalized = token.trim();
  if (!normalized) {
    throw new Error("Customer session token is required");
  }
  const owner = Symbol("customer-workspace-session");
  workspaceCredentialEpoch += 1;
  bumpDownloadUrlCacheEpoch();
  customerSessionToken = normalized;
  customerSessionOwner = owner;
  return () => {
    if (customerSessionOwner === owner) {
      workspaceCredentialEpoch += 1;
      customerSessionToken = null;
      customerSessionOwner = null;
    }
  };
}

function workspaceAccessToken(): string | null {
  // CW-015: the customer session token takes precedence — the internal access
  // token no longer wins. In the customer build the internal App shell is
  // unreachable (CW-013), so `internalAccessToken` is never set there and
  // business requests authenticate solely with the customer session; the
  // internal token remains only for the legacy internal shell until CW-041
  // retires it. Error recovery therefore never falls back to internal identity.
  return customerSessionToken ?? internalAccessToken;
}

export async function getWallet(): Promise<WalletSnapshot> {
  const wallet = await requestApiJson<OpenApiWalletSnapshot>(
    "/api/wallet",
    "读取钱包失败",
  );
  return requireWalletPricing(wallet);
}

export async function listWalletTransactions({
  limit = 20,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<WalletTransactionPage> {
  return requestApiJson<WalletTransactionPage>(
    `/api/wallet/transactions?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取条数流水失败",
  );
}

export async function createRechargeOrder(
  amountFen: number,
): Promise<CreatedRechargeOrder> {
  return requestApiJson<CreatedRechargeOrder>(
    "/api/recharge-orders",
    "创建充值订单失败",
    { method: "POST", body: JSON.stringify({ amount_fen: amountFen }) },
  );
}

export async function listRechargeOrders({
  limit = 20,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<RechargeOrderPage> {
  return requestApiJson<RechargeOrderPage>(
    `/api/recharge-orders?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取充值订单失败",
  );
}

export async function getRechargeOrder(
  orderNo: string,
): Promise<RechargeOrder> {
  return requestApiJson<RechargeOrder>(
    `/api/recharge-orders/${encodeURIComponent(orderNo)}`,
    "读取充值状态失败",
  );
}

export async function getControlAccounts({
  limit = 50,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<ControlAccountPage> {
  return requestControlJson<ControlAccountPage>(
    `/api/control/accounts?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取内部账号失败",
  );
}

export async function getControlRechargeOrders({
  status,
  limit = 50,
  offset = 0,
}: {
  status?: RechargeOrderStatus;
  limit?: number;
  offset?: number;
} = {}): Promise<ControlRechargeOrderPage> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (status) {
    query.set("status", status);
  }
  return requestControlJson<ControlRechargeOrderPage>(
    `/api/control/recharge-orders?${query}`,
    "读取充值订单失败",
  );
}

export async function getControlWalletTransactions({
  limit = 50,
  offset = 0,
}: {
  limit?: number;
  offset?: number;
} = {}): Promise<ControlWalletTransactionPage> {
  return requestControlJson<ControlWalletTransactionPage>(
    `/api/control/wallet-transactions?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    "读取账务流水失败",
  );
}

export async function getControlReconciliation(): Promise<ControlReconciliation> {
  return requestControlJson<ControlReconciliation>(
    "/api/control/billing-reconciliation",
    "读取对账结果失败",
  );
}

export async function getControlSettings(): Promise<ControlSettings> {
  return requestControlJson<ControlSettings>(
    "/api/control/settings",
    "读取管理设置失败",
  );
}

function newControlWriteIdempotencyKey(): string {
  const cryptoRef = globalThis.crypto;
  if (cryptoRef && typeof cryptoRef.randomUUID === "function") {
    return cryptoRef.randomUUID();
  }
  return `control-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

function controlWriteInit(
  method: "PATCH" | "PUT" | "POST",
  body: Record<string, unknown>,
  reason: string,
): RequestInit {
  return {
    method,
    headers: {
      "Idempotency-Key": newControlWriteIdempotencyKey(),
    },
    body: JSON.stringify({
      ...body,
      confirm: true,
      reason,
    }),
  };
}

export async function updateControlZPaySettings(
  input: {
    pid: string;
    key: string;
    enabled_channels: Array<"alipay" | "wxpay">;
  },
  // 资金链路最敏感的写操作：确认对话框收集的操作原因必须透传到审计。
  // 缺省值仅保留给尚未接入确认流的旧调用点（workspace SettingsPanel）。
  reason = "更新 ZPay 支付配置",
): Promise<ControlSettings["zpay"]> {
  return requestControlJson<ControlSettings["zpay"]>(
    "/api/control/settings/zpay",
    "保存 ZPay 设置失败",
    controlWriteInit("PATCH", input, reason),
  );
}

export async function updateControlBillingSettings(
  input: {
    internal_base_unit_price_fen: number;
    oral_unit_price_fen: number;
    min_recharge_fen: number;
    recharge_step_fen: number;
  },
  reason = "更新后台计费配置",
): Promise<BillingSettings> {
  return requestControlJson<BillingSettings>(
    "/api/control/settings/billing",
    "保存内部价格失败",
    controlWriteInit("PATCH", input, reason),
  );
}

export async function updateControlProviderSettings(
  provider: ProviderName,
  config: Record<string, string>,
): Promise<ProviderSettings> {
  return requestControlJson<ProviderSettings>(
    `/api/control/settings/providers/${provider}`,
    "保存服务设置失败",
    controlWriteInit("PUT", { config }, `更新 ${provider} 服务配置`),
  );
}

export async function testControlProviderConnection(
  provider: ProviderName,
): Promise<ProviderTestResult> {
  return requestControlJson<ProviderTestResult>(
    `/api/control/settings/providers/${provider}/connection-test`,
    "连接测试失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function updateControlRuntimeSettings(
  runtime: RuntimeSettings,
): Promise<RuntimeSettings> {
  return requestControlJson<RuntimeSettings>(
    "/api/control/settings/runtime",
    "保存运行设置失败",
    controlWriteInit("PATCH", runtime, "更新后台运行参数"),
  );
}

export async function syncControlRechargeOrder(
  orderNo: string,
  reason: string,
): Promise<RechargeOrder> {
  // A4（2026-09-02 评估）：手动查单现在执行管理端写契约
  // （confirm + reason + 幂等键），服务端会落一条 payment.sync 审计行。
  return requestControlJson<RechargeOrder>(
    `/api/control/recharge-orders/${encodeURIComponent(orderNo)}/sync`,
    "同步充值订单失败",
    controlWriteInit("POST", {}, reason),
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function downloadCustomersCsv(
  options: {
    status?: string;
    username?: string;
    createdFrom?: string;
    createdTo?: string;
    balanceMin?: number;
    balanceMax?: number;
  } = {},
): Promise<void> {
  const query = new URLSearchParams();
  if (options.status) query.set("status", options.status);
  if (options.username) query.set("username", options.username);
  if (options.createdFrom) query.set("created_from", options.createdFrom);
  if (options.createdTo) query.set("created_to", options.createdTo);
  if (options.balanceMin !== undefined)
    query.set("balance_min", String(options.balanceMin));
  if (options.balanceMax !== undefined)
    query.set("balance_max", String(options.balanceMax));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  await downloadControlCsv(
    `/api/control/customers.csv${suffix}`,
    "customers.csv",
  );
}

export type LedgerExportSummary = {
  total: number;
  returned: number;
  truncated: boolean;
} | null;
export type LedgerExportFilters = {
  username?: string;
  createdFrom?: string;
  createdTo?: string;
  userId?: string;
};

function ledgerExportQuery(options: LedgerExportFilters): URLSearchParams {
  const query = new URLSearchParams();
  if (options.username) query.set("username", options.username);
  if (options.userId) query.set("user_id", options.userId);
  if (options.createdFrom) query.set("created_from", options.createdFrom);
  if (options.createdTo) query.set("created_to", options.createdTo);
  return query;
}

export async function downloadControlRechargeOrdersCsv(
  options: LedgerExportFilters & {
    status?: RechargeOrderStatus;
    channel?: string;
  } = {},
): Promise<LedgerExportSummary> {
  const query = ledgerExportQuery(options);
  if (options.status) query.set("status", options.status);
  if (options.channel) query.set("channel", options.channel);
  return downloadControlCsv(
    `/api/control/recharge-orders.csv${query.size ? `?${query}` : ""}`,
    "recharge-orders.csv",
  );
}

export async function downloadControlWalletTransactionsCsv(
  options: LedgerExportFilters & { type?: string } = {},
): Promise<LedgerExportSummary> {
  const query = ledgerExportQuery(options);
  if (options.type) query.set("type", options.type);
  return downloadControlCsv(
    `/api/control/wallet-transactions.csv${query.size ? `?${query}` : ""}`,
    "wallet-transactions.csv",
  );
}

export async function createScriptVersion(
  projectId: string,
  input: ScriptVersionInput,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/scripts`,
    "保存口播稿失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function getLatestScriptVersion(
  projectId: string,
): Promise<GenerationVersionState> {
  return requestGenerationJson<GenerationVersionState>(
    `/api/projects/${encodeURIComponent(projectId)}/scripts/latest`,
    "读取口播稿失败",
  );
}

export async function compileGenerationPrompt(
  projectId: string,
  input: PromptCompileInput,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/compile`,
    "编译视频生成提示词失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function reviseGenerationPrompt(
  projectId: string,
  input: PromptRevisionInput,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/revise`,
    "保存视频生成提示词失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function saveGenerationPrompt(
  projectId: string,
  input: SavedPromptInput,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/saved-prompts`,
    "另存提示词失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function listSavedGenerationPrompts(
  projectId: string,
): Promise<GenerationVersion[]> {
  return requestGenerationJson<GenerationVersion[]>(
    `/api/projects/${encodeURIComponent(projectId)}/saved-prompts`,
    "读取我的提示词失败",
  );
}

export async function applySavedGenerationPrompt(
  projectId: string,
  savedPromptId: string,
  basePromptVersionId: string,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/saved-prompts/${encodeURIComponent(savedPromptId)}/apply`,
    "应用我的提示词失败",
    {
      method: "POST",
      body: JSON.stringify({ base_prompt_version_id: basePromptVersionId }),
    },
  );
}

export type PromptPreviewInput = {
  output_duration_seconds?: number;
  resolution?: "768P" | "2K";
  ratio?: GenerationRatio;
};

export type PromptPreviewResult = {
  prompt_text: string;
  output_duration_seconds: number;
  resolution: "768P" | "2K";
  ratio: GenerationRatio;
  script_source: "script_version" | "analysis_original";
  shot_card_version_id: string | null;
};

export async function previewGenerationPrompt(
  projectId: string,
  input: PromptPreviewInput = {},
): Promise<PromptPreviewResult> {
  try {
    return await requestGenerationJson<PromptPreviewResult>(
      `/api/projects/${encodeURIComponent(projectId)}/prompts/preview`,
      "生成提示词预览失败",
      { method: "POST", body: JSON.stringify(input) },
    );
  } catch (error) {
    throw previewRequestError(error);
  }
}

// 生成系的通用 409 文案（"上游内容已变化"）描述的是版本级联过期；预览端点的
// 两种 409 都指向拆解数据缺失，换成可行动的原因，避免误导用户。
function previewRequestError(error: unknown): Error {
  const { status, code } = error as RequestError;
  if (
    status === 409 &&
    (code === "ANALYSIS_NOT_READY" || code === "SHOT_CARD_TIMELINE_INVALID")
  ) {
    const mapped = new Error(
      "拆解结果缺少镜头数据，请重新上传视频拆解",
    ) as RequestError;
    mapped.status = status;
    mapped.code = code;
    return mapped;
  }
  return error instanceof Error ? error : new Error("生成提示词预览失败");
}

export async function getLatestGenerationPrompt(
  projectId: string,
): Promise<GenerationVersionState> {
  return requestGenerationJson<GenerationVersionState>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/latest`,
    "读取视频生成提示词失败",
  );
}

export async function lockGenerationPrompt(
  projectId: string,
  promptVersionId: string,
): Promise<GenerationVersion> {
  return requestGenerationJson<GenerationVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/prompts/${encodeURIComponent(promptVersionId)}/lock`,
    "锁定视频生成提示词失败",
    { method: "POST" },
  );
}

export async function getGenerationRuntimeLimits(): Promise<GenerationRuntimeLimits> {
  return requestGenerationJson<GenerationRuntimeLimits>(
    "/api/generation/runtime-limits",
    "读取生成数量上限失败",
  );
}

export async function getGenerationPriceQuote(input: {
  resolution: "768P" | "2K";
  duration_seconds: number;
  quantity: number;
}): Promise<GenerationPriceQuote> {
  const query = new URLSearchParams({
    resolution: input.resolution,
    duration_seconds: String(input.duration_seconds),
    quantity: String(input.quantity),
  });
  return requestGenerationJson<GenerationPriceQuote>(
    `/api/generation/price-quote?${query.toString()}`,
    "读取生成费用失败",
  );
}

// 批次 Provider 由 VITE_GENERATION_PROVIDER 显式驱动：未设置时默认走真实
// Metaso H3，避免开发环境静默创建模拟任务、让用户误以为真实生成已触发。
// 需要本地模拟管线（不产生付费调用）时，显式设置
// VITE_GENERATION_PROVIDER=fake_h3。
export function defaultBatchProvider(): GenerationBatchInput["provider"] {
  return import.meta.env.VITE_GENERATION_PROVIDER === "fake_h3"
    ? "fake_h3"
    : "metaso";
}

// ---------------------------------------------------------------------------
// C2 独立创作（视频生成页）
// ---------------------------------------------------------------------------

export type IndependentCapabilities = {
  extended_modes_enabled: boolean;
  t2v_enabled: boolean;
  i2v_enabled: boolean;
  r2v_enabled: boolean;
  last_frame_enabled: boolean;
  max_reference_images: number;
  max_reference_videos: number;
  max_reference_audios: number;
  max_quantity: number;
};

export async function getIndependentCapabilities(): Promise<IndependentCapabilities> {
  return requestApiJson<IndependentCapabilities>(
    "/api/independent/capabilities",
    "读取视频生成能力失败",
  );
}

export type IndependentVideoTaskInput = {
  display_name: string;
  mode: "t2v" | "i2v" | "l2v" | "r2v";
  prompt_text: string;
  first_frame_asset_id?: string | null;
  last_frame_asset_id?: string | null;
  reference_asset_ids?: string[];
  output_duration_seconds: number;
  resolution: "768P" | "2K";
  ratio: GenerationRatio;
  quantity: number;
  idempotency_key: string;
  provider?: "fake_h3" | "metaso";
};

/** 幂等创建独立创作批次：成功后任务中心/进度轮询走既有批次通道。 */
export async function createIndependentVideoTask(
  input: IndependentVideoTaskInput,
): Promise<GenerationBatch> {
  return requestGenerationJson<GenerationBatch>(
    "/api/independent/video-tasks",
    "视频生成任务提交失败",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    },
  );
}

export type SavedPromptItem = {
  generation_context?: PromptGenerationContext & {
    mode?: H3Mode;
    generation_assets?: { label: string; purpose: string }[];
  };
  id: string;
  project_id: string;
  name: string;
  prompt_text: string;
  created_at: string;
};

/** 跨项目「我的提示词」：独立创作页导入提示词的数据源。 */
export async function listUserSavedPrompts(): Promise<SavedPromptItem[]> {
  const page = await requestApiJson<{ items: SavedPromptItem[] }>(
    "/api/studio/saved-prompts",
    "读取我的提示词失败",
  );
  return page.items;
}

export async function createGenerationBatch(
  projectId: string,
  input: GenerationBatchInput,
): Promise<GenerationBatch> {
  return requestGenerationJson<GenerationBatch>(
    `/api/projects/${encodeURIComponent(projectId)}/generation-batches`,
    "创建视频生成批次失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function getGenerationBatch(
  batchId: string,
): Promise<GenerationBatch> {
  return requestApiJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}`,
    "任务批次暂不可用",
  );
}

export async function regenerateGenerationBatch(
  batchId: string,
  input: PaidRegenerationInput,
): Promise<GenerationBatch> {
  return requestGenerationJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}/regenerate`,
    "整批重新生成失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function renameGenerationBatch(
  batchId: string,
  displayName: string,
): Promise<GenerationBatch> {
  return requestApiJson<GenerationBatch>(
    `/api/generation-batches/${encodeURIComponent(batchId)}/name`,
    "修改批次名称失败",
    { method: "PATCH", body: JSON.stringify({ display_name: displayName }) },
  );
}

export async function deleteGenerationBatch(batchId: string): Promise<void> {
  const response = await requestApi(
    `/api/generation-batches/${encodeURIComponent(batchId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "删除批次失败"));
  }
}

export async function listGenerationBatches(
  filters: GenerationBatchListFilters = {},
): Promise<GenerationBatchListPage> {
  const query = new URLSearchParams();
  if (filters.projectId) {
    query.set("project_id", filters.projectId);
  }
  if (filters.createdByUserId) {
    query.set("created_by_user_id", filters.createdByUserId);
  }
  if (filters.status) {
    query.set("status", filters.status);
  }
  if (filters.needsAttention !== undefined) {
    query.set("needs_attention", String(filters.needsAttention));
  }
  if (filters.limit !== undefined) {
    query.set("limit", String(filters.limit));
  }
  if (filters.cursor) {
    query.set("cursor", filters.cursor);
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return requestApiJson<GenerationBatchListPage>(
    `/api/generation-batches${suffix}`,
    "任务记录列表暂不可用",
  );
}

export async function archiveGenerationTask(
  taskId: string,
): Promise<GenerationTask> {
  return requestGenerationJson<GenerationTask>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/archive`,
    "保存结果暂未确认，请刷新任务核对；不会重新生成或扣费",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function retryGenerationTask(
  taskId: string,
  input: GenerationTaskRetryInput,
): Promise<GenerationTask> {
  return requestGenerationJson<GenerationTask>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/retry`,
    "重试生成任务失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function regenerateGenerationTask(
  taskId: string,
  input: PaidRegenerationInput,
): Promise<GenerationBatch> {
  return requestGenerationJson<GenerationBatch>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/regenerate`,
    "重新生成视频任务失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function confirmGenerationTaskNotCharged(
  taskId: string,
  input: ConfirmNotChargedInput,
): Promise<GenerationTask> {
  return requestGenerationJson<GenerationTask>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/confirm-not-charged`,
    "确认任务未计费失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function getGenerationResultDownloadUrl(
  assetId: string,
): Promise<DownloadUrl> {
  const result = await requestGenerationJson<DownloadUrl>(
    `/api/assets/${encodeURIComponent(assetId)}/download-url`,
    "获取生成结果下载地址失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
  // 与 getAssetDownloadUrl 同口径：服务端签发的是站内相对路径，桌面端页面
  // origin 不是 API origin，不绝对化则播放器与下载都拿不到字节。地址缺失是
  // 契约异常，原样返回交由调用方抛出可读错误。
  if (!result.url) return result;
  return { ...result, url: resolveManagedMediaUrl(result.url) };
}

// 在线播放：直接复用后端签发的预签名 URL 作为 video src（COS 与本地
// 存储均支持 Range 渐进播放，无需整包下载 blob，首帧秒出）。url 缺失
// 视为契约异常直接抛错，由调用方落入失败分支，避免上层自动签发循环。
export async function createGenerationResultPreviewUrl(
  assetId: string,
): Promise<string> {
  const { url } = await getGenerationResultDownloadUrl(assetId);
  if (!url) {
    throw new Error("预览链接获取失败，请重试。");
  }
  return url;
}

// 直链交付不复制成片到云存储。URL 仅通过任务所属权限接口按需签发，
// 不进入批次详情，避免被列表、日志或跨用户缓存意外暴露。
export async function createGenerationTaskPreviewUrl(
  taskId: string,
): Promise<string> {
  const { url } = await requestGenerationJson<
    components["schemas"]["GenerationTaskPreviewUrlResponse"]
  >(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/preview-url`,
    "获取生成结果播放地址失败",
  );
  if (!url) {
    throw new Error("预览链接获取失败，请重试。");
  }
  return url;
}

export async function downloadGenerationResult(
  assetId: string,
  filename: string,
): Promise<VideoDownloadResult> {
  if (!isTauri()) {
    await downloadMaterialAsset(assetId, filename);
    return { status: "started" };
  }
  return downloadVideoResult(
    async () => (await getGenerationResultDownloadUrl(assetId)).url,
    filename,
  );
}

export async function downloadGenerationTaskResult(
  taskId: string,
  filename: string,
): Promise<VideoDownloadResult> {
  if (!isTauri()) {
    // A browser-managed HTTP attachment survives navigation and does not rely
    // on a short-lived blob URL. Archive only the existing result, never submit
    // generation again; the archive endpoint reuses an already saved asset.
    const task = await archiveGenerationTask(taskId);
    if (!task.result_asset_id) {
      throw new Error("成片尚未保存完成，请重试；不会重新生成或扣费。");
    }
    return downloadGenerationResult(task.result_asset_id, filename);
  }
  return downloadVideoResult(
    () => createGenerationTaskPreviewUrl(taskId),
    filename,
  );
}

export type VideoDownloadResult =
  | { status: "saved"; downloadId: string; path: string }
  | { status: "cancelled" }
  | { status: "started" };

export class VideoDownloadUnconfirmedError extends Error {
  constructor() {
    super("尚未确认保存结果，请先检查所选文件夹，避免重复下载。");
    this.name = "VideoDownloadUnconfirmedError";
  }
}

type NativeVideoDownload = { download_id: string; path: string };
type NativeVideoDownloadFinished = {
  download_id: string;
  success: boolean;
  path: string | null;
  error: string | null;
};

export async function openVideoDownloadFolder(
  downloadId: string,
): Promise<void> {
  try {
    await invoke("open_video_download_folder", { downloadId });
  } catch (error) {
    throw new Error("无法打开文件夹，请按显示的保存路径查找视频。", {
      cause: error,
    });
  }
}

async function downloadVideoResult(
  getUrl: () => Promise<string>,
  filename: string,
): Promise<VideoDownloadResult> {
  let destination: NativeVideoDownload | null;
  try {
    destination = await invoke<NativeVideoDownload | null>(
      "choose_video_download",
      { filename },
    );
  } catch (error) {
    throw new Error("无法选择保存位置，请检查桌面权限后重试。", {
      cause: error,
    });
  }
  if (!destination) return { status: "cancelled" };

  let blobUrl: string | undefined;
  let unlisten: (() => void) | undefined;
  let timeout: number | undefined;
  let statusTimer: number | undefined;
  let finished = false;
  let started = false;
  try {
    const blob = await fetchGenerationResultBlob(
      await getUrl(),
      "下载生成结果失败",
    );
    blobUrl = URL.createObjectURL(blob);
    let resolveCompletion: (value: NativeVideoDownloadFinished) => void =
      () => {};
    const completion = new Promise<NativeVideoDownloadFinished>((resolve) => {
      resolveCompletion = resolve;
    });
    const finish = (value: NativeVideoDownloadFinished) => {
      if (finished || value.download_id !== destination.download_id) return;
      finished = true;
      resolveCompletion(value);
    };
    const unconfirmed = () =>
      finish({
        download_id: destination.download_id,
        success: false,
        path: null,
        error: "COMPLETION_UNCONFIRMED",
      });
    unlisten = await listen<NativeVideoDownloadFinished>(
      "video-download-finished",
      ({ payload }) => {
        if (payload.download_id === destination.download_id) finish(payload);
      },
    );
    timeout = window.setTimeout(unconfirmed, 300_000);
    await invoke("start_video_download", {
      downloadId: destination.download_id,
      url: blobUrl,
    });
    const anchor = document.createElement("a");
    anchor.href = blobUrl;
    anchor.download = filename;
    document.body.append(anchor);
    try {
      anchor.click();
      started = true;
    } finally {
      anchor.remove();
    }
    // 完成事件可能丢失；只查询本机已记录的结果，不重试下载或猜测成功。
    const checkStatus = async () => {
      try {
        const result = await invoke<NativeVideoDownloadFinished | null>(
          "get_video_download_status",
          { downloadId: destination.download_id },
        );
        if (result) finish(result);
      } catch {
        unconfirmed();
      }
      if (!finished) statusTimer = window.setTimeout(checkStatus, 2_000);
    };
    statusTimer = window.setTimeout(checkStatus, 2_000);
    const result = await completion;
    if (result.error === "COMPLETION_UNCONFIRMED") {
      throw new VideoDownloadUnconfirmedError();
    }
    if (!result.success || !result.path) {
      throw new Error(
        "保存视频失败或下载被中断，请检查磁盘空间和文件夹权限后重试。",
      );
    }
    return {
      status: "saved",
      downloadId: destination.download_id,
      path: result.path,
    };
  } catch (error) {
    if (!started) {
      try {
        await invoke("cancel_video_download", {
          downloadId: destination.download_id,
        });
      } catch (cleanupError) {
        throw new Error(
          "下载未完成且未能清理下载请求，请重新打开客户端后重试。",
          {
            cause: new AggregateError([error, cleanupError]),
          },
        );
      }
    }
    throw error;
  } finally {
    finished = true;
    window.clearTimeout(timeout);
    window.clearTimeout(statusTimer);
    unlisten?.();
    if (blobUrl) URL.revokeObjectURL(blobUrl);
  }
}

async function fetchGenerationResultBlob(
  url: string,
  errorPrefix: string,
): Promise<Blob> {
  const inlinePrefix = "data:video/mp4;base64,";
  if (url.startsWith(inlinePrefix)) {
    // 测试成片已随授权接口返回，直接解码避免触发桌面端 connect-src 限制。
    try {
      const content = atob(url.slice(inlinePrefix.length));
      const bytes = Uint8Array.from(content, (character) =>
        character.charCodeAt(0),
      );
      return await validateGenerationDownloadBlob(
        new Blob([bytes], { type: "video/mp4" }),
        errorPrefix,
      );
    } catch (error) {
      throw new Error(`${errorPrefix}：内联视频数据无效。`, { cause: error });
    }
  }
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    CLOUD_OP_TIMEOUT_MS,
  );

  try {
    // 任务权限由应用接口校验；下载文件时不向供应商传递会话凭据。
    const response = await fetch(url, {
      signal: controller.signal,
      credentials: "omit",
    });
    if (!response.ok) {
      throw new Error(`${errorPrefix}（${response.status}）`);
    }
    return await validateGenerationDownloadBlob(
      await response.blob(),
      errorPrefix,
    );
  } finally {
    window.clearTimeout(timeout);
  }
}

async function validateGenerationDownloadBlob(
  blob: Blob,
  errorPrefix: string,
): Promise<Blob> {
  const invalid = () =>
    new Error(`${errorPrefix}：返回的文件不是有效的 MP4，请刷新结果后重试。`);
  const mime = blob.type.split(";", 1)[0].trim().toLowerCase();
  if (
    mime &&
    !["video/mp4", "application/mp4", "application/octet-stream"].includes(mime)
  ) {
    throw invalid();
  }
  // 仅检查有界容器头以拦截错误页/伪装响应，不代表媒体可完整解码或画幅合格。
  const header = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      if (reader.result instanceof ArrayBuffer) resolve(reader.result);
      else reject(invalid());
    };
    reader.onerror = () => reject(invalid());
    reader.onabort = () => reject(invalid());
    reader.readAsArrayBuffer(blob.slice(0, 4096));
  });
  if (header.byteLength < 24) throw invalid();
  const bytes = new Uint8Array(header);
  const tag = (offset: number) =>
    String.fromCharCode(...bytes.subarray(offset, offset + 4));
  const boxSize = new DataView(header).getUint32(0);
  if (
    tag(4) !== "ftyp" ||
    boxSize < 16 ||
    boxSize > header.byteLength ||
    (boxSize - 16) % 4 !== 0 ||
    blob.size <= boxSize + 8
  )
    throw invalid();
  const mp4Brand = (brand: string) =>
    /^(iso[2-9m]|mp4[12]|avc1|dash|M4V |MSNV|cmf[cs])$/.test(brand);
  let supported = mp4Brand(tag(8));
  for (let offset = 16; offset < boxSize && !supported; offset += 4) {
    supported = mp4Brand(tag(offset));
  }
  if (!supported) throw invalid();
  return blob.slice(0, blob.size, "video/mp4");
}

export async function reconcileUncertainTask(
  taskId: string,
  input: ReconcileGenerationTaskInput,
): Promise<GenerationReconcileOperation> {
  return requestApiJson<GenerationReconcileOperation>(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/reconcile`,
    "任务对账失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

/** 取消仍在排队的生成批次；取消与计费终态均以服务端为准。 */
export async function cancelGenerationBatch(batchId: string): Promise<void> {
  await requestApiJson<unknown>(
    `/api/generation-batches/${encodeURIComponent(batchId)}/cancel`,
    "取消任务失败",
    { method: "POST" },
  );
}

const generationReconcileWaiters = new Map<
  string,
  Promise<GenerationReconcileOperation>
>();

export async function getGenerationReconcileOperation(
  operationId: string,
): Promise<GenerationReconcileOperation> {
  return requestApiJson<GenerationReconcileOperation>(
    `/api/generation-reconcile-operations/${encodeURIComponent(operationId)}`,
    "读取任务对账进度失败",
  );
}

export async function getLatestGenerationReconcileOperation(
  taskId: string,
): Promise<GenerationReconcileOperation | null> {
  const response = await requestApi(
    `/api/generation-tasks/${encodeURIComponent(taskId)}/reconcile/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取任务对账进度失败（${response.status}）`);
  }
  return (await response.json()) as GenerationReconcileOperation | null;
}

export async function waitForGenerationReconcileOperation(
  operationId: string,
): Promise<GenerationReconcileOperation> {
  const existing = generationReconcileWaiters.get(operationId);
  if (existing) {
    return existing;
  }
  const waiter = pollGenerationReconcileOperation(operationId);
  generationReconcileWaiters.set(operationId, waiter);
  const clear = () => {
    if (generationReconcileWaiters.get(operationId) === waiter) {
      generationReconcileWaiters.delete(operationId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollGenerationReconcileOperation(
  operationId: string,
): Promise<GenerationReconcileOperation> {
  const deadline = Date.now() + 10 * 60_000;
  const nextDelay = createPollBackoff();
  while (Date.now() < deadline) {
    const operation = await getGenerationReconcileOperation(operationId);
    if (operation.status === "SUCCEEDED") {
      return operation;
    }
    if (operation.status === "FAILED") {
      throw new Error(operation.error_message || "任务对账失败，请重新提交。");
    }
    await waitForPoll(nextDelay());
  }
  throw new Error("任务仍在后台对账，请稍后返回查看。");
}

export async function listProjects(): Promise<Project[]> {
  return requestApiJson<Project[]>("/api/projects", "项目列表暂不可用");
}

export async function createProject(name: string): Promise<Project> {
  return requestApiJson<Project>("/api/projects", "创建项目失败", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}

export async function deleteProject(projectId: string): Promise<void> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "删除项目失败"));
  }
}

export async function renameProject(
  projectId: string,
  name: string,
): Promise<Project> {
  return requestApiJson<Project>(
    `/api/projects/${encodeURIComponent(projectId)}/name`,
    "修改项目名称失败",
    { method: "PATCH", body: JSON.stringify({ name }) },
  );
}

export async function createVideoUploadIntent(
  projectId: string,
  file: File,
  purpose: "replica" | "script" = "replica",
): Promise<UploadIntent> {
  const sha256 = await sha256ForUpload(file);
  return requestApiJson<UploadIntent>(
    "/api/assets/upload-intent",
    "创建上传任务失败",
    {
      method: "POST",
      body: JSON.stringify({
        project_id: projectId,
        purpose,
        filename: file.name,
        // Derive from the extension so a generic/empty file.type (e.g.
        // application/octet-stream from some file managers) is normalized.
        content_type: contentTypeForFile(file),
        size_bytes: file.size,
        ...(sha256 === null ? {} : { sha256 }),
      }),
    },
  );
}

export function uploadReferenceVideo(
  intent: UploadIntent,
  file: File,
  onProgress: (progressPercent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  if (
    intent.upload_required === false ||
    intent.method !== "PUT" ||
    !intent.url
  ) {
    return Promise.reject(new Error("该视频已存在，无需重复上传。"));
  }
  return uploadStorageObject(
    { headers: intent.headers, method: intent.method, url: intent.url },
    file,
    onProgress,
    "上传参考视频",
    signal,
  );
}

export function uploadIdentityAsset(
  intent: IdentityUploadIntent,
  file: File,
  onProgress: (progressPercent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return uploadStorageObject(intent, file, onProgress, "上传人物资料", signal);
}

export async function listMaterials(
  filters: {
    mediaType?: MaterialItem["media_type"];
    source?: MaterialItem["source"];
    query?: string;
    // MATERIAL-UX-01：分组导航精确筛选。undefined 忽略；空串为「未分组」
    // 语义（当前数据模型下为空集）；非空为分组名精确匹配。
    group?: string;
    page?: number;
    pageSize?: number;
  } = {},
): Promise<MaterialPage> {
  const query = new URLSearchParams();
  if (filters.mediaType) query.set("media_type", filters.mediaType);
  if (filters.source) query.set("source", filters.source);
  if (filters.query?.trim()) query.set("q", filters.query.trim());
  if (filters.group !== undefined) query.set("group", filters.group);
  if (filters.page !== undefined) query.set("page", String(filters.page));
  if (filters.pageSize !== undefined) {
    query.set("page_size", String(filters.pageSize));
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return requestApiJson<MaterialPage>(
    `/api/studio/materials${suffix}`,
    "读取素材库失败",
  );
}

export async function resolveMaterials(
  materialIds: string[],
): Promise<MaterialResolveResponse> {
  return requestApiJson<MaterialResolveResponse>(
    "/api/studio/materials/resolve",
    "恢复素材引用失败",
    { method: "POST", body: JSON.stringify({ material_ids: materialIds }) },
  );
}

export async function createMaterialUploadIntent(
  file: File,
  input: {
    title?: string;
    group?: string;
    audioPurpose?: "oral_audio" | "voice_clone" | "reference";
    durationSeconds?: number;
  } = {},
): Promise<MaterialUploadIntent> {
  const sha256 = await sha256ForUpload(file);
  return requestApiJson<MaterialUploadIntent>(
    "/api/studio/materials/upload-intent",
    "创建素材上传任务失败",
    {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        content_type: materialContentTypeForFile(file),
        size_bytes: file.size,
        ...(sha256 === null ? {} : { sha256 }),
        ...(input.title ? { title: input.title } : {}),
        ...(input.group ? { group: input.group } : {}),
        ...(input.audioPurpose ? { audio_purpose: input.audioPurpose } : {}),
        ...(input.durationSeconds !== undefined
          ? { duration_seconds: input.durationSeconds }
          : {}),
      }),
    },
  );
}

export function uploadMaterial(
  intent: MaterialUploadIntent,
  file: File,
  onProgress: (progressPercent: number) => void,
  signal?: AbortSignal,
): Promise<void> {
  return uploadStorageObject(intent, file, onProgress, "上传素材", signal);
}

export async function completeMaterialUpload(
  assetId: string,
  signal?: AbortSignal,
): Promise<MaterialItem> {
  return requestApiJson<MaterialItem>(
    `/api/studio/materials/uploads/${encodeURIComponent(assetId)}/complete`,
    "完成素材上传失败",
    { method: "POST", signal },
    CLOUD_OP_TIMEOUT_MS,
  );
}

/**
 * 完成一次素材上传，并在服务端已持有相同字节时直接复用。
 *
 * 上传意图会先按内容哈希查登记表：命中时资产已按「已完成」状态建好，
 * `upload_required` 为 false，此时既不能传输（URL 为空）也不能再调
 * /complete（会重复计一次引用），只把这条素材解析回来即可。
 */
export async function putMaterial(
  intent: MaterialUploadIntent,
  file: File,
  onProgress: (progressPercent: number) => void,
  signal?: AbortSignal,
): Promise<MaterialItem> {
  signal?.throwIfAborted();
  if (intent.upload_required === false) {
    // Resolve the completed asset without repeating transfer or reference counting.
    const resolved = await resolveMaterials([intent.material_id]);
    signal?.throwIfAborted();
    const item = resolved.items[0];
    if (!item) {
      throw new Error("复用素材后未能读取素材详情");
    }
    return item;
  }
  await uploadMaterial(intent, file, onProgress, signal);
  return completeMaterialUpload(intent.asset_id, signal);
}

export async function updateMaterial(
  materialId: string,
  update: MaterialUpdate,
): Promise<MaterialItem> {
  return requestApiJson<MaterialItem>(
    `/api/studio/materials/${encodeURIComponent(materialId)}`,
    "更新素材失败",
    { method: "PATCH", body: JSON.stringify(update) },
  );
}

// MATERIAL-UX-01：分组导航计数（对当前用户可见、未隐藏素材按有效分组聚合）。
export async function listMaterialGroups(): Promise<MaterialGroupsResponse> {
  return requestApiJson<MaterialGroupsResponse>(
    "/api/studio/materials/groups",
    "读取素材分组失败",
  );
}

// MATERIAL-UX-01：批量分组/隐藏。越权或不支持的素材由服务端逐条跳过。
export async function bulkUpdateMaterials(
  request: MaterialBulkRequest,
): Promise<MaterialBulkResult> {
  return requestApiJson<MaterialBulkResult>(
    "/api/studio/materials/bulk",
    "批量更新素材失败",
    { method: "PATCH", body: JSON.stringify(request) },
  );
}

export async function hideMaterial(materialId: string): Promise<void> {
  const response = await requestApi(
    `/api/studio/materials/${encodeURIComponent(materialId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(await responseErrorMessage(response, "移除素材失败"));
  }
}

export async function downloadMaterialAsset(
  assetId: string,
  filename: string,
): Promise<void> {
  const { url } = await getAssetDownloadUrl(assetId);
  if (!url) {
    throw new Error("素材下载链接获取失败，请重试。");
  }
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
}

function uploadStorageObject(
  intent: {
    headers: Record<string, string>;
    method: string;
    url: string;
  },
  file: File,
  onProgress: (progressPercent: number) => void,
  errorPrefix: string,
  signal?: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    // API-managed uploads follow the same configured base (including a proxy
    // prefix) as JSON requests. Absolute provider URLs retain their signature.
    const uploadUrl = resolveManagedMediaUrl(intent.url);
    request.open(intent.method, uploadUrl);
    // Scale the timeout with the payload (~200KB/s) so large 50MB uploads are
    // not cut off on slow links, while small files keep a tight bound.
    request.timeout = Math.max(60_000, Math.ceil(file.size / 200));
    // CW-015: only an upload aimed at the API origin carries the workspace
    // Bearer token; a direct-to-COS/Provider presigned upload must never
    // receive it. The development-identity header is removed entirely, so a
    // formal customer upload can never carry a synthetic dev identity.
    if (isApiUploadUrl(uploadUrl)) {
      const accessToken = workspaceAccessToken();
      if (accessToken) {
        request.setRequestHeader("Authorization", `Bearer ${accessToken}`);
        if (accessToken.startsWith("web-session:"))
          request.setRequestHeader("X-Customer-Web", "1");
      }
    }
    for (const [name, value] of Object.entries(intent.headers)) {
      request.setRequestHeader(name, value);
    }
    request.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) {
        onProgress(100);
        resolve();
        return;
      }
      if (request.status === 401 && isApiUploadUrl(uploadUrl)) {
        emitSessionExpired();
        reject(new Error("登录已失效，请重新进入工作台。"));
        return;
      }
      reject(new Error(`${errorPrefix}失败（${request.status}）`));
    };
    request.onerror = () =>
      reject(
        new Error(
          isApiUploadUrl(uploadUrl)
            ? `${errorPrefix}失败（无法连接服务，请确认服务已启动）`
            : `${errorPrefix}失败（无法连接素材库；请检查网络以及素材库跨域访问规则）`,
        ),
      );
    request.ontimeout = () =>
      reject(new Error(`${errorPrefix}失败（请求超时）`));
    request.onabort = () => reject(new Error("上传已取消"));
    if (signal) {
      const onAbort = () => request.abort();
      if (signal.aborted) {
        request.abort();
      } else {
        signal.addEventListener("abort", onAbort, { once: true });
      }
    }
    request.send(file);
  });
}

export async function listPersonIdentities(): Promise<PersonIdentity[]> {
  return requestApiJson<PersonIdentity[]>(
    "/api/person-identities",
    "读取人物身份失败",
  );
}

export async function createPersonIdentity(input: {
  display_name: string;
  authorization_scope: string[];
  authorization_expires_at: string | null;
}): Promise<PersonIdentity> {
  return requestApiJson<PersonIdentity>(
    "/api/person-identities",
    "创建人物身份失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function createIdentityUploadIntent(
  identityId: string,
  purpose: IdentityUploadPurpose,
  file: File,
): Promise<IdentityUploadIntent> {
  return requestApiJson<IdentityUploadIntent>(
    `/api/person-identities/${encodeURIComponent(identityId)}/${purpose}-upload-intent`,
    purpose === "authorization" ? "创建授权上传失败" : "创建源图上传失败",
    {
      method: "POST",
      body: JSON.stringify({
        filename: file.name,
        content_type: contentTypeForIdentityFile(file),
        size_bytes: file.size,
      }),
    },
  );
}

export async function completeIdentityAuthorizationUpload(
  identityId: string,
  assetId: string,
): Promise<PersonIdentity> {
  return requestApiJson<PersonIdentity>(
    `/api/person-identities/${encodeURIComponent(identityId)}/authorization-upload-complete`,
    "确认授权文件失败",
    { method: "POST", body: JSON.stringify({ asset_id: assetId }) },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function completeIdentitySourceUpload(
  identityId: string,
  assetId: string,
): Promise<CompletedIdentitySource> {
  return requestApiJson<CompletedIdentitySource>(
    `/api/person-identities/${encodeURIComponent(identityId)}/source-upload-complete`,
    "检查真人源图失败",
    { method: "POST", body: JSON.stringify({ asset_id: assetId }) },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function listCharacterPersonas(
  identityId: string,
): Promise<CharacterPersona[]> {
  return requestApiJson<CharacterPersona[]>(
    `/api/person-identities/${encodeURIComponent(identityId)}/personas`,
    "读取人物人设失败",
  );
}

export async function createCharacterPersona(
  identityId: string,
  input: CharacterPersonaInput,
): Promise<CharacterPersona> {
  return requestApiJson<CharacterPersona>(
    `/api/person-identities/${encodeURIComponent(identityId)}/personas`,
    "创建人物人设失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function updateCharacterPersona(
  personaId: string,
  input: CharacterPersonaInput,
): Promise<CharacterPersona> {
  return requestApiJson<CharacterPersona>(
    `/api/character-personas/${encodeURIComponent(personaId)}`,
    "更新人物人设失败",
    { method: "PATCH", body: JSON.stringify(input) },
  );
}

export async function listCharacterVersions(
  personaId: string,
): Promise<CharacterVersion[]> {
  return requestApiJson<CharacterVersion[]>(
    `/api/character-personas/${encodeURIComponent(personaId)}/versions`,
    "读取角色版本失败",
  );
}

export async function createCharacterVersion(
  personaId: string,
  input: CharacterVersionInput,
): Promise<CharacterVersion> {
  return requestApiJson<CharacterVersion>(
    `/api/character-personas/${encodeURIComponent(personaId)}/versions`,
    "创建角色版本失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

export async function listCharacterGenerationTasks(
  versionId: string,
): Promise<CharacterGenerationTask[]> {
  return requestApiJson<CharacterGenerationTask[]>(
    `/api/character-versions/${encodeURIComponent(versionId)}/generation-tasks`,
    "读取人物生成任务失败",
  );
}

export async function listCharacterAssets(
  versionId: string,
): Promise<CharacterAsset[]> {
  return requestApiJson<CharacterAsset[]>(
    `/api/character-versions/${encodeURIComponent(versionId)}/assets`,
    "读取人物视角资产失败",
  );
}

export async function generateCharacterAssets(
  versionId: string,
  input: {
    idempotency_key: string;
    candidates_per_view: number;
    view_types?: RequiredCharacterViewType[];
  },
): Promise<CharacterGenerationTask[]> {
  return requestApiJson<CharacterGenerationTask[]>(
    `/api/character-versions/${encodeURIComponent(versionId)}/generate-assets`,
    "启动人物视角生成失败",
    { method: "POST", body: JSON.stringify(input) },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function regenerateCharacterAsset(
  characterAssetId: string,
  idempotencyKey: string,
): Promise<CharacterGenerationTask[]> {
  return requestApiJson<CharacterGenerationTask[]>(
    `/api/character-assets/${encodeURIComponent(characterAssetId)}/regenerate`,
    "重新生成人物视角失败",
    {
      method: "POST",
      body: JSON.stringify({ idempotency_key: idempotencyKey }),
    },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function reviewCharacterAsset(
  characterAssetId: string,
  decision: CharacterReviewDecision,
  comment: string,
): Promise<CharacterAssetReview> {
  return requestApiJson<CharacterAssetReview>(
    `/api/character-assets/${encodeURIComponent(characterAssetId)}/review`,
    decision === "APPROVED" ? "批准人物资产失败" : "驳回人物资产失败",
    {
      method: "POST",
      body: JSON.stringify({
        decision,
        issue_codes: decision === "REJECTED" ? ["MANUAL_REJECT"] : [],
        comment: comment.trim() || null,
      }),
    },
  );
}

export async function publishCharacterVersion(
  versionId: string,
  selectedAssetIds: Record<RequiredCharacterViewType, string>,
): Promise<CharacterVersion> {
  return requestApiJson<CharacterVersion>(
    `/api/character-versions/${encodeURIComponent(versionId)}/publish`,
    "发布角色版本失败",
    {
      method: "POST",
      body: JSON.stringify({ selected_asset_ids: selectedAssetIds }),
    },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function completeVideoUpload(
  assetId: string,
): Promise<CompletedUpload> {
  return requestApiJson<CompletedUpload>(
    `/api/assets/${encodeURIComponent(assetId)}/complete`,
    "参考视频预检失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

// The reference duration is never sent from here: the server already stores the
// ffprobe measurement taken during upload, and echoing it back only risks a
// request-validation rejection when the two contracts drift apart.
export async function startVideoAnalysis(
  projectId: string,
  assetId: string,
  generationContext?: PromptGenerationContext,
  forceReanalysis = false,
): Promise<AnalysisTask> {
  const errorPrefix = "启动视频拆解失败";
  try {
    return await requestApiJson<AnalysisTask>(
      `/api/projects/${encodeURIComponent(projectId)}/analysis-tasks`,
      errorPrefix,
      {
        method: "POST",
        // Clicking “重新拆解” must publish a fresh immutable analysis version;
        // existing versions are loaded separately when the workspace opens.
        body: JSON.stringify({
          asset_id: assetId,
          reuse_existing: !forceReanalysis,
          generation_context: generationContext
            ? {
                route: "replica",
                project_id: projectId,
                source_asset_id: assetId,
              }
            : undefined,
        }),
      },
    );
  } catch (error) {
    throw analysisRequestError(error, errorPrefix);
  }
}

export async function getAnalysisTask(taskId: string): Promise<AnalysisTask> {
  return requestApiJson<AnalysisTask>(
    `/api/analysis-tasks/${encodeURIComponent(taskId)}`,
    "读取视频拆解任务失败",
  );
}

/**
 * 取消排队中 / 运行中的拆解任务并即时释放预留积分（S11 换源场景）。
 * 已是终态的任务原样返回：已交付的结果不会因晚到的取消被作废。
 */
export async function cancelAnalysisTask(
  taskId: string,
): Promise<AnalysisTask> {
  const errorPrefix = "取消视频拆解任务失败";
  try {
    return await requestApiJson<AnalysisTask>(
      `/api/analysis-tasks/${encodeURIComponent(taskId)}/cancel`,
      errorPrefix,
      { method: "POST" },
    );
  } catch (error) {
    throw analysisRequestError(error, errorPrefix);
  }
}

export async function waitForAnalysisTask(
  taskId: string,
  onUpdate?: (task: AnalysisTask) => void,
): Promise<AnalysisTask> {
  const observers = analysisTaskObservers.get(taskId) ?? new Set();
  analysisTaskObservers.set(taskId, observers);
  if (onUpdate) observers.add(onUpdate);
  const existing = analysisTaskWaiters.get(taskId);
  if (existing) {
    try {
      return await existing;
    } finally {
      if (onUpdate) observers.delete(onUpdate);
    }
  }
  const waiter = pollAnalysisTask(taskId);
  analysisTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (analysisTaskWaiters.get(taskId) === waiter) {
      analysisTaskWaiters.delete(taskId);
      analysisTaskObservers.delete(taskId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollAnalysisTask(taskId: string): Promise<AnalysisTask> {
  const deadline = Date.now() + 20 * 60_000;
  const nextDelay = createPollBackoff();
  while (Date.now() < deadline) {
    const task = await getAnalysisTask(taskId);
    for (const observer of analysisTaskObservers.get(taskId) ?? []) {
      try {
        observer(task);
      } catch {
        // A UI status callback must not interrupt polling shared by all callers.
      }
    }
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED") {
      throw new Error(task.error_message || "视频拆解失败，请重新提交。");
    }
    await waitForPoll(nextDelay());
  }
  throw new Error("视频拆解仍在后台进行，请稍后返回项目列表查看。");
}

export async function getLatestProjectAnalysis(
  projectId: string,
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/analysis/latest`,
    "读取视频拆解失败",
  );
}

export async function getLatestProjectShotCards(
  projectId: string,
): Promise<AnalysisVersion | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/shot-cards/latest`,
    {},
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取已保存镜头卡片失败（${response.status}）`);
  }
  const version = (await response.json()) as unknown;
  return isAnalysisVersion(version) ? version : null;
}

export async function saveShotCards(
  analysisId: string,
  shots: ShotCard[],
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/analysis/${encodeURIComponent(analysisId)}/shots`,
    "保存镜头卡片失败",
    { method: "PUT", body: JSON.stringify({ shots }) },
  );
}

export async function listProjectCharacters(
  projectId: string,
): Promise<Character[]> {
  return requestApiJson<Character[]>(
    `/api/characters?project_id=${encodeURIComponent(projectId)}`,
    "读取可用人物失败",
  );
}

export async function listProjectCharacterVersions(
  projectId: string,
): Promise<ProjectCharacterVersionOption[]> {
  return requestApiJson<ProjectCharacterVersionOption[]>(
    `/api/projects/${encodeURIComponent(projectId)}/character-versions/available`,
    "读取可用角色版本失败",
  );
}

export type ScriptRewriteResult = {
  rewritten_text: string;
  provider: string;
  model: string;
};

export type ScriptRewriteTask = {
  id: string;
  project_id: string;
  identity_id: string | null;
  ip_profile_hash: string | null;
  ip_profile_snapshot: {
    display_name: string;
    role: string;
    service_scope: string;
    target_audience: string;
    expression_style: string;
    audience_needs?: string;
    factual_background?: string;
    sample_script?: string;
    forbidden_claims?: string;

    profile_version: number;
  } | null;
  source_asset_id: string | null;
  source_text: string;
  instructions?: string;
  status:
    | "PENDING"
    | "RUNNING"
    | "SUCCEEDED"
    | "FAILED"
    | "SUBMISSION_UNCERTAIN";
  attempt: number;
  result: ScriptRewriteResult | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
};

const scriptRewriteTaskWaiters = new Map<string, Promise<ScriptRewriteTask>>();

export async function rewriteProjectScript(
  projectId: string,
  text: string,
  identityId?: string,
  sourceAssetId?: string,
  idempotencyKey: string = newControlWriteIdempotencyKey(),
  instructions = "",
): Promise<ScriptRewriteTask> {
  return requestApiJson<ScriptRewriteTask>(
    `/api/projects/${encodeURIComponent(projectId)}/script-rewrite`,
    "AI 改写失败",
    {
      method: "POST",
      body: JSON.stringify({
        text,
        ...(identityId ? { identity_id: identityId } : {}),
        ...(instructions.trim() ? { instructions: instructions.trim() } : {}),
        ...(sourceAssetId ? { source_asset_id: sourceAssetId } : {}),
        idempotency_key: idempotencyKey,
      }),
    },
  );
}

export async function getScriptRewriteTask(
  taskId: string,
): Promise<ScriptRewriteTask> {
  return requestApiJson<ScriptRewriteTask>(
    `/api/script-rewrite-tasks/${encodeURIComponent(taskId)}`,
    "读取 AI 改写任务失败",
  );
}

export async function getLatestScriptRewriteTask(
  projectId: string,
  identityId?: string | null,
  sourceAssetId?: string | null,
): Promise<ScriptRewriteTask | null> {
  const query = new URLSearchParams({
    identity_scope: identityId ? "identity" : "none",
  });
  if (identityId) query.set("identity_id", identityId);
  if (sourceAssetId) query.set("source_asset_id", sourceAssetId);
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/script-rewrite-tasks/latest?${query}`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取 AI 改写任务失败（${response.status}）`);
  }
  return (await response.json()) as ScriptRewriteTask | null;
}

export async function waitForScriptRewriteTask(
  taskId: string,
): Promise<ScriptRewriteTask> {
  const existing = scriptRewriteTaskWaiters.get(taskId);
  if (existing) {
    return existing;
  }
  const waiter = pollScriptRewriteTask(taskId);
  scriptRewriteTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (scriptRewriteTaskWaiters.get(taskId) === waiter) {
      scriptRewriteTaskWaiters.delete(taskId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollScriptRewriteTask(
  taskId: string,
): Promise<ScriptRewriteTask> {
  const deadline = Date.now() + 10 * 60_000;
  const nextDelay = createPollBackoff();
  while (Date.now() < deadline) {
    const task = await getScriptRewriteTask(taskId);
    if (task.status === "SUCCEEDED") {
      if (!task.result) {
        throw new Error("AI 改写已完成，但结果暂不可用，请刷新后重试。");
      }
      return task;
    }
    if (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN") {
      throw new ScriptRewriteTaskError(task);
    }
    await waitForPoll(nextDelay());
  }
  throw new Error("AI 改写仍在后台执行，请稍后返回查看。");
}

export class ScriptRewriteTaskError extends Error {
  readonly code: string | null;
  readonly retryable: boolean;

  constructor(task: ScriptRewriteTask) {
    super(task.error_message || "AI 改写失败，请重新提交。");
    this.name = "ScriptRewriteTaskError";
    this.code = task.error_code;
    this.retryable = task.retryable;
  }
}

export type CharacterViewType =
  | "FRONT_FACE"
  | "FRONT_HALF"
  | "FRONT_FULL"
  | "LEFT_45"
  | "LEFT_SIDE";

export interface SimpleCharacterView {
  view_type: CharacterViewType;
  asset_id: string;
}

export interface SimpleCharacterResult {
  identity_id: string;
  persona_id: string;
  character_version_id: string;
  publication_hash: string;
  contact_sheet_asset_id: string;
  generation_source: "image_provider" | "local_placeholder";
  views: SimpleCharacterView[];
}

export interface SimpleLibraryEntry {
  identity_id: string;
  persona_id: string | null;
  version_number: number | null;
  display_name: string;
  role: string;
  service_scope: string;
  target_audience: string;
  expression_style: string;
  audience_needs?: string;
  factual_background?: string;
  sample_script?: string;
  forbidden_claims?: string;
  owner_user_id: string | null;
  status: string;
  contact_sheet_asset_id: string | null;
  generation_source: "image_provider" | "local_placeholder" | null;
  scene_look_count: number;
  views: SimpleCharacterView[];
}

export interface SimpleLibraryPage {
  items: SimpleLibraryEntry[];
  next_cursor: string | null;
  total: number;
}

export interface SimpleLibraryPageFilters {
  limit?: number;
  cursor?: string;
  query?: string;
}

export type DurableImageTaskStatus =
  | "PENDING"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "SUBMISSION_UNCERTAIN";

export interface CharacterSheetTask {
  id: string;
  project_id: string | null;
  identity_id: string | null;
  operation: "CREATE" | "REGENERATE" | "SCENE";
  display_name: string;
  status: DurableImageTaskStatus;
  attempt: number;
  result_identity_id: string | null;
  result_version_id: string | null;
  result:
    | SimpleCharacterResult
    | SimpleCharacterRegenerationResult
    | SimpleSceneLook
    | null;
  error_code: string | null;
  error_message: string | null;
  retryable: boolean;
  created_at: string;
  updated_at: string;
  started_at: string | null;
  completed_at: string | null;
}

function createRequestKey(prefix: string): string {
  const suffix =
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${suffix}`;
}

const POLL_BASE_MS = 1_500;
const POLL_GROWTH = 1.5;
const POLL_MAX_MS = 30_000;
const POLL_JITTER = 0.2;

/**
 * S12：轮询退避。任务早期状态变化快，首个间隔保持基准 1.5s；此后按 1.5 倍
 * 指数增长、封顶 30s，并附 ±20% 抖动，避免多客户端同频轮询在服务端形成尖峰。
 * 20 分钟窗口下请求数从固定间隔的约 800 次降到几十次；首个间隔不抖动，
 * 保证初期进度刷新节奏稳定可预期。
 */
function createPollBackoff(): () => number {
  let attempt = 0;
  return () => {
    const delay = Math.min(POLL_BASE_MS * POLL_GROWTH ** attempt, POLL_MAX_MS);
    attempt += 1;
    if (attempt === 1) return delay;
    const jitter = 1 - POLL_JITTER + Math.random() * POLL_JITTER * 2;
    return Math.round(delay * jitter);
  };
}

function waitForPoll(delayMs = 1_500): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, delayMs));
}

const characterSheetTaskWaiters = new Map<
  string,
  {
    promise: Promise<CharacterSheetTask>;
    observers: Set<(task: CharacterSheetTask) => void>;
    last?: CharacterSheetTask;
  }
>();
type FirstFrameTaskObserver = (task: FirstFrameTask) => void;

type FirstFrameTaskWaiter = {
  observers: Set<FirstFrameTaskObserver>;
  promise: Promise<FirstFrameTask>;
};

const firstFrameTaskWaiters = new Map<string, FirstFrameTaskWaiter>();
export async function uploadSimpleCharacter(
  projectId: string | null,
  file: File,
  displayName: string,
  personaName = "",
  imageConsentVersion?: string,
): Promise<SimpleCharacterResult> {
  if (imageConsentVersion !== "2026-09-14-v1") {
    throw new Error("请先阅读并确认人物图像使用授权。");
  }
  const form = new FormData();
  form.append("image_consent_version", imageConsentVersion);
  form.append("image_consent_accepted", "true");
  form.append("file", file);
  form.append("display_name", displayName);
  if (personaName.trim()) {
    form.append("persona_name", personaName.trim());
  }
  form.append("idempotency_key", createRequestKey("character-sheet"));
  const endpoint = projectId
    ? `/api/simple-characters/tasks/${encodeURIComponent(projectId)}/generate`
    : "/api/simple-characters/tasks/generate";
  const task = await requestApiJson<CharacterSheetTask>(
    endpoint,
    "一键创建人物失败",
    { method: "POST", body: form },
  );
  const completed = await waitForCharacterSheetTask(task.id);
  if (!completed.result || !("persona_id" in completed.result)) {
    throw new Error("人物生成任务完成但结果不可用，请重新读取人物库。");
  }
  return completed.result as SimpleCharacterResult;
}

export async function renamePersonIdentity(
  identityId: string,
  displayName: string,
): Promise<PersonIdentity> {
  return requestApiJson<PersonIdentity>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/name`,
    "修改人物名称失败",
    { method: "PATCH", body: JSON.stringify({ display_name: displayName }) },
  );
}

export async function updateSimpleCharacterProfile(
  identityId: string,
  profile: {
    display_name: string;
    role: string;
    service_scope: string;
    target_audience: string;
    expression_style: string;
    audience_needs?: string;
    factual_background?: string;
    sample_script?: string;
    forbidden_claims?: string;
  },
): Promise<SimpleLibraryEntry> {
  return requestApiJson<SimpleLibraryEntry>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/profile`,
    "保存 IP 定位失败",
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(profile),
    },
  );
}

export async function deleteSimpleCharacterIdentity(
  identityId: string,
): Promise<void> {
  const response = await requestApi(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(
      await responseErrorMessage(response, "删除人物失败，请稍后重试。"),
    );
  }
}

export interface SimpleCharacterRegenerationResult {
  identity_id: string;
  persona_id: string;
  character_version_id: string;
  previous_version_id: string;
  version_number: number;
  publication_hash: string;
  contact_sheet_asset_id: string;
  generation_source: "image_provider" | "local_placeholder";
  views: SimpleCharacterView[];
}

export interface SimpleSceneLook {
  identity_id: string;
  persona_id: string;
  character_version_id: string;
  scene_name: string;
  scene_description: string;
  costume_description: string;
  contact_sheet_asset_id: string;
  generation_source: "image_provider" | "local_placeholder";
  views: SimpleCharacterView[];
  published_at?: string | null;
}

// Re-run the identity-preserve contact sheet from the stored source photo.
// The provider call can take 1–3 minutes, so reuse the analysis-sized budget.
export async function regenerateContactSheet(
  identityId: string,
): Promise<SimpleCharacterRegenerationResult> {
  const form = new FormData();
  form.append("idempotency_key", createRequestKey("character-regenerate"));
  const task = await requestApiJson<CharacterSheetTask>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/regenerate-contact-sheet-task`,
    "重新生成五视图失败",
    { method: "POST", body: form },
  );
  const completed = await waitForCharacterSheetTask(task.id);
  if (!completed.result || !("previous_version_id" in completed.result)) {
    throw new Error("人物重新生成任务完成但结果不可用，请重新读取人物库。");
  }
  return completed.result as SimpleCharacterRegenerationResult;
}

export async function getCharacterSheetTask(
  taskId: string,
): Promise<CharacterSheetTask> {
  return requestApiJson<CharacterSheetTask>(
    `/api/simple-characters/task-status/${encodeURIComponent(taskId)}`,
    "读取人物生成任务失败",
  );
}

export async function getLatestCharacterSheetTask(): Promise<CharacterSheetTask | null> {
  return requestApiJson<CharacterSheetTask | null>(
    "/api/simple-characters/tasks/active-or-latest",
    "读取人物生成任务失败",
  );
}

export async function getLatestSceneLookTask(
  identityId: string,
): Promise<CharacterSheetTask | null> {
  return requestApiJson<CharacterSheetTask | null>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/scene-looks/tasks/active-or-latest`,
    "读取场景造型生成任务失败",
  );
}

export async function waitForCharacterSheetTask(
  taskId: string,
  onTask?: (task: CharacterSheetTask) => void,
): Promise<CharacterSheetTask> {
  const existing = characterSheetTaskWaiters.get(taskId);
  if (existing) {
    if (onTask) {
      existing.observers.add(onTask);
      if (existing.last) onTask(existing.last);
    }
    try {
      return await existing.promise;
    } finally {
      if (onTask) existing.observers.delete(onTask);
    }
  }
  const observers = new Set<(task: CharacterSheetTask) => void>();
  if (onTask) observers.add(onTask);
  const waiter: {
    promise: Promise<CharacterSheetTask>;
    observers: typeof observers;
    last?: CharacterSheetTask;
  } = {
    observers,
    promise: pollCharacterSheetTask(taskId, (task) => {
      waiter.last = task;
      for (const observer of observers) observer(task);
    }),
  };
  characterSheetTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (characterSheetTaskWaiters.get(taskId) === waiter) {
      characterSheetTaskWaiters.delete(taskId);
    }
  };
  void waiter.promise.then(clear, clear);
  return waiter.promise;
}

async function pollCharacterSheetTask(
  taskId: string,
  onTask: (task: CharacterSheetTask) => void,
): Promise<CharacterSheetTask> {
  // 场景造型最坏要两轮生成加质检（约 16 分钟），轮询死线留足余量。
  const deadline = Date.now() + 30 * 60_000;
  const nextDelay = createPollBackoff();
  while (Date.now() < deadline) {
    const task = await getCharacterSheetTask(taskId);
    onTask(task);
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN") {
      throw new Error(task.error_message || "人物生成失败，请重新提交。");
    }
    await waitForPoll(nextDelay());
  }
  throw new Error("人物生成仍在后台进行，请稍后返回人物库查看。");
}

export async function listSimpleCharacterLibraryPage(
  filters: SimpleLibraryPageFilters = {},
): Promise<SimpleLibraryPage> {
  const query = new URLSearchParams();
  if (filters.limit !== undefined) {
    query.set("limit", String(filters.limit));
  }
  if (filters.cursor) {
    query.set("cursor", filters.cursor);
  }
  if (filters.query) {
    query.set("query", filters.query);
  }
  const suffix = query.size > 0 ? `?${query.toString()}` : "";
  return requestApiJson<SimpleLibraryPage>(
    `/api/simple-characters/library${suffix}`,
    "读取人物库失败",
  );
}

export async function listSimpleCharacterLibrary(): Promise<
  SimpleLibraryEntry[]
> {
  const entries: SimpleLibraryEntry[] = [];
  let cursor: string | undefined;
  do {
    const page = await listSimpleCharacterLibraryPage({ limit: 100, cursor });
    entries.push(...page.items);
    cursor = page.next_cursor ?? undefined;
  } while (cursor);
  return entries;
}

export async function listCharacterSceneLooks(
  identityId: string,
): Promise<SimpleSceneLook[]> {
  const items: SimpleSceneLook[] = [];
  let offset = 0;
  let total = Number.POSITIVE_INFINITY;
  while (offset < total) {
    const page = await listCharacterSceneLooksPage(identityId, {
      limit: 100,
      offset,
    });
    items.push(...page.items);
    offset += page.items.length;
    total = page.total;
    if (page.items.length === 0) {
      return items;
    }
  }
  return items;
}

export async function listCharacterSceneLooksPage(
  identityId: string,
  { limit = 12, offset = 0 }: { limit?: number; offset?: number } = {},
): Promise<OffsetPage<SimpleSceneLook>> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  return requestApiJson<OffsetPage<SimpleSceneLook>>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/scene-looks?${query.toString()}`,
    "读取人物场景造型失败",
  );
}

export async function createCharacterSceneLook(
  identityId: string,
  input: {
    scene_name: string;
    scene_description: string;
    costume_description: string;
  },
  onTask?: (task: CharacterSheetTask) => void,
): Promise<SimpleSceneLook> {
  const task = await requestApiJson<CharacterSheetTask>(
    `/api/simple-characters/identities/${encodeURIComponent(identityId)}/scene-looks/tasks/generate`,
    "启动场景造型生成失败",
    {
      method: "POST",
      body: JSON.stringify({
        ...input,
        idempotency_key: createRequestKey("scene-look"),
      }),
    },
  );
  onTask?.(task);
  const completed = await waitForCharacterSheetTask(task.id, onTask);
  if (!completed.result || !("scene_name" in completed.result)) {
    throw new Error("场景造型任务完成但结果不可用，请重新读取人物库。");
  }
  return completed.result;
}

export async function downloadCharacterAsset(
  assetId: string,
  filename: string,
): Promise<void> {
  const download = await getCachedCharacterAssetUrl(assetId);
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    CLOUD_OP_TIMEOUT_MS,
  );
  try {
    const response = await fetch(download.url, { signal: controller.signal });
    if (!response.ok) {
      throw new Error(`下载人物视角图失败（${response.status}）`);
    }
    downloadBlob(await response.blob(), filename);
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function getProjectMainCharacter(
  projectId: string,
): Promise<ProjectMainCharacter | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/main-character`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取已选人物失败（${response.status}）`);
  }
  return (await response.json()) as ProjectMainCharacter | null;
}

export async function chooseProjectMainCharacter(
  projectId: string,
  characterId: string,
): Promise<ProjectMainCharacter> {
  return requestApiJson<ProjectMainCharacter>(
    `/api/projects/${encodeURIComponent(projectId)}/main-character`,
    "选择人物失败",
    { method: "PUT", body: JSON.stringify({ character_id: characterId }) },
  );
}

export async function chooseProjectMainCharacterVersion(
  projectId: string,
  characterVersionId: string,
): Promise<ProjectMainCharacter> {
  return requestApiJson<ProjectMainCharacter>(
    `/api/projects/${encodeURIComponent(projectId)}/main-character`,
    "选择角色版本失败",
    {
      method: "PUT",
      body: JSON.stringify({ character_version_id: characterVersionId }),
    },
  );
}

export async function getLatestProjectSourceFrames(
  projectId: string,
): Promise<AnalysisVersion | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取候选源画面失败（${response.status}）`);
  }
  const version = (await response.json()) as unknown;
  return isAnalysisVersion(version) ? version : null;
}

export async function getLatestProjectSourceFrameSelection(
  projectId: string,
): Promise<SourceFrameSelectionState> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/selection/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return { version: null, stale: false };
  }
  if (response.status === 409) {
    return { version: null, stale: true };
  }
  if (!response.ok) {
    throw new Error(`读取已确认源画面失败（${response.status}）`);
  }
  const version = (await response.json()) as unknown;
  return {
    version: isAnalysisVersion(version) ? version : null,
    stale: false,
  };
}

export async function extractSourceFrames(
  projectId: string,
  assetId: string,
  timestampsSeconds: number[],
): Promise<SourceFrameTask> {
  return requestApiJson<SourceFrameTask>(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/extract`,
    "提取候选源画面失败",
    {
      method: "POST",
      body: JSON.stringify({
        asset_id: assetId,
        timestamps_seconds: timestampsSeconds,
        idempotency_key: newControlWriteIdempotencyKey(),
      }),
    },
  );
}

export async function getSourceFrameTask(
  taskId: string,
): Promise<SourceFrameTask> {
  return requestApiJson<SourceFrameTask>(
    `/api/source-frame-tasks/${encodeURIComponent(taskId)}`,
    "读取候选源画面任务失败",
  );
}

export async function cancelSourceFrameTask(
  taskId: string,
): Promise<SourceFrameTask> {
  return requestApiJson<SourceFrameTask>(
    `/api/source-frame-tasks/${encodeURIComponent(taskId)}/cancel`,
    "停止候选源画面任务失败",
    { method: "POST" },
  );
}

export async function getLatestProjectSourceFrameTask(
  projectId: string,
  assetId: string,
): Promise<SourceFrameTask | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/source-frame-tasks/latest?asset_id=${encodeURIComponent(assetId)}`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取候选源画面任务失败（${response.status}）`);
  }
  return (await response.json()) as SourceFrameTask | null;
}

export async function waitForSourceFrameTask(
  taskId: string,
): Promise<SourceFrameTask> {
  const existing = sourceFrameTaskWaiters.get(taskId);
  if (existing) {
    return existing;
  }
  const waiter = pollSourceFrameTask(taskId);
  sourceFrameTaskWaiters.set(taskId, waiter);
  const clear = () => {
    if (sourceFrameTaskWaiters.get(taskId) === waiter) {
      sourceFrameTaskWaiters.delete(taskId);
    }
  };
  void waiter.then(clear, clear);
  return waiter;
}

async function pollSourceFrameTask(taskId: string): Promise<SourceFrameTask> {
  const deadline = Date.now() + 10 * 60_000;
  const nextDelay = createPollBackoff();
  while (Date.now() < deadline) {
    const task = await getSourceFrameTask(taskId);
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED") {
      throw new SourceFrameTaskFailedError(task);
    }
    await waitForPoll(nextDelay());
  }
  throw new Error("候选源画面仍在后台提取，请稍后返回查看。");
}

export async function confirmSourceFrame(
  projectId: string,
  sourceFrameAssetId: string,
  characterFeatures?: SourceFrameCharacterFeatures | null,
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/source-frames/confirm`,
    "确认源画面失败",
    {
      method: "POST",
      body: JSON.stringify({
        source_frame_asset_id: sourceFrameAssetId,
        ...(characterFeatures ? { character_features: characterFeatures } : {}),
      }),
    },
  );
}

export async function getCharacterReferenceRecommendation(
  projectId: string,
): Promise<CharacterReferenceRecommendation> {
  return requestApiJson<CharacterReferenceRecommendation>(
    `/api/projects/${encodeURIComponent(projectId)}/character-reference-recommendation`,
    "读取人物参考图推荐失败",
  );
}

export async function getLatestCharacterReferenceSelection(
  projectId: string,
): Promise<CharacterReferenceSelection | null> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/character-reference-selection/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`读取已确认人物参考图失败（${response.status}）`);
  }
  return (await response.json()) as CharacterReferenceSelection | null;
}

export async function selectCharacterReferences(
  projectId: string,
  input: SelectCharacterReferencesInput,
): Promise<CharacterReferenceSelection> {
  return requestApiJson<CharacterReferenceSelection>(
    `/api/projects/${encodeURIComponent(projectId)}/character-reference-selection`,
    "确认人物参考图失败",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export async function getLatestProjectFirstFrames(
  projectId: string,
): Promise<FirstFrameSelectionState> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return { version: null, stale: false };
  }
  if (response.status === 409) {
    return { version: null, stale: true };
  }
  if (!response.ok) {
    throw new Error(`读取候选首帧失败（${response.status}）`);
  }
  const version = (await response.json()) as unknown;
  return {
    version: isAnalysisVersion(version) ? version : null,
    stale: false,
  };
}

export async function getProjectFirstFrameHistory(
  projectId: string,
): Promise<AnalysisVersion[]> {
  const versions = await requestApiJson<unknown>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/history`,
    "读取首帧历史失败",
  );
  return Array.isArray(versions) && versions.every(isAnalysisVersion)
    ? versions
    : [];
}

export async function getLatestProjectFirstFrameSelection(
  projectId: string,
): Promise<FirstFrameSelectionState> {
  const response = await requestApi(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/selection/latest`,
    { method: "GET" },
  );
  if (response.status === 404) {
    return { version: null, stale: false };
  }
  if (response.status === 409) {
    return { version: null, stale: true };
  }
  if (!response.ok) {
    throw new Error(`读取已确认首帧失败（${response.status}）`);
  }
  const version = (await response.json()) as unknown;
  return {
    version: isAnalysisVersion(version) ? version : null,
    stale: false,
  };
}

export async function generateFirstFrames(
  projectId: string,
  input: GenerateFirstFramesInput,
  onTaskUpdate?: FirstFrameTaskObserver,
): Promise<AnalysisVersion> {
  const task = await requestApiJson<FirstFrameTask>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frame-tasks`,
    "生成人物置换首帧失败",
    {
      method: "POST",
      body: JSON.stringify({
        ...input,
        idempotency_key: createRequestKey("first-frame"),
      }),
    },
  );
  onTaskUpdate?.(task);
  return resumeFirstFrameGeneration(projectId, task.id, onTaskUpdate);
}

export async function resumeFirstFrameGeneration(
  projectId: string,
  taskId: string,
  onTaskUpdate?: FirstFrameTaskObserver,
): Promise<AnalysisVersion> {
  const completed = await waitForFirstFrameTask(taskId, onTaskUpdate);
  const latest = await getLatestProjectFirstFrames(projectId);
  if (
    !completed.result_version_id ||
    !latest.version ||
    latest.version.id !== completed.result_version_id
  ) {
    throw new Error("首帧任务已完成，但最新版本尚未同步，请重新读取项目。");
  }
  return latest.version;
}

export async function getFirstFrameTask(
  taskId: string,
): Promise<FirstFrameTask> {
  return requestApiJson<FirstFrameTask>(
    `/api/first-frame-tasks/${encodeURIComponent(taskId)}`,
    "读取首帧生成任务失败",
  );
}

export async function getLatestFirstFrameTask(
  projectId: string,
): Promise<FirstFrameTask | null> {
  return requestApiJson<FirstFrameTask | null>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frame-tasks/active-or-latest`,
    "读取首帧生成任务失败",
  );
}

export async function waitForFirstFrameTask(
  taskId: string,
  onTaskUpdate?: FirstFrameTaskObserver,
): Promise<FirstFrameTask> {
  let waiter = firstFrameTaskWaiters.get(taskId);
  if (!waiter) {
    const observers = new Set<FirstFrameTaskObserver>();
    const promise = pollFirstFrameTask(taskId, (task) => {
      for (const observer of observers) {
        observer(task);
      }
    });
    waiter = { observers, promise };
    firstFrameTaskWaiters.set(taskId, waiter);
    const clear = () => {
      if (firstFrameTaskWaiters.get(taskId) === waiter) {
        firstFrameTaskWaiters.delete(taskId);
      }
    };
    void promise.then(clear, clear);
  }
  if (onTaskUpdate) {
    waiter.observers.add(onTaskUpdate);
  }
  return waiter.promise.finally(() => {
    if (onTaskUpdate) {
      waiter.observers.delete(onTaskUpdate);
    }
  });
}

async function pollFirstFrameTask(
  taskId: string,
  onTaskUpdate: FirstFrameTaskObserver,
): Promise<FirstFrameTask> {
  // 供应商生成和归档的等待上限为 30 分钟；超时后任务仍在云端继续，
  // 重新进入项目会通过 active-or-latest 接上。
  const deadline = Date.now() + 30 * 60_000;
  const nextDelay = createPollBackoff();
  while (Date.now() < deadline) {
    const task = await getFirstFrameTask(taskId);
    onTaskUpdate(task);
    if (task.status === "SUCCEEDED") {
      return task;
    }
    if (task.status === "FAILED" || task.status === "SUBMISSION_UNCERTAIN") {
      throw new Error(task.error_message || "首帧生成失败，请重新提交。");
    }
    await waitForPoll(nextDelay());
  }
  throw new Error("首帧仍在后台生成，请稍后返回项目查看。");
}

export async function confirmFirstFrame(
  projectId: string,
  firstFrameAssetId: string,
  options?: { allowUnverified?: boolean; candidatesVersionId?: string },
): Promise<AnalysisVersion> {
  return requestApiJson<AnalysisVersion>(
    `/api/projects/${encodeURIComponent(projectId)}/first-frames/confirm`,
    "确认首帧失败",
    {
      method: "POST",
      body: JSON.stringify({
        first_frame_asset_id: firstFrameAssetId,
        ...(options?.allowUnverified ? { allow_unverified: true } : {}),
        // 问题3：历史版本确认显式指明候选版本，后端按该版本校验并放行。
        ...(options?.candidatesVersionId
          ? { first_frame_candidates_version_id: options.candidatesVersionId }
          : {}),
      }),
    },
  );
}

// MATERIAL-PERF-C（P1-2）：签名 URL 有效期 15 分钟，模块级缓存 12 分钟内
// 直接复用（跨页/跨弹层不再重复授权请求）；会话代际变化时整体失效。
const DOWNLOAD_URL_CACHE_TTL_MS = 12 * 60 * 1000;
const downloadUrlCache = new Map<string, { url: string; expiresAt: number }>();

function clearDownloadUrlCache(): void {
  downloadUrlCache.clear();
}

/** 会话凭据代际变化时整体失效签名缓存（换号/登出绝不复用旧授权）。 */
function bumpDownloadUrlCacheEpoch(): void {
  clearDownloadUrlCache();
}

export async function getAssetDownloadUrl(
  assetId: string,
  options: { fresh?: boolean } = {},
): Promise<DownloadUrl> {
  // 素材持久缓存通道要求每次预览都重新授权（吊销/清理必须即时生效，
  // 由既有测试钉住）；其余展示型调用方默认享受 12 分钟签名复用。
  const cached = options.fresh ? undefined : downloadUrlCache.get(assetId);
  if (cached && Date.now() < cached.expiresAt) {
    return { url: cached.url };
  }
  const result = await requestApiJson<DownloadUrl>(
    `/api/assets/${encodeURIComponent(assetId)}/download-url`,
    "读取源画面失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
  const url = resolveManagedMediaUrl(result.url);
  downloadUrlCache.set(assetId, {
    url,
    expiresAt: Date.now() + DOWNLOAD_URL_CACHE_TTL_MS,
  });
  return { url };
}

export async function getCachedCharacterAssetUrl(
  assetId: string,
): Promise<DownloadUrl> {
  const result = await requestApiJson<DownloadUrl>(
    `/api/assets/${encodeURIComponent(assetId)}/cached-url`,
    "读取人物图片缓存失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
  return { ...result, url: resolveManagedMediaUrl(result.url) };
}

const MATERIAL_CACHE_NAME = "video-replica-material-media-v1";
const MATERIAL_CACHE_LOCK = `${MATERIAL_CACHE_NAME}:writes`;
const MATERIAL_CACHE_ROOT = "https://material-cache.invalid/v1/";
const MATERIAL_CACHE_LIMIT = 256 * 1024 * 1024;
const MATERIAL_FILE_LIMIT = 50 * 1024 * 1024;
const materialInvalidations = new Map<string, number>();
const materialFills = new Map<
  string,
  {
    scope: string;
    assetId?: string;
    controller: AbortController;
    promise: Promise<Blob | null>;
    users: number;
  }
>();
let materialDownloadCount = 0;
const materialDownloadWaiters = new Set<() => void>();

export type MaterialCachedPreview = {
  url: string;
  cached: boolean;
  release: () => void;
};
type MaterialAssetMetadata = components["schemas"]["AssetResponse"];
type MaterialCacheContext = {
  scope: string;
  assetId?: string;
  assetInvalidation: number;
  epoch: number;
  invalidation: number;
  signal?: AbortSignal;
};

function materialCacheContext(
  userId: string,
  signal?: AbortSignal,
  assetId?: string,
): MaterialCacheContext {
  if (!userId.trim()) throw new Error("素材缓存需要当前用户");
  const scope = `${encodeURIComponent(new URL(apiBaseUrl()).origin)}/${encodeURIComponent(userId)}/`;
  return {
    scope,
    assetId,
    assetInvalidation:
      materialInvalidations.get(
        `${scope}${encodeURIComponent(assetId ?? "")}`,
      ) ?? 0,
    epoch: workspaceCredentialEpoch,
    invalidation: materialInvalidations.get(scope) ?? 0,
    signal,
  };
}

function requireMaterialContext(context: MaterialCacheContext): void {
  if (context.signal?.aborted)
    throw new DOMException("素材读取已取消", "AbortError");
  if (context.epoch !== workspaceCredentialEpoch)
    throw new Error("素材读取会话已变化，请重试");
  if (
    context.invalidation !== (materialInvalidations.get(context.scope) ?? 0)
  ) {
    throw new DOMException("素材缓存已清理", "AbortError");
  }
  if (
    context.assetId !== undefined &&
    context.assetInvalidation !==
      (materialInvalidations.get(
        `${context.scope}${encodeURIComponent(context.assetId)}`,
      ) ?? 0)
  ) {
    throw new DOMException("素材缓存已移除", "AbortError");
  }
}

function materialCacheAvailable(): boolean {
  return (
    typeof caches !== "undefined" &&
    typeof navigator.locks?.request === "function" &&
    typeof crypto.subtle?.digest === "function" &&
    typeof URL.createObjectURL === "function"
  );
}

async function materialCacheLocked<T>(
  context: MaterialCacheContext,
  action: (cache: Cache) => Promise<T>,
): Promise<T> {
  requireMaterialContext(context);
  return navigator.locks.request(
    MATERIAL_CACHE_LOCK,
    { signal: context.signal },
    async () => {
      requireMaterialContext(context);
      const cache = await caches.open(MATERIAL_CACHE_NAME);
      requireMaterialContext(context);
      const result = await action(cache);
      requireMaterialContext(context);
      return result;
    },
  );
}

function materialStateKey(scope: string, assetId?: string): string {
  return `${MATERIAL_CACHE_ROOT}state/${scope}${assetId === undefined ? "" : encodeURIComponent(assetId)}`;
}

async function materialGeneration(
  cache: Cache,
  scope: string,
  assetId?: string,
): Promise<string> {
  const user =
    (await cache.match(materialStateKey(scope)))?.headers.get(
      "X-Material-Generation",
    ) ?? "0";
  const asset =
    assetId === undefined
      ? "0"
      : ((await cache.match(materialStateKey(scope, assetId)))?.headers.get(
          "X-Material-Generation",
        ) ?? "0");
  return `${user}:${asset}`;
}

function materialMime(value: string | null): string {
  return value?.split(";", 1)[0].trim().toLowerCase() ?? "";
}

function materialMetadataValid(metadata: MaterialAssetMetadata): boolean {
  return (
    /^[a-f0-9]{64}$/i.test(metadata.sha256) &&
    Number.isSafeInteger(metadata.size_bytes) &&
    metadata.size_bytes > 0 &&
    metadata.size_bytes <= MATERIAL_FILE_LIMIT &&
    /^(image|audio|video)\/[a-z0-9.+-]+$/.test(
      materialMime(metadata.content_type),
    )
  );
}

async function materialWait<T>(
  promise: Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) throw new DOMException("素材读取已取消", "AbortError");
  return new Promise<T>((resolve, reject) => {
    const abort = () =>
      reject(new DOMException("素材读取已取消", "AbortError"));
    signal.addEventListener("abort", abort, { once: true });
    promise
      .then(resolve, reject)
      .finally(() => signal.removeEventListener("abort", abort));
  });
}

async function materialVerifiedBytes(
  response: Response,
  metadata: MaterialAssetMetadata,
  context: MaterialCacheContext,
): Promise<Uint8Array<ArrayBuffer>> {
  if (
    !response.ok ||
    response.type === "opaque" ||
    !response.body ||
    materialMime(response.headers.get("Content-Type")) !==
      materialMime(metadata.content_type)
  ) {
    throw new Error("素材缓存内容类型无效");
  }
  const declared = response.headers.get("Content-Length");
  if (declared !== null && Number(declared) !== metadata.size_bytes)
    throw new Error("素材缓存大小不符");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const result = await materialWait(reader.read(), context.signal);
      requireMaterialContext(context);
      if (result.done) break;
      size += result.value.byteLength;
      if (size > MATERIAL_FILE_LIMIT || size > metadata.size_bytes)
        throw new Error("素材缓存超过大小上限");
      chunks.push(result.value);
    }
  } catch (error) {
    void reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }
  if (size !== metadata.size_bytes) throw new Error("素材缓存不完整");
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  requireMaterialContext(context);
  const hash = Array.from(digest, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  if (hash !== metadata.sha256.toLowerCase())
    throw new Error("素材缓存校验失败");
  return bytes;
}

function materialResponse(
  bytes: Uint8Array<ArrayBuffer>,
  metadata: MaterialAssetMetadata,
): Response {
  return new Response(bytes, {
    headers: {
      "Content-Type": materialMime(metadata.content_type),
      "Content-Length": String(bytes.byteLength),
      "X-Material-Sha256": metadata.sha256.toLowerCase(),
      "X-Material-Accessed": String(Date.now()),
    },
  });
}

async function materialRead(
  cache: Cache,
  key: string,
  metadata: MaterialAssetMetadata,
  context: MaterialCacheContext,
): Promise<Blob | null> {
  const response = await cache.match(key);
  requireMaterialContext(context);
  if (!response) return null;
  let bytes: Uint8Array<ArrayBuffer>;
  try {
    bytes = await materialVerifiedBytes(response, metadata, context);
  } catch {
    requireMaterialContext(context);
    await cache.delete(key);
    return null;
  }
  requireMaterialContext(context);
  await cache.put(key, materialResponse(bytes, metadata));
  requireMaterialContext(context);
  return new Blob([bytes], { type: materialMime(metadata.content_type) });
}

async function materialEntries(
  cache: Cache,
): Promise<{ key: Request; bytes: number; accessed: number }[]> {
  const entries = [];
  for (const key of await cache.keys()) {
    if (!key.url.startsWith(`${MATERIAL_CACHE_ROOT}media/`)) continue;
    const response = await cache.match(key);
    const bytes = Number(response?.headers.get("Content-Length"));
    const accessed = Number(response?.headers.get("X-Material-Accessed"));
    if (
      !Number.isSafeInteger(bytes) ||
      bytes <= 0 ||
      bytes > MATERIAL_FILE_LIMIT ||
      !Number.isFinite(accessed)
    ) {
      await cache.delete(key);
    } else entries.push({ key, bytes, accessed });
  }
  return entries.sort(
    (a, b) => a.accessed - b.accessed || a.key.url.localeCompare(b.key.url),
  );
}

async function materialDownloadSlot<T>(
  context: MaterialCacheContext,
  work: () => Promise<T>,
): Promise<T> {
  while (materialDownloadCount >= 2) {
    let wake: (() => void) | undefined;
    const ready = new Promise<void>((resolve) => {
      wake = resolve;
      materialDownloadWaiters.add(resolve);
    });
    try {
      await materialWait(ready, context.signal);
    } finally {
      if (wake) materialDownloadWaiters.delete(wake);
    }
    requireMaterialContext(context);
  }
  requireMaterialContext(context);
  materialDownloadCount += 1;
  try {
    return await work();
  } finally {
    materialDownloadCount -= 1;
    for (const wake of materialDownloadWaiters) wake();
  }
}

async function materialPopulate(
  context: MaterialCacheContext,
  key: string,
  generation: string,
  metadata: MaterialAssetMetadata,
  url: string,
): Promise<Blob | null> {
  // A per-object lock also deduplicates complete downloads across browser tabs.
  return navigator.locks.request(
    `${MATERIAL_CACHE_NAME}:fill:${key}`,
    { signal: context.signal },
    async () => {
      const existing = await materialCacheLocked(context, async (cache) => {
        if (
          (await materialGeneration(cache, context.scope, context.assetId)) !==
          generation
        )
          throw new DOMException("素材缓存已清理", "AbortError");
        return materialRead(cache, key, metadata, context);
      });
      if (existing) return existing;
      return materialDownloadSlot(context, async () => {
        const controller = new AbortController();
        const abort = () => controller.abort();
        context.signal?.addEventListener("abort", abort, { once: true });
        const timeout = window.setTimeout(abort, 60_000);
        try {
          requireMaterialContext(context);
          const response = await materialWait(
            fetch(url, {
              signal: controller.signal,
              credentials: "omit",
              cache: "no-store",
              referrerPolicy: "no-referrer",
            }),
            controller.signal,
          );
          requireMaterialContext(context);
          if (response.status === 401 || response.status === 403) {
            throw Object.assign(new Error("素材访问权限已失效"), {
              materialAuthorizationDenied: true,
            });
          }
          const bytes = await materialVerifiedBytes(response, metadata, {
            ...context,
            signal: controller.signal,
          });
          return await materialCacheLocked(context, async (cache) => {
            if (
              (await materialGeneration(
                cache,
                context.scope,
                context.assetId,
              )) !== generation
            )
              throw new DOMException("素材缓存已清理", "AbortError");
            requireMaterialContext(context);
            const entries = await materialEntries(cache);
            let total = entries.reduce(
              (sum, entry) => sum + (entry.key.url === key ? 0 : entry.bytes),
              0,
            );
            for (const entry of entries) {
              if (total + bytes.byteLength <= MATERIAL_CACHE_LIMIT) break;
              if (entry.key.url !== key) {
                await cache.delete(entry.key);
                total -= entry.bytes;
              }
            }
            requireMaterialContext(context);
            await cache.put(key, materialResponse(bytes, metadata));
            // Clear/credential changes in this tab can occur while Cache.put awaits.
            try {
              requireMaterialContext(context);
            } catch (error) {
              await cache.delete(key);
              throw error;
            }
            return new Blob([bytes], {
              type: materialMime(metadata.content_type),
            });
          });
        } catch (error) {
          requireMaterialContext(context);
          if (controller.signal.aborted) throw new Error("素材缓存下载超时");
          throw error;
        } finally {
          window.clearTimeout(timeout);
          context.signal?.removeEventListener("abort", abort);
        }
      });
    },
  );
}

function materialBlobPreview(blob: Blob): MaterialCachedPreview {
  const url = URL.createObjectURL(blob);
  let released = false;
  return {
    url,
    cached: true,
    release: () => {
      if (!released) {
        released = true;
        URL.revokeObjectURL(url);
      }
    },
  };
}

async function materialReadGeneration(
  context: MaterialCacheContext,
  assetId: string,
): Promise<string | null> {
  if (!materialCacheAvailable()) return null;
  try {
    return await materialCacheLocked(context, (cache) =>
      materialGeneration(cache, context.scope, assetId),
    );
  } catch {
    requireMaterialContext(context);
  }
  return null;
}

/** 授权后的缓存判定（单资产与批量共用）：命中本机缓存则出 Blob，
 * 未命中且允许填充时后台写缓存；否则退回在线签名 URL。 */
async function materialPreviewAfterAuthorization(
  context: MaterialCacheContext,
  assetId: string,
  url: string,
  metadata: MaterialAssetMetadata,
  generationAtStart: string | null,
  populate: boolean,
): Promise<MaterialCachedPreview> {
  const online = { url, cached: false, release: () => undefined };
  if (generationAtStart === null || !materialMetadataValid(metadata))
    return online;
  const key = `${MATERIAL_CACHE_ROOT}media/${context.scope}${encodeURIComponent(assetId)}/${metadata.sha256.toLowerCase()}`;
  try {
    const initial = await materialCacheLocked(context, async (cache) => {
      const generation = await materialGeneration(
        cache,
        context.scope,
        assetId,
      );
      if (generation !== generationAtStart)
        throw new DOMException("素材缓存已清理", "AbortError");
      return {
        generation,
        blob: await materialRead(cache, key, metadata, context),
      };
    });
    requireMaterialContext(context);
    if (initial.blob) return materialBlobPreview(initial.blob);
    if (!populate) return online;
    const fillKey = `${key}:${initial.generation}:${context.epoch}:${context.invalidation}:${context.assetInvalidation}`;
    let fill = materialFills.get(fillKey);
    if (!fill) {
      const controller = new AbortController();
      fill = {
        scope: context.scope,
        assetId,
        controller,
        users: 0,
        promise: materialPopulate(
          { ...context, signal: controller.signal },
          key,
          initial.generation,
          metadata,
          url,
        ),
      };
      materialFills.set(fillKey, fill);
      const captured = fill;
      void fill.promise
        .finally(() => {
          if (materialFills.get(fillKey) === captured)
            materialFills.delete(fillKey);
        })
        .catch(() => undefined);
    }
    fill.users += 1;
    try {
      const blob = await materialWait(fill.promise, context.signal);
      requireMaterialContext(context);
      return blob ? materialBlobPreview(blob) : online;
    } finally {
      fill.users -= 1;
      if (!fill.users) fill.controller.abort();
    }
  } catch (error) {
    requireMaterialContext(context);
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
    if (
      typeof error === "object" &&
      error !== null &&
      "materialAuthorizationDenied" in error
    )
      throw error;
    return online;
  }
}

export async function getMaterialCachedPreview(
  userId: string,
  assetId: string,
  options: { populate?: boolean; signal?: AbortSignal } = {},
): Promise<MaterialCachedPreview> {
  const context = materialCacheContext(userId, options.signal, assetId);
  requireMaterialContext(context);
  // Capture invalidation before remote authorization waits. Another tab may
  // clear this user while those requests are in flight; it must win over them.
  // Only the non-sensitive generation marker is read before authorization.
  const generationAtStart = await materialReadGeneration(context, assetId);
  // Fresh authorization is required even when every media byte is already local.
  const { url } = await materialWait(
    getAssetDownloadUrl(assetId, { fresh: true }),
    options.signal,
  );
  requireMaterialContext(context);
  const metadata = await requestApiJson<MaterialAssetMetadata>(
    `/api/assets/${encodeURIComponent(assetId)}`,
    "读取素材信息失败",
    { signal: options.signal },
  );
  requireMaterialContext(context);
  if (!url) throw new Error("素材预览地址不可用");
  return materialPreviewAfterAuthorization(
    context,
    assetId,
    url,
    metadata,
    generationAtStart,
    options.populate ?? false,
  );
}

/** 批量预览解析结果：previews 键为请求 id；thumbnails 仅为带缩略图键的视频
 * 资产（MATERIAL-THUMBS-B，7 天有效签名 URL），其余 id 不出现在 thumbnails。 */
export type MaterialBatchPreviews = {
  previews: Record<string, MaterialCachedPreview>;
  thumbnails: Record<string, string>;
};

/** 批量预览解析（MATERIAL-PERF-A P0-2 + MATERIAL-THUMBS-B P0-3）：一次批量授权
 * + 逐条本机缓存判定，替代素材库网格的逐瓦片 N+1 授权请求。授权失败或被
 * 拒绝的 id 不出现在 previews/thumbnails 中，由调用方按失败处理。 */
export async function getMaterialBatchPreviews(
  userId: string,
  entries: { id: string; populate: boolean }[],
  options: { signal?: AbortSignal } = {},
): Promise<MaterialBatchPreviews> {
  const unique = [...new Set(entries.map((entry) => entry.id).filter(Boolean))];
  if (!unique.length) return { previews: {}, thumbnails: {} };
  const contexts = new Map<string, MaterialCacheContext>();
  const generations = new Map<string, string | null>();
  const populateById = new Map<string, boolean>(
    entries.map((entry) => [entry.id, entry.populate]),
  );
  for (const assetId of unique) {
    const context = materialCacheContext(userId, options.signal, assetId);
    requireMaterialContext(context);
    contexts.set(assetId, context);
    generations.set(assetId, await materialReadGeneration(context, assetId));
  }
  const authorized = await materialWait(
    requestApiJson<components["schemas"]["DownloadUrlsResponse"]>(
      "/api/assets/download-urls",
      "批量读取素材预览授权失败",
      {
        method: "POST",
        body: JSON.stringify({ asset_ids: unique }),
        signal: options.signal,
      },
    ),
    options.signal,
  );
  requireMaterialContext(materialCacheContext(userId, options.signal));
  const results: Record<string, MaterialCachedPreview> = {};
  const thumbnails: Record<string, string> = {};
  for (const item of authorized.items) {
    const context = contexts.get(item.asset_id);
    if (!context || !item.url) continue;
    // 服务端签发的是站内相对路径；桌面端（tauri:// origin）与前后端分域名
    // 部署都不能直接喂给 img/video，必须与单资产通道同口径绝对化。
    if (item.thumbnail_url)
      thumbnails[item.asset_id] = resolveManagedMediaUrl(item.thumbnail_url);
    const metadata: MaterialAssetMetadata = {
      id: item.asset_id,
      project_id: null,
      kind: "material_image",
      sha256: item.sha256 ?? "",
      size_bytes: item.size_bytes ?? 0,
      content_type: item.content_type ?? null,
    };
    results[item.asset_id] = await materialPreviewAfterAuthorization(
      context,
      item.asset_id,
      resolveManagedMediaUrl(item.url),
      metadata,
      generations.get(item.asset_id) ?? null,
      populateById.get(item.asset_id) ?? false,
    );
  }
  return { previews: results, thumbnails };
}

/** 兼容包装（MATERIAL-PERF-A 语义）：只要预览映射、不带缩略图。 */
export async function getMaterialCachedPreviews(
  userId: string,
  entries: { id: string; populate: boolean }[],
  options: { signal?: AbortSignal } = {},
): Promise<Record<string, MaterialCachedPreview>> {
  const { previews } = await getMaterialBatchPreviews(userId, entries, options);
  return previews;
}

export async function getMaterialCacheUsage(
  userId: string,
): Promise<{ bytes: number; limitBytes: number; available: boolean }> {
  const context = materialCacheContext(userId);
  const unavailable = {
    bytes: 0,
    limitBytes: MATERIAL_CACHE_LIMIT,
    available: false,
  };
  if (!materialCacheAvailable()) return unavailable;
  try {
    const bytes = await materialCacheLocked(context, async (cache) =>
      (await materialEntries(cache))
        .filter((entry) =>
          entry.key.url.startsWith(
            `${MATERIAL_CACHE_ROOT}media/${context.scope}`,
          ),
        )
        .reduce((sum, entry) => sum + entry.bytes, 0),
    );
    requireMaterialContext(context);
    return { bytes, limitBytes: MATERIAL_CACHE_LIMIT, available: true };
  } catch {
    requireMaterialContext(context);
    return unavailable;
  }
}

async function materialClear(userId: string, assetId?: string): Promise<void> {
  const previous = materialCacheContext(userId);
  const invalidationKey = `${previous.scope}${assetId === undefined ? "" : encodeURIComponent(assetId)}`;
  materialInvalidations.set(
    invalidationKey,
    (materialInvalidations.get(invalidationKey) ?? 0) + 1,
  );
  for (const fill of materialFills.values()) {
    if (
      fill.scope === previous.scope &&
      (assetId === undefined || fill.assetId === assetId)
    )
      fill.controller.abort();
  }
  const context = materialCacheContext(userId);
  if (!materialCacheAvailable()) return;
  await materialCacheLocked(context, async (cache) => {
    await cache.put(
      materialStateKey(context.scope, assetId),
      new Response(null, {
        headers: { "X-Material-Generation": crypto.randomUUID() },
      }),
    );
    requireMaterialContext(context);
    const prefix = `${MATERIAL_CACHE_ROOT}media/${context.scope}${assetId === undefined ? "" : `${encodeURIComponent(assetId)}/`}`;
    for (const key of await cache.keys()) {
      requireMaterialContext(context);
      if (key.url.startsWith(prefix)) await cache.delete(key);
    }
  });
}

export function clearMaterialCache(userId: string): Promise<void> {
  return materialClear(userId);
}
export function evictMaterialCachedPreview(
  userId: string,
  assetId: string,
): Promise<void> {
  return materialClear(userId, assetId);
}

export function readSourceFrameCandidates(
  version: AnalysisVersion,
): SourceFrameCandidates | null {
  const payload = version.payload;
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.requested_timestamps_seconds) ||
    !payload.requested_timestamps_seconds.every(
      (timestamp) => typeof timestamp === "number",
    ) ||
    !Array.isArray(payload.candidates) ||
    !payload.candidates.every(isSourceFrameCandidate)
  ) {
    return null;
  }
  return {
    requested_timestamps_seconds: payload.requested_timestamps_seconds,
    semantic_quality_status:
      payload.semantic_quality_status === "VERIFIED" ||
      payload.semantic_quality_status === "UNAVAILABLE" ||
      payload.semantic_quality_status === "NOT_REQUESTED"
        ? payload.semantic_quality_status
        : "NOT_REQUESTED",
    candidates: payload.candidates,
  };
}

export function readFirstFrameCandidates(
  version: AnalysisVersion,
): FirstFrameCandidates | null {
  const payload = version.payload;
  if (
    !isRecord(payload) ||
    typeof payload.provider !== "string" ||
    (payload.model !== "gpt-image-2" &&
      payload.model !== "nano-banana-pro-2k") ||
    typeof payload.prompt !== "string" ||
    !Array.isArray(payload.candidates) ||
    !payload.candidates.every(isFirstFrameCandidate)
  ) {
    return null;
  }
  return {
    replace_scene: payload.replace_scene === true,
    review_mode:
      payload.review_mode === "HUMAN_CONFIRMATION"
        ? "HUMAN_CONFIRMATION"
        : "AUTOMATIC_QUALITY",
    aspect_ratio: ["9:16", "16:9", "1:1", "3:4", "4:3"].includes(
      String(payload.aspect_ratio),
    )
      ? (payload.aspect_ratio as GenerateFirstFramesInput["aspect_ratio"])
      : null,
    source_frame_asset_id:
      typeof payload.source_frame_asset_id === "string"
        ? payload.source_frame_asset_id
        : undefined,
    character_reference_asset_ids:
      Array.isArray(payload.character_reference_asset_ids) &&
      payload.character_reference_asset_ids.every(
        (id) => typeof id === "string",
      )
        ? payload.character_reference_asset_ids
        : [],
    provider: payload.provider,
    model: payload.model,
    prompt: payload.prompt,
    candidates: payload.candidates,
    reconstruction_mode:
      typeof payload.reconstruction_mode === "string"
        ? payload.reconstruction_mode
        : null,
    character_contract: isRecord(payload.character_contract)
      ? payload.character_contract
      : null,
    project_character_appearance_version_id:
      typeof payload.project_character_appearance_version_id === "string"
        ? payload.project_character_appearance_version_id
        : null,
    project_appearance: readProjectAppearance(payload.project_appearance),
  };
}

function readProjectAppearance(value: unknown): ProjectAppearanceSpec | null {
  if (
    !isRecord(value) ||
    typeof value.category !== "string" ||
    typeof value.scene !== "string" ||
    typeof value.subject !== "string" ||
    typeof value.outfit_description !== "string" ||
    typeof value.selection_reason !== "string"
  ) {
    return null;
  }
  return {
    category: value.category,
    scene: value.scene,
    subject: value.subject,
    outfit_description: value.outfit_description,
    selection_reason: value.selection_reason,
  };
}

export function readFirstFrameSelectionPayload(
  version: AnalysisVersion,
): FirstFrameSelectionPayload | null {
  const payload = version.payload;
  if (
    !isRecord(payload) ||
    typeof payload.first_frame_candidates_version_id !== "string" ||
    typeof payload.first_frame_asset_id !== "string"
  ) {
    return null;
  }
  return {
    first_frame_candidates_version_id:
      payload.first_frame_candidates_version_id,
    first_frame_asset_id: payload.first_frame_asset_id,
  };
}

export function readAnalysisH3Prompt(
  version: { payload: Record<string, unknown> } | null | undefined,
): string {
  const result = version?.payload.generation_prompt as
    | { status?: string; prompt_text?: string }
    | undefined;
  return result?.status === "READY" && typeof result.prompt_text === "string"
    ? result.prompt_text
    : "";
}

export function readAnalysisPayload(
  version: AnalysisVersion,
): AnalysisPayload | null {
  const analysis = version.payload.analysis;
  if (!isRecord(analysis) || typeof analysis.summary !== "string") {
    return null;
  }
  if (
    typeof analysis.duration_seconds !== "number" ||
    !Array.isArray(analysis.shots) ||
    !analysis.shots.every(isShotCard)
  ) {
    return null;
  }
  return {
    summary: analysis.summary,
    duration_seconds: analysis.duration_seconds,
    original_script:
      typeof analysis.original_script === "string"
        ? analysis.original_script
        : analysis.shots
            .map((shot) => shot.spoken_text)
            .filter(Boolean)
            .join(""),
    shots: analysis.shots,
  };
}

export function readAnalysisProvider(
  version: AnalysisVersion,
): AnalysisProvider | null {
  const responseRef = version.payload.provider_response_ref;
  if (!isRecord(responseRef) || !isRecord(responseRef.raw)) {
    return null;
  }
  const provider = responseRef.raw.provider;
  return provider === "apilio_gemini" || provider === "fake_gemini"
    ? provider
    : null;
}

function isSourceFrameCandidate(value: unknown): value is SourceFrameCandidate {
  return (
    isRecord(value) &&
    typeof value.asset_id === "string" &&
    typeof value.timestamp_seconds === "number" &&
    (typeof value.score === "number" || value.score === null)
  );
}

function isFirstFrameCandidate(value: unknown): value is FirstFrameCandidate {
  const qualityValid =
    !isRecord(value) ||
    value.quality === undefined ||
    value.quality === null ||
    (isRecord(value.quality) &&
      typeof value.quality.passed === "boolean" &&
      typeof value.quality.attempt === "number" &&
      Array.isArray(value.quality.issue_codes) &&
      value.quality.issue_codes.every((code) => typeof code === "string") &&
      isRecord(value.quality.inspection));
  return (
    isRecord(value) &&
    typeof value.asset_id === "string" &&
    typeof value.storage_key === "string" &&
    typeof value.storage_uri === "string" &&
    typeof value.sha256 === "string" &&
    typeof value.size_bytes === "number" &&
    typeof value.content_type === "string" &&
    qualityValid
  );
}

export function readShotCardPayload(
  version: AnalysisVersion,
): ShotCardPayload | null {
  const payload = version.payload;
  if (
    typeof payload.source_analysis_version_id !== "string" ||
    typeof payload.duration_seconds !== "number" ||
    !Array.isArray(payload.shots) ||
    !payload.shots.every(isShotCard)
  ) {
    return null;
  }
  return {
    source_analysis_version_id: payload.source_analysis_version_id,
    duration_seconds: payload.duration_seconds,
    shots: payload.shots,
  };
}

export async function getSettings(): Promise<SettingsSnapshot> {
  return requestAdminJson<SettingsSnapshot>(
    "/api/admin/settings",
    "设置暂不可用",
  );
}

export async function updateProviderSettings(
  provider: ProviderName,
  config: Record<string, string>,
): Promise<ProviderSettings> {
  return requestAdminJson<ProviderSettings>(
    `/api/admin/settings/providers/${provider}`,
    "保存设置失败",
    { method: "PUT", body: JSON.stringify({ config }) },
  );
}

export async function revealProviderSecret(
  provider: ProviderName,
  field: string,
): Promise<string> {
  const result = await requestAdminJson<{ value: string }>(
    `/api/admin/settings/providers/${provider}/secrets/${encodeURIComponent(field)}/reveal`,
    "读取已保存密钥失败",
    { method: "POST" },
  );
  return result.value;
}

export async function updateRuntimeSettings(
  runtime: RuntimeSettings,
): Promise<RuntimeSettings> {
  return requestAdminJson<RuntimeSettings>(
    "/api/admin/settings/runtime",
    "保存运行设置失败",
    { method: "PATCH", body: JSON.stringify(runtime) },
  );
}

export async function updateBillingSettings(input: {
  internal_base_unit_price_fen: number;
  oral_unit_price_fen: number;
  min_recharge_fen: number;
  recharge_step_fen: number;
}): Promise<BillingSettings> {
  return requestAdminJson<BillingSettings>(
    "/api/admin/settings/billing",
    "保存口播价格失败",
    { method: "PATCH", body: JSON.stringify(input) },
  );
}

export async function runSettingsDiagnostic(): Promise<SettingsDiagnosticReport> {
  return requestAdminJson<SettingsDiagnosticReport>(
    "/api/admin/settings/diagnostic-test",
    "测试设置失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export async function testProviderConnection(
  provider: ProviderName,
): Promise<ProviderTestResult> {
  return requestAdminJson<ProviderTestResult>(
    `/api/admin/settings/providers/${provider}/connection-test`,
    "连接测试失败",
    { method: "POST" },
    CLOUD_OP_TIMEOUT_MS,
  );
}

export type ProviderTestResult = {
  status: string;
  provider: string;
  test_kind: string;
  account_credit?: number;
};

export async function downloadDiagnosticReport(
  downloadUrl: string,
  reportId: string,
): Promise<void> {
  const response = await requestAdmin(downloadUrl, { method: "GET" });
  if (!response.ok) {
    throw new Error(`下载诊断日志失败（${response.status}）`);
  }

  downloadBlob(await response.blob(), `settings-diagnostic-${reportId}.json`);
}

async function downloadControlCsv(
  path: string,
  filename: string,
): Promise<LedgerExportSummary> {
  const response = await requestControl(
    path,
    { method: "GET" },
    CLOUD_OP_TIMEOUT_MS,
  );
  if (!response.ok) {
    throw new Error(`下载管理导出失败（${response.status}）`);
  }

  downloadBlob(await response.blob(), filename);
  const totalText = response.headers.get("X-Export-Total");
  const returnedText = response.headers.get("X-Export-Returned");
  const truncatedText = response.headers.get("X-Export-Truncated");
  if (
    !totalText ||
    !returnedText ||
    !/^\d+$/.test(totalText) ||
    !/^\d+$/.test(returnedText)
  )
    return null;
  const total = Number(totalText);
  const returned = Number(returnedText);
  if (
    !Number.isSafeInteger(total) ||
    !Number.isSafeInteger(returned) ||
    returned > total ||
    truncatedText !== String(total > returned)
  )
    return null;
  return { total, returned, truncated: total > returned };
}

function downloadBlob(blob: Blob, filename: string): void {
  const blobUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // Defer revocation so WebView2/WKWebView have time to start the download.
  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 1_000);
}

async function requestJson<T>(path: string, errorPrefix: string): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );

  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      signal: controller.signal,
    });

    if (!response.ok) {
      throw new Error(await responseErrorMessage(response, errorPrefix));
    }

    return (await response.json()) as T;
  } finally {
    window.clearTimeout(timeout);
  }
}

async function requestAdminJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const response = await requestAdmin(path, init, timeoutMs);
  if (!response.ok) {
    const details = await responseErrorDetails(response, errorPrefix);
    const error = new Error(details.message) as RequestError;
    error.status = response.status;
    error.code = details.code;
    throw error;
  }
  return (await response.json()) as T;
}

async function requestControlJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const response = await requestControl(path, init, timeoutMs);
  if (!response.ok) {
    const details = await responseErrorDetails(response, errorPrefix);
    const error = new Error(details.message) as RequestError;
    error.status = response.status;
    error.code = details.code;
    throw error;
  }
  return (await response.json()) as T;
}

export type WorkspaceSearchKind = "video" | "script" | "person" | "material";
export type WorkspaceNotifications = {
  enabled: boolean;
  unread_count: number;
  items: Array<{
    id: string;
    task_id: string;
    task_kind: "generation_batch" | "oral_task";
    title: string;
    status: string;
    occurred_at: string;
    unread: boolean;
  }>;
};
export function getWorkspaceNotifications() {
  return requestApiJson<WorkspaceNotifications>(
    "/api/studio/notifications",
    "读取任务通知失败",
  );
}
export function markWorkspaceNotificationsRead() {
  return requestApiJson<{ read_before: string }>(
    "/api/studio/notifications/read",
    "标记已读失败",
    { method: "POST" },
  );
}
export type WorkspaceSearchItem = {
  id: string;
  kind: WorkspaceSearchKind;
  title: string;
  description: string;
  platform: ViralPlatform | null;
  person: SimpleLibraryEntry | null;
};
export type WorkspaceSearchPage = {
  items: WorkspaceSearchItem[];
  total: number;
  page: number;
  page_size: number;
};

export function searchWorkspace(
  query: string,
  kind: WorkspaceSearchKind,
  page = 1,
) {
  const params = new URLSearchParams({
    q: query,
    kind,
    page: String(page),
    page_size: "12",
  });
  return requestApiJson<WorkspaceSearchPage>(
    `/api/studio/search?${params}`,
    "搜索暂不可用，请重试",
  );
}

async function requestApiJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const response = await requestApi(path, init, timeoutMs);
  if (!response.ok) {
    const details = await responseErrorDetails(response, errorPrefix);
    const error = new Error(details.message) as RequestError;
    error.status = response.status;
    error.code = details.code;
    error.retryable = details.retryable;
    error.staleReasons = details.staleReasons;
    error.requestId = response.headers?.get?.("X-Request-Id") ?? undefined;
    throw error;
  }
  return (await response.json()) as T;
}

async function requestGenerationJson<T>(
  path: string,
  errorPrefix: string,
  init: RequestInit = {},
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<T> {
  try {
    return await requestApiJson<T>(path, errorPrefix, init, timeoutMs);
  } catch (error) {
    throw generationRequestError(error, errorPrefix);
  }
}

// 合成最终提示词链路的 409：每个 code 对应一个不同的自救动作（补开场衔接、勾压缩、
// 改文案、重存分镜）。通用兜底文案（"上游内容已变化"）描述的是版本级联过期，套到
// 这些 code 上会丢掉服务端已经给出的可行动原因，用户看不出该补哪一步。
const GENERATION_CONFLICT_MESSAGES: Readonly<Record<string, string>> = {
  FIRST_FRAME_ALIGNMENT_REQUIRED:
    "首帧不是视频开头，请在「开场衔接」里写明如何从这张画面开始，再合成。",
  TIMELINE_CONFIRMATION_REQUIRED:
    "源视频长于目标时长，请勾选压缩确认或调整目标时长，再合成。",
  SCRIPT_TAG_INVALID: "确认文案需为纯文本，请删除其中的提示词标签后重试。",
  DIALOGUE_MISMATCH: "最终稿台词与确认文案不一致，请核对文案和分镜后重新合成。",
  SCRIPT_STALE: "文案不是基于当前分镜保存的，请重新保存文案后再合成。",
  SHOT_CARD_STALE: "分镜不是基于当前拆解保存的，请重新保存分镜后再合成。",
  SCRIPT_SHOT_CARD_MISMATCH:
    "文案与当前分镜版本不匹配，请重新保存文案后再合成。",
  FIRST_FRAME_CONFIRMATION_REQUIRED:
    "请先在「首帧置换」里确认一张首帧，再合成。",
  FIRST_FRAME_QUALITY_NOT_VERIFIED:
    "首帧的质检结果已失效：请回到「首帧置换」重新生成并确认首帧，再合成。",
  SHOT_CARD_TIMELINE_INVALID: "拆解结果缺少有效时间轴，请重新拆解视频。",
  PROMPT_STALE:
    "上游（拆解、分镜、文案或首帧）已更新，最终提示词已失效：请重新合成后再继续。",
};

// 服务端在 409 详情里带上 stale_reasons：到底是哪一环又生成了新版本。把原因翻成
// 环节名（hint），再按当前 code 给出该做的下一步（action），用户才能直接定位。
const STALE_STAGE_HINTS: Readonly<Record<string, string>> = {
  ANALYSIS_SUPERSEDED: "视频拆解已更新",
  SHOT_CARD_SUPERSEDED: "分镜已更新",
  SCRIPT_SUPERSEDED: "口播文案已更新",
  PROMPT_SUPERSEDED: "最终提示词已更新",
  TEMPLATE_SUPERSEDED: "合成模板已升级",
  FIRST_FRAME_SUPERSEDED: "首帧选择已更新",
};

const STALE_CONFLICT_ACTIONS: Readonly<Record<string, string>> = {
  SCRIPT_STALE: "请重新保存文案后再合成。",
  SHOT_CARD_STALE: "请重新保存分镜、再重新保存文案后合成。",
  PROMPT_STALE: "请重新合成最终提示词后再继续。",
};

// 当前提示词已被新版本取代时用户手上就有最新一版：让他改用最新版，别白跑一次重新合成。
const PROMPT_SUPERSEDED_ACTION = "请改用最新一版，或重新合成后再继续。";

// 动作必须对 stale_reasons 敏感：拆解更新会让分镜一并过期，只重存文案会被服务端再拒一次。
function staleConflictAction(code: string, staleReasons: string[]): string {
  if (code === "SCRIPT_STALE" && staleReasons.includes("ANALYSIS_SUPERSEDED")) {
    return STALE_CONFLICT_ACTIONS.SHOT_CARD_STALE;
  }
  if (code === "PROMPT_STALE" && staleReasons.includes("PROMPT_SUPERSEDED")) {
    return PROMPT_SUPERSEDED_ACTION;
  }
  return STALE_CONFLICT_ACTIONS[code];
}

function staleStageConflictMessage(
  code: string,
  staleReasons?: string[],
): string | undefined {
  if (!staleReasons?.length || !STALE_CONFLICT_ACTIONS[code]) {
    return undefined;
  }
  const hints = staleReasons
    .map((reason) => STALE_STAGE_HINTS[reason])
    .filter((hint): hint is string => Boolean(hint));
  if (!hints.length) {
    return undefined;
  }
  return `${hints.join("、")}，${staleConflictAction(code, staleReasons)}`;
}

function generationRequestError(error: unknown, errorPrefix: string): Error {
  const { status, code, requestId, staleReasons } = error as RequestError;
  const conflictMessage =
    status === 409 && code
      ? (staleStageConflictMessage(code, staleReasons) ??
        GENERATION_CONFLICT_MESSAGES[code])
      : undefined;
  if (conflictMessage) {
    const mapped = new Error(conflictMessage) as RequestError;
    mapped.status = status;
    mapped.code = code;
    mapped.staleReasons = staleReasons;
    return mapped;
  }
  const archiveMessage =
    status === 409 && code === "RESULT_ARCHIVE_IN_PROGRESS"
      ? "成片正在保存，请稍后刷新任务核对；不会重新生成或扣费。"
      : status === 409 && code === "RESULT_ARCHIVE_LEASE_LOST"
        ? "本次保存已中断，请刷新任务核对后再试；不会重新生成或扣费。"
        : null;
  if (archiveMessage) {
    const mapped = new Error(archiveMessage) as RequestError;
    mapped.status = status;
    mapped.code = code;
    return mapped;
  }
  const referenceMessage =
    status === 422 && code === "INDEPENDENT_REFERENCE_DURATION_INVALID"
      ? "参考视频/音频每段须为2–15秒；缺少时长的历史素材请重新上传后选取。"
      : status === 422 &&
          code === "INDEPENDENT_REFERENCE_DURATION_LIMIT_EXCEEDED"
        ? "参考视频、音频各自累计不能超过15秒，请移除部分素材或裁剪后重试。"
        : null;
  if (referenceMessage) {
    const mapped = new Error(referenceMessage) as RequestError;
    mapped.status = status;
    mapped.code = code;
    return mapped;
  }
  if (status === 422 && error instanceof Error) {
    const message =
      code || error.message !== `${errorPrefix}（422）`
        ? error.message
        : `${errorPrefix}：参数校验未通过，请核对文案、首帧、时长和画幅（HTTP 422）`;
    const diagnostic = [
      code ? `错误代码：${code}` : "",
      requestId && !message.includes(requestId) ? `问题编号：${requestId}` : "",
    ]
      .filter(Boolean)
      .join("；");
    return Object.assign(
      new Error(`${message}${diagnostic ? `。${diagnostic}` : ""}`),
      {
        status,
        code,
        requestId,
      },
    );
  }
  const statusMessage =
    status === 401
      ? "登录已失效，请重新进入工作台"
      : status === 403
        ? "当前账号无权执行此操作"
        : status === 409
          ? `上游内容已变化，请重新确认后再试${
              code ? `（错误代码：${code}）` : ""
            }`
          : status === 429
            ? "请求过于频繁，请稍后重试"
            : status !== undefined && status >= 500
              ? "生成服务暂不可用，请稍后重试"
              : null;
  if (statusMessage) {
    // 未映射的 409 带上错误代码与问题编号：用户不必描述现象，客服可凭编号定位日志。
    const diagnostic =
      status === 409 && requestId && !statusMessage.includes(requestId)
        ? `；问题编号：${requestId}`
        : "";
    const mapped = new Error(`${statusMessage}${diagnostic}`) as RequestError;
    mapped.status = status;
    mapped.code = code;
    mapped.staleReasons = staleReasons;
    return mapped;
  }
  if (error instanceof Error && error.message === "请求超时，请重试") {
    return new Error(`${errorPrefix}：请求超时，请重试`);
  }
  if (error instanceof TypeError) {
    return new Error(`${errorPrefix}：网络连接失败，请检查网络后重试`);
  }
  return error instanceof Error ? error : new Error(errorPrefix);
}

// The server already returns a readable Chinese reason for every provider failure;
// this only fills the gaps where it cannot (request validation, auth, transport),
// so the desktop never shows a bare HTTP status for a failed analysis.
function analysisRequestError(error: unknown, errorPrefix: string): Error {
  const { status, code, retryable } = error as RequestError;
  const fallback =
    status === 422
      ? "参考视频不满足拆解要求，请确认时长在 4–15 秒之间后重试"
      : status === 401
        ? "登录已失效，请重新进入工作台"
        : status === 403
          ? "当前账号无权启动视频拆解"
          : null;
  // A missing code means the server answered with something other than our error
  // envelope (FastAPI request validation, a proxy, an auth redirect).
  if (fallback && code === undefined && error instanceof Error) {
    const mapped = new Error(
      `${errorPrefix}：${fallback}（${status}）`,
    ) as RequestError;
    mapped.status = status;
    mapped.code = code;
    mapped.retryable = retryable;
    return mapped;
  }
  if (error instanceof Error && error.message === "请求超时，请重试") {
    return new Error(`${errorPrefix}：请求超时，请重试`);
  }
  if (error instanceof TypeError) {
    return new Error(`${errorPrefix}：网络连接失败，请检查网络后重试`);
  }
  return error instanceof Error ? error : new Error(errorPrefix);
}

async function responseErrorMessage(
  response: Response,
  errorPrefix: string,
): Promise<string> {
  return (await responseErrorDetails(response, errorPrefix)).message;
}

type RequestError = Error & {
  status?: number;
  code?: string;
  retryable?: boolean;
  requestId?: string;
  staleReasons?: string[];
};

const BRANDED_SERVICE_ERRORS: ReadonlyArray<{
  pattern: RegExp;
  message: string;
}> = [
  {
    pattern: /metaso|minimax|(?:^|[_\W])h3(?:$|[_\W])/i,
    message: "视频生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern:
      /(?:apilio|gemini).*(?:首帧|图像|人物)|(?:首帧|图像|人物).*(?:apilio|gemini)/i,
    message: "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /apilio|gemini/i,
    message: "视频拆解服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /gpt[\s_-]*image|nano[\s_-]*banana/i,
    message: "首帧生成服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /(?:^|[_\W])cos(?:$|[_\W])|腾讯云|myqcloud/i,
    message: "素材库暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /deepseek/i,
    message: "文案优化服务暂时不可用，请稍后重试；如持续失败，请联系客服。",
  },
  {
    pattern: /zpay/i,
    message:
      "在线支付暂时不可用，请稍后重试；如已扣款，请勿重复支付并联系客服。",
  },
];

const CUSTOMER_ACCOUNT_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  RATE_LIMITED: "操作过于频繁，请稍后重试。",
  METASO_REQUIRES_CLOUD_STORAGE:
    "所选素材尚未存入当前云端素材库。请选择已归档的素材，或联系管理员完成云端存储配置后重新上传。",
  ANALYSIS_VIDEO_URL_UNAVAILABLE:
    "当前视频尚未就绪，无法交给云端分析。请联系管理员配置云端素材存储，再重新上传视频。",
  SINGLE_PERSON_SOURCE_REQUIRED:
    "当前参考画面未通过单人检查。请选择只有一位清晰人物的画面，再生成首帧。",
  // ACTIVATION_UNAVAILABLE / PAIRING_UNAVAILABLE 保留：它们属于仍在使用的
  // 激活/配对登录流程（ActivationPage / DevicePairingPage / useCustomerSession），
  // 不在本次「个人中心 UI 残留」清理范围内。
  ACTIVATION_UNAVAILABLE: "该激活码当前无法使用，请确认激活码仍在有效期内。",
  PAIRING_UNAVAILABLE: "该激活码当前无法用于设备配对，请联系服务人员处理。",
  SESSION_CONFLICT: "另一台设备当前正在使用此账号，请稍后重新打开应用。",
  SESSION_EXPIRED: "登录已过期，请重新登录。",
  SESSION_REPLACED: "当前设备已被另一台设备切换下线，请重新登录。",
  DEVICE_REVOKED: "当前设备凭据已失效，请联系服务人员处理。",
  // 子账号月度额度超限（accept_operation 403）：保留业务语义而不是落成泛化的
  // 「无权操作」——额度是配置问题，母账号调整后即可继续，文案要给人下一步。
  SUB_ACCOUNT_QUOTA_EXCEEDED: "本月额度已用完，请联系母账号调整额度后重试。",
};

/**
 * Keep provider identifiers and raw diagnostics intact inside the API and
 * server, while translating only messages that are about to reach a product
 * surface. Non-branded, actionable errors are preserved verbatim.
 */
export function customerVisibleErrorMessage(
  error: unknown,
  fallback = "操作失败，请稍后重试。",
): string {
  let message = "";
  let code = "";
  let requestId = "";

  if (typeof error === "string") {
    message = error.trim();
  } else if (error instanceof Error) {
    message = error.message.trim();
    const details = error as RequestError;
    code = typeof details.code === "string" ? details.code.trim() : "";
    requestId =
      typeof details.requestId === "string" ? details.requestId.trim() : "";
  } else if (isRecord(error)) {
    message = typeof error.message === "string" ? error.message.trim() : "";
    code = typeof error.code === "string" ? error.code.trim() : "";
    requestId =
      typeof error.requestId === "string" ? error.requestId.trim() : "";
  }

  const accountMessage = CUSTOMER_ACCOUNT_ERROR_MESSAGES[code];
  if (accountMessage) {
    return accountMessage;
  }
  if (error instanceof TypeError) {
    return fallback;
  }

  const source = `${code} ${message}`;
  const branded = BRANDED_SERVICE_ERRORS.find(({ pattern }) =>
    pattern.test(source),
  );
  if (!branded) {
    return message || fallback;
  }
  return requestId
    ? `${branded.message} 问题编号：${requestId}`
    : branded.message;
}

async function responseErrorDetails(
  response: Response,
  errorPrefix: string,
): Promise<{
  message: string;
  code?: string;
  retryable?: boolean;
  staleReasons?: string[];
}> {
  try {
    const payload: unknown = await response.json();
    if (
      response.status === 422 &&
      isRecord(payload) &&
      Array.isArray(payload.detail)
    ) {
      const fields: Record<string, string> = {
        text: "口播文案",
        full_text: "口播文案",
        script_text: "口播文案",
        opening_action: "开场衔接",
        ratio: "画幅",
        resolution: "分辨率",
        output_duration_seconds: "视频时长",
        duration_seconds: "视频时长",
        first_frame_asset_id: "已选首帧",
        script_version_id: "已确认文案",
        shot_card_version_id: "分镜版本",
        timeline_policy: "时长压缩确认",
        prompt_text: "提示词",
        source: "文案来源",
        quantity: "生成数量",
      };
      const details = payload.detail
        .filter(isRecord)
        .slice(0, 5)
        .map((item) => {
          const loc = Array.isArray(item.loc) ? item.loc : [];
          const field = [...loc]
            .reverse()
            .find((part) => typeof part === "string" && fields[part]);
          const label = typeof field === "string" ? fields[field] : "提交参数";
          const ctx = isRecord(item.ctx) ? item.ctx : {};
          const limit = (key: string) =>
            typeof ctx[key] === "number" ? ctx[key] : undefined;
          const reason =
            item.type === "missing"
              ? "不能为空，请补充后重试"
              : item.type === "string_too_long" &&
                  limit("max_length") !== undefined
                ? `最多 ${limit("max_length")} 个字符`
                : item.type === "string_too_short" &&
                    limit("min_length") !== undefined
                  ? `至少 ${limit("min_length")} 个字符`
                  : item.type === "greater_than_equal" &&
                      limit("ge") !== undefined
                    ? `不能小于 ${limit("ge")}`
                    : item.type === "less_than_equal" &&
                        limit("le") !== undefined
                      ? `不能大于 ${limit("le")}`
                      : item.type === "literal_error" || item.type === "enum"
                        ? "请选择支持的选项"
                        : item.type === "extra_forbidden"
                          ? "客户端与服务端版本不匹配，请更新后重试"
                          : "格式不正确，请检查后重试";
          // Never echo Pydantic's input/context text: it can contain user content.
          return `${label}：${reason}`;
        });
      if (details.length)
        return {
          message: `${errorPrefix}：${details.join("；")}`,
          code: "VALIDATION_ERROR",
        };
    }
    if (isRecord(payload) && isRecord(payload.detail)) {
      const message = payload.detail.message;
      const code =
        typeof payload.detail.code === "string"
          ? payload.detail.code
          : undefined;
      const retryable =
        typeof payload.detail.retryable === "boolean"
          ? payload.detail.retryable
          : undefined;
      // 服务端在 409 里给出机器可读的 stale_reasons（哪一环变旧），
      // 这一层不能丢，否则上层只能回退到笼统的“上游内容已变化”。
      const staleReasons = Array.isArray(payload.detail.stale_reasons)
        ? payload.detail.stale_reasons.filter(
            (reason): reason is string => typeof reason === "string",
          )
        : undefined;
      // The code must survive even when the server omits a message, otherwise
      // callers cannot tell a retryable failure from a permanent one.
      return {
        message: customerVisibleErrorMessage({
          message:
            typeof message === "string" && message.trim()
              ? `${errorPrefix}：${message}（${response.status}）`
              : `${errorPrefix}（${response.status}）`,
          code,
          requestId: response.headers?.get?.("X-Request-Id") ?? "",
        }),
        code,
        retryable,
        staleReasons,
      };
    }
  } catch {
    // A missing or non-JSON error body must not hide the HTTP status.
  }
  return { message: `${errorPrefix}（${response.status}）` };
}

async function requestAdmin(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  return requestApi(path, init, timeoutMs);
}

async function requestControl(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const csrfAtStart = getAdminCsrfToken();
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (
    init.method &&
    ["POST", "PUT", "PATCH", "DELETE"].includes(init.method.toUpperCase()) &&
    !headers.has("X-Admin-CSRF")
  ) {
    const csrf = getAdminCsrfToken();
    if (csrf) {
      headers.set("X-Admin-CSRF", csrf);
    }
  }
  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers,
      credentials: "include",
      signal: controller.signal,
    });
    if (response.status === 401) {
      notifyAdminSessionExpired(csrfAtStart);
    }
    return response;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("请求超时，请重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function publishBrowserRequest(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  if (!path.startsWith("/api/studio/publish/browser/"))
    throw new Error("发布账号请求地址无效");
  const headers = new Headers(init.headers);
  headers.set("X-Request-Id", crypto.randomUUID());
  if (init.body) headers.set("Content-Type", "application/json");
  const token = workspaceAccessToken();
  const owner = customerSessionOwner;
  if (!token) throw new Error("请先登录工作台");
  headers.set("Authorization", `Bearer ${token}`);
  if (token.startsWith("web-session:")) headers.set("X-Customer-Web", "1");
  // Preserve the caller's signal after response headers: it owns the QR stream.
  const response = await fetch(`${apiBaseUrl()}${path}`, {
    ...init,
    headers,
    cache: "no-store",
  });
  if (response.status === 401) await emitWorkspaceSessionEnded(response, owner);
  if (!response.ok)
    throw new Error(await responseErrorMessage(response, "发布账号请求失败"));
  return response;
}

async function requestApi(
  path: string,
  init: RequestInit,
  timeoutMs: number = REQUEST_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const callerSignal = init.signal;
  const abortFromCaller = () => controller.abort();
  if (callerSignal?.aborted) abortFromCaller();
  else callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const headers = new Headers(init.headers);
  const customerOwnerAtStart = customerSessionOwner;

  if (init.body && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  // CW-015: the development-identity header (X-Dev-User-Id) is removed, so a
  // formal customer request can never carry a synthetic dev identity; auth is
  // the workspace Bearer token only.
  const accessToken = workspaceAccessToken();
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
    if (accessToken.startsWith("web-session:"))
      headers.set("X-Customer-Web", "1");
  }

  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      ...init,
      headers,
      signal: controller.signal,
    });
    if (response.status === 401 && path !== "/api/auth/me") {
      await emitWorkspaceSessionEnded(response, customerOwnerAtStart);
    }
    return response;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      if (callerSignal?.aborted) throw error;
      throw new Error("请求超时，请重试");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

function emitSessionExpired() {
  window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}

/** End only the administrator session; customer credentials have their own lifecycle. */
export function notifyAdminSessionExpired(csrfAtStart: string | null): void {
  // A delayed response must not invalidate a later login or repeat its expiry.
  if (getAdminCsrfToken() !== csrfAtStart) return;
  clearAdminCsrfToken();
  emitSessionExpired();
}

async function emitWorkspaceSessionEnded(
  response: Response,
  ownerAtStart: symbol | null,
) {
  // A request made in the unbound handoff window does not own a session
  // attached later, just as a request from a replaced workspace does not.
  if (ownerAtStart !== customerSessionOwner) {
    return;
  }
  if (customerSessionToken === null) {
    emitSessionExpired();
    return;
  }

  let lifecycle = CUSTOMER_SESSION_EXPIRED_EVENT;
  try {
    const requestId = response.headers.get("X-Request-Id") ?? "";
    const error = await customerErrorFromResponse(response.clone(), requestId);
    lifecycle = customerLifecycleEvent(error.kind) ?? lifecycle;
  } catch {
    // A proxy/non-JSON 401 still ends the customer session, but it must never
    // be guessed as a permanent device revocation (which would wipe the
    // long-lived device credential).
  }
  if (ownerAtStart !== customerSessionOwner) {
    return;
  }
  window.dispatchEvent(new Event(lifecycle));
}

function contentTypeForFile(file: File): "video/mp4" | "video/quicktime" {
  return file.name.toLowerCase().endsWith(".mov")
    ? "video/quicktime"
    : "video/mp4";
}

function materialContentTypeForFile(file: File): string {
  const name = file.name.toLowerCase();
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) return "image/jpeg";
  if (name.endsWith(".png")) return "image/png";
  const audioTypes: Record<string, string> = {
    mp3: "audio/mpeg",
    m4a: "audio/mp4",
    wav: "audio/wav",
    wma: "audio/x-ms-wma",
    wmv: "video/x-ms-wmv",
    aac: "audio/aac",
    flac: "audio/flac",
    ogg: "audio/ogg",
    opus: "audio/ogg",
    aiff: "audio/aiff",
    aif: "audio/aiff",
    amr: "audio/amr",
  };
  const audioType = audioTypes[name.split(".").pop() ?? ""];
  if (audioType) return audioType;
  if (name.endsWith(".mov")) return "video/quicktime";
  if (name.endsWith(".mp4")) return "video/mp4";
  return file.type || "application/octet-stream";
}

async function sha256ForUpload(file: File): Promise<string | null> {
  try {
    if (!globalThis.crypto?.subtle || typeof file.arrayBuffer !== "function") {
      return null;
    }
    const digest = await globalThis.crypto.subtle.digest(
      "SHA-256",
      await file.arrayBuffer(),
    );
    return Array.from(new Uint8Array(digest), (value) =>
      value.toString(16).padStart(2, "0"),
    ).join("");
  } catch {
    // Deduplication is an optimization.  Older WebViews must still be able to
    // upload normally when Web Crypto cannot hash a local File.
    return null;
  }
}

function contentTypeForIdentityFile(file: File): string {
  const name = file.name.toLowerCase();
  if (name.endsWith(".pdf")) {
    return "application/pdf";
  }
  if (name.endsWith(".jpg") || name.endsWith(".jpeg")) {
    return "image/jpeg";
  }
  if (name.endsWith(".png")) {
    return "image/png";
  }
  return file.type || "application/octet-stream";
}

// CW-015: an upload URL is an "API upload" only when it targets the configured
// API origin (the single address source). A direct-to-COS/Provider presigned
// URL has a different origin and must not receive the workspace Bearer token.
// apiBaseUrl() may throw when no address is configured; the catch treats that
// as "not an API upload" so no credential is attached (fail-safe).
function isApiUploadUrl(url: string): boolean {
  try {
    const target = new URL(url);
    return target.origin === new URL(apiBaseUrl()).origin;
  } catch {
    return false;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCurrentUser(value: unknown): value is CurrentUser {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.username === "string" &&
    typeof value.display_name === "string" &&
    (value.role === "employee" ||
      value.role === "admin" ||
      value.role === "auditor" ||
      value.role === "customer")
  );
}

function isAnalysisVersion(value: unknown): value is AnalysisVersion {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.project_id === "string" &&
    typeof value.kind === "string" &&
    typeof value.version_number === "number" &&
    isRecord(value.payload)
  );
}

function isShotMotion(value: unknown): value is ShotMotion {
  return (
    isRecord(value) &&
    typeof value.subject_motion_state === "string" &&
    typeof value.subject_direction === "string" &&
    typeof value.subject_displacement === "string" &&
    typeof value.hand_action === "string" &&
    typeof value.camera_motion === "string" &&
    typeof value.relative_motion === "string"
  );
}

function isShotCard(value: unknown): value is ShotCard {
  if (!isRecord(value)) {
    return false;
  }
  if (
    value.person_count !== undefined &&
    value.person_count !== null &&
    typeof value.person_count !== "number"
  ) {
    return false;
  }
  if (
    value.motion !== undefined &&
    value.motion !== null &&
    !isShotMotion(value.motion)
  ) {
    return false;
  }
  if (
    value.segment_kind !== undefined &&
    value.segment_kind !== null &&
    value.segment_kind !== "SHOT_CUT" &&
    value.segment_kind !== "ACTION_BEAT"
  ) {
    return false;
  }
  if (
    value.boundary_reason !== undefined &&
    value.boundary_reason !== null &&
    typeof value.boundary_reason !== "string"
  ) {
    return false;
  }
  return (
    typeof value.shot_id === "string" &&
    typeof value.start_time === "number" &&
    typeof value.end_time === "number" &&
    typeof value.shot_type === "string" &&
    typeof value.composition === "string" &&
    typeof value.camera_motion === "string" &&
    typeof value.subject === "string" &&
    typeof value.action === "string" &&
    typeof value.scene === "string" &&
    typeof value.spoken_text === "string" &&
    typeof value.transition === "string"
  );
}

// ---------------------------------------------------------------------------
// T28 / FE-01 — customer API lane: the activation / device / session adapter.
//
// Every type below is cut from ./generated/api (the server's OpenAPI
// contract) — hand-written shapes are forbidden here, and the drift guards in
// customerApi.test.ts fail `tsc -b` the moment a regeneration changes a field
// set. Credentials travel as explicit call arguments (dev doc §7: no global
// plaintext variable may simulate a persisted session); the desktop-side
// persistence lands with T29's credential adapter in the Tauri layer.
// ---------------------------------------------------------------------------

type CustomerActivationResponse =
  components["schemas"]["CustomerActivationResponse"];
type CustomerLoginResponse = components["schemas"]["LoginResponse"] & {
  /** CSRF handle returned only when upgrading an existing browser login. */
  device_token?: string;
};
type CustomerHeartbeatResponse = components["schemas"]["HeartbeatResponse"];
export type CustomerDeviceListResponse =
  components["schemas"]["DeviceListResponse"];
type CustomerEnrollPendingResponse =
  components["schemas"]["DeviceEnrollPendingResponse"];
type CustomerEnrollConsumedResponse =
  components["schemas"]["DeviceEnrollConsumedResponse"];
type CustomerPairingApproveResponse =
  components["schemas"]["PairingApproveResponse"];

// Task list §10.1: the single SESSION_EXPIRED_EVENT of the internal lane is
// split into expired / replaced / revoked so the customer workspace can show
// the sentence that matches what actually happened, instead of one generic
// “登录已失效” for every 401.
export const CUSTOMER_SESSION_EXPIRED_EVENT =
  "video-replica:customer-session-expired";
export const CUSTOMER_SESSION_REPLACED_EVENT =
  "video-replica:customer-session-replaced";
export const CUSTOMER_SESSION_REVOKED_EVENT =
  "video-replica:customer-session-revoked";

/** The long-lived device credential returned once at bind time. */
export type CustomerDeviceCredential = { kind: "device"; token: string };
/** The short-lived session token from login / switch. */
export type CustomerSessionCredential = { kind: "session"; token: string };
export type CustomerCredential =
  | CustomerDeviceCredential
  | CustomerSessionCredential;

/** Every 401/403/409/429/idempotency answer resolves to exactly one of
 * these states — the UI never has to parse a raw status line (FE-01). */
export type CustomerApiErrorKind =
  | "session-expired"
  | "session-replaced"
  | "credential-revoked"
  | "credential-invalid"
  | "code-suspended"
  | "code-revoked"
  | "other-device-online"
  | "idempotency-conflict"
  | "rate-limited"
  | "bad-request"
  | "unauthorized"
  | "forbidden"
  | "not-found"
  | "conflict"
  | "service-unavailable"
  | "network"
  | "timeout"
  | "unknown";

export class CustomerApiError extends Error {
  readonly status?: number;
  readonly code?: string;
  readonly retryAfterSeconds?: number;
  /** OTHER_DEVICE_ONLINE extras (dev doc §3.3: masked hint + remaining lease). */
  readonly onlineDeviceNameMasked?: string;
  readonly onlineSlotNo?: number;
  readonly leaseExpiresAt?: string;
  /** The X-Request-Id this client sent (and retains) for the failed request —
   * the correlation key for a server-side audit lookup (dev doc §13.2: an
   * IDEMPOTENCY_CONFLICT must be reported with its request id). */
  readonly requestId?: string;
  /** Transport failures (network / timeout) carry no status or server code —
   * their kind is decided at construction and cannot be derived later. */
  private readonly transportKind?: CustomerApiErrorKind;

  constructor(properties: {
    message: string;
    status?: number;
    code?: string;
    retryAfterSeconds?: number;
    onlineDeviceNameMasked?: string;
    onlineSlotNo?: number;
    leaseExpiresAt?: string;
    requestId?: string;
    transportKind?: CustomerApiErrorKind;
  }) {
    super(properties.message);
    this.name = "CustomerApiError";
    this.status = properties.status;
    this.code = properties.code;
    this.retryAfterSeconds = properties.retryAfterSeconds;
    this.onlineDeviceNameMasked = properties.onlineDeviceNameMasked;
    this.onlineSlotNo = properties.onlineSlotNo;
    this.leaseExpiresAt = properties.leaseExpiresAt;
    this.requestId = properties.requestId;
    this.transportKind = properties.transportKind;
  }

  get kind(): CustomerApiErrorKind {
    return this.transportKind ?? customerErrorKind(this.status, this.code);
  }
}

function customerErrorKind(
  status: number | undefined,
  code: string | undefined,
): CustomerApiErrorKind {
  switch (code) {
    case "SESSION_EXPIRED":
      return "session-expired";
    case "SESSION_REPLACED":
      return "session-replaced";
    case "DEVICE_REVOKED":
      return "credential-revoked";
    case "DEVICE_CREDENTIAL_INVALID":
    case "DEVICE_CREDENTIAL_REQUIRED":
      return "credential-invalid";
    case "CODE_SUSPENDED":
      return "code-suspended";
    case "CODE_REVOKED":
      return "code-revoked";
    case "OTHER_DEVICE_ONLINE":
      return "other-device-online";
    case "IDEMPOTENCY_CONFLICT":
      return "idempotency-conflict";
    case "RATE_LIMITED":
      return "rate-limited";
    case "IDEMPOTENCY_KEY_REQUIRED":
      return "bad-request";
    default:
      break;
  }
  if (status === undefined) {
    return "unknown";
  }
  // No code-suffix matching here: ACTIVATION_UNAVAILABLE / PAIRING_UNAVAILABLE
  // are 400 anti-enumeration rejections (the user must fix the code), not
  // outages. Every true service-interruption code arrives with status 503.
  if (status === 503) {
    return "service-unavailable";
  }
  if (status === 400 || status === 422) {
    return "bad-request";
  }
  if (status === 401) {
    return "unauthorized";
  }
  if (status === 403) {
    return "forbidden";
  }
  if (status === 404) {
    return "not-found";
  }
  if (status === 409) {
    return "conflict";
  }
  return "unknown";
}

/** The lifecycle outcomes that end the customer session for good, mapped to
 * their dedicated events. A mere invalid credential never fires one — that
 * is a caller input problem, not a session the UI must tear down.
 *
 * A suspended code (kind "code-suspended") is deliberately absent: the
 * suspension is reversible (an admin can resume the code), so firing the
 * revoked event would make consumers drop a still-valid device credential —
 * after the resume the user could not log in again without device recovery.
 * The UI surfaces the suspension through the error kind itself (PR #55
 * review); only permanent revocations fire the event. */
function customerLifecycleEvent(
  kind: CustomerApiErrorKind,
): string | undefined {
  if (kind === "session-expired") {
    return CUSTOMER_SESSION_EXPIRED_EVENT;
  }
  if (kind === "session-replaced") {
    return CUSTOMER_SESSION_REPLACED_EVENT;
  }
  if (kind === "credential-revoked" || kind === "code-revoked") {
    return CUSTOMER_SESSION_REVOKED_EVENT;
  }
  return undefined;
}

async function customerErrorFromResponse(
  response: Response,
  requestId: string,
): Promise<CustomerApiError> {
  let code: string | undefined;
  let message = `客户服务请求失败（${response.status}）`;
  let onlineDeviceNameMasked: string | undefined;
  let onlineSlotNo: number | undefined;
  let leaseExpiresAt: string | undefined;
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && isRecord(payload.detail)) {
      const detail = payload.detail;
      if (typeof detail.code === "string") {
        code = detail.code;
      }
      if (typeof detail.message === "string" && detail.message.trim()) {
        message = detail.message;
      }
      if (typeof detail.online_device_name_masked === "string") {
        onlineDeviceNameMasked = detail.online_device_name_masked;
      }
      if (typeof detail.online_slot_no === "number") {
        onlineSlotNo = detail.online_slot_no;
      }
      if (typeof detail.lease_expires_at === "string") {
        leaseExpiresAt = detail.lease_expires_at;
      }
    }
  } catch {
    // A missing or non-JSON error body must not hide the HTTP status.
  }
  const retryAfterHeader = response.headers.get("Retry-After");
  const retryAfterSeconds = retryAfterHeader
    ? Number.parseInt(retryAfterHeader, 10)
    : undefined;
  return new CustomerApiError({
    message: customerVisibleErrorMessage({ message, code, requestId }),
    status: response.status,
    code,
    requestId,
    retryAfterSeconds:
      retryAfterSeconds !== undefined && Number.isNaN(retryAfterSeconds)
        ? undefined
        : retryAfterSeconds,
    onlineDeviceNameMasked,
    onlineSlotNo,
    leaseExpiresAt,
  });
}

type CustomerRequestOptions = {
  browserSession?: boolean;
  method?: string;
  body?: unknown;
  credential?: CustomerCredential;
  idempotencyKey?: string;
  /** Dev doc §13.1: state-changing customer requests carry X-Request-Id.
   * Omit it and the transport mints one (crypto.randomUUID) so every call
   * keeps a correlation key the server audit can be looked up by. */
  requestId?: string;
  /** Optional stale-request gate for lifecycle events. The caller may reject
   * an older response before it can end a newer request in the same session. */
  shouldDispatchLifecycle?: () => boolean;
};

function isReplayed(response: Response): boolean {
  return response.headers.get("X-Idempotent-Replay") === "true";
}

// Cookie response headers are applied by the browser before fetch resolves.
// Serialize changes so an old deletion cannot arrive after a new login's
// Set-Cookie. A failed request releases the queue, allowing an explicit retry.
let browserCookieMutationQueue: Promise<void> = Promise.resolve();

function requestCustomer(
  path: string,
  options: CustomerRequestOptions,
): ReturnType<typeof requestCustomerNow> {
  const browser =
    options.browserSession || options.credential?.token.startsWith("web-");
  const method =
    options.method ?? (options.body !== undefined ? "POST" : "GET");
  const changesCookies =
    browser &&
    ((path === "/api/customer/browser-session" && method === "DELETE") ||
      (method === "POST" &&
        [
          "/api/customer/login",
          "/api/customer/activate",
          "/api/customer/sessions/login",
          "/api/customer/sessions/switch",
        ].includes(path)));
  if (!changesCookies) return requestCustomerNow(path, options);
  const pending = browserCookieMutationQueue.then(() =>
    requestCustomerNow(path, options),
  );
  browserCookieMutationQueue = pending.then(
    () => undefined,
    () => undefined,
  );
  return pending;
}

async function requestCustomerNow(
  path: string,
  options: CustomerRequestOptions,
): Promise<{
  response: Response;
  requestId: string;
  sessionOwnerAtStart: symbol | null;
  sessionTokenAtStart: string | null;
  sessionCredentialMatchedAtStart: boolean;
}> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    REQUEST_TIMEOUT_MS,
  );
  // Dev doc §13.1: every customer request carries an X-Request-Id. The id
  // travels back inside CustomerApiError so the UI can report it on an
  // IDEMPOTENCY_CONFLICT (§13.2) and the audit trail can be located by it.
  const requestId = options.requestId ?? crypto.randomUUID();
  const isSessionRequest = options.credential?.kind === "session";
  const sessionOwnerAtStart = isSessionRequest ? customerSessionOwner : null;
  const sessionTokenAtStart = isSessionRequest ? customerSessionToken : null;
  const sessionCredentialMatchedAtStart =
    !isSessionRequest ||
    sessionTokenAtStart === null ||
    options.credential?.token === sessionTokenAtStart;
  const headers = new Headers();
  headers.set("X-Request-Id", requestId);
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  if (options.credential) {
    headers.set("Authorization", `Bearer ${options.credential.token}`);
  }
  if (options.browserSession || options.credential?.token.startsWith("web-")) {
    headers.set("X-Customer-Web", "1");
  }
  if (options.idempotencyKey) {
    headers.set("Idempotency-Key", options.idempotencyKey);
  }
  try {
    const response = await fetch(`${apiBaseUrl()}${path}`, {
      method: options.method ?? (options.body !== undefined ? "POST" : "GET"),
      headers,
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: controller.signal,
    });
    return {
      response,
      requestId,
      sessionOwnerAtStart,
      sessionTokenAtStart,
      sessionCredentialMatchedAtStart,
    };
  } catch (error) {
    throw customerTransportError(error, requestId);
  } finally {
    window.clearTimeout(timeout);
  }
}

function customerTransportError(
  error: unknown,
  requestId: string,
): CustomerApiError {
  if (error instanceof DOMException && error.name === "AbortError") {
    return new CustomerApiError({
      message: "请求超时，请重试",
      requestId,
      transportKind: "timeout",
    });
  }
  if (error instanceof TypeError) {
    return new CustomerApiError({
      message: "网络连接失败，请检查网络",
      requestId,
      transportKind: "network",
    });
  }
  const message = error instanceof Error ? error.message : "客户服务请求失败";
  return new CustomerApiError({ message, requestId });
}

async function customerJson<T>(
  path: string,
  options: CustomerRequestOptions,
): Promise<{ response: Response; body: T }> {
  const {
    response,
    requestId,
    sessionOwnerAtStart,
    sessionTokenAtStart,
    sessionCredentialMatchedAtStart,
  } = await requestCustomer(path, options);
  if (!response.ok) {
    const error = await customerErrorFromResponse(response, requestId);
    const lifecycle = customerLifecycleEvent(error.kind);
    const belongsToCurrentSession =
      options.credential?.kind !== "session" ||
      (sessionCredentialMatchedAtStart &&
        sessionOwnerAtStart === customerSessionOwner &&
        sessionTokenAtStart === customerSessionToken);
    const isCurrentRequest = options.shouldDispatchLifecycle?.() ?? true;
    if (lifecycle && belongsToCurrentSession && isCurrentRequest) {
      window.dispatchEvent(new Event(lifecycle));
    }
    throw error;
  }
  if (response.status === 204) {
    return { response, body: undefined as T };
  }
  return { response, body: (await response.json()) as T };
}

/**
 * @deprecated 激活码方案已废弃（2026-09-19），改用注册登录 + user_id 绑定。
 * 本类型保留仅为兼容旧代码（useCustomerSession.ts 等），将在后续 PR 中删除。
 */
export type CustomerActivateInput = {
  activationCode: string;
  deviceFingerprint: string;
  deviceName: string;
  devicePlatform: string;
  /** Mandatory (dev doc §6.3): activation carries an idempotency key. */
  idempotencyKey: string;
  /** Optional correlation id; omitted, the transport mints one. */
  requestId?: string;
};

export type CustomerPasswordSession =
  components["schemas"]["CustomerPasswordLoginResponse"];

export async function customerRegister(
  username: string,
  password: string,
): Promise<void> {
  await customerJson("/api/customer/register", {
    method: "POST",
    body: { username, password },
  });
}

export async function customerPasswordLogin(
  input: {
    username: string;
    password: string;
    device_fingerprint: string;
    device_platform: string;
  },
  idempotencyKey: string,
): Promise<CustomerPasswordSession> {
  const { body } = await customerJson<CustomerPasswordSession>(
    "/api/customer/login",
    {
      method: "POST",
      browserSession: input.device_platform === "browser",
      body: input,
      idempotencyKey,
      shouldDispatchLifecycle: () => false,
    },
  );
  return body;
}

/** Browser JS receives only CSRF handles; bearer credentials stay HttpOnly. */
export async function customerBrowserCredentials(): Promise<{
  device_token: string | null;
  session_token: string | null;
}> {
  const { body } = await customerJson<{
    device_token: string | null;
    session_token: string | null;
  }>("/api/customer/browser-session", {
    browserSession: true,
    shouldDispatchLifecycle: () => false,
  });
  return body;
}

export async function clearCustomerBrowserCredentials(
  credential: CustomerCredential,
): Promise<void> {
  await customerJson<undefined>("/api/customer/browser-session", {
    method: "DELETE",
    browserSession: true,
    credential,
    shouldDispatchLifecycle: () => false,
  });
}

/**
 * Redeem an activation code: user + wallet + first device + first charge +
 * first session in one transaction (POST /api/customer/activate).
 *
 * @deprecated 激活码方案已废弃（2026-09-19），改用注册登录 + user_id 绑定。
 * 本函数保留仅为兼容旧代码（useCustomerSession.ts:495），将在后续 PR 中删除。
 */
export async function customerActivate(
  input: CustomerActivateInput,
): Promise<CustomerActivationResponse> {
  const { body } = await customerJson<CustomerActivationResponse>(
    "/api/customer/activate",
    {
      method: "POST",
      browserSession: input.devicePlatform === "browser",
      body: {
        activation_code: input.activationCode,
        device_fingerprint: input.deviceFingerprint,
        device_name: input.deviceName,
        device_platform: input.devicePlatform,
      },
      idempotencyKey: input.idempotencyKey,
      requestId: input.requestId,
    },
  );
  return body;
}

export type CustomerLoginResult = {
  /** 201 established / recovered, 200 renewed (the server sets it). */
  status: 200 | 201;
  /** True when the sealed envelope replayed a lost response. */
  replayed: boolean;
  session: CustomerLoginResponse;
};

export type CustomerLoginOptions = {
  browserSession?: boolean;
  idempotencyKey: string;
  /** Presenting the previous session token renews instead of conflicting. */
  sessionToken?: string;
  /** Optional correlation id; omitted, the transport mints one. */
  requestId?: string;
};

async function customerEstablishSession(
  path: string,
  credential: CustomerDeviceCredential,
  options: CustomerLoginOptions,
): Promise<CustomerLoginResult> {
  const { response, body } = await customerJson<CustomerLoginResponse>(path, {
    method: "POST",
    browserSession: options.browserSession,
    credential,
    body: { session_token: options.sessionToken ?? null },
    idempotencyKey: options.idempotencyKey,
    requestId: options.requestId,
  });
  const status = response.status === 200 ? 200 : 201;
  return { status, replayed: isReplayed(response), session: body };
}

/** Device-credential login (POST /api/customer/sessions/login); 409
 * OTHER_DEVICE_ONLINE carries the masked hint for the conflict dialog. */
export async function customerLogin(
  credential: CustomerDeviceCredential,
  options: CustomerLoginOptions,
): Promise<CustomerLoginResult> {
  return customerEstablishSession(
    "/api/customer/sessions/login",
    credential,
    options,
  );
}

/** The explicit atomic switch (POST /api/customer/sessions/switch): displaces
 * the other device's live lease after the user confirmed the takeover. */
export async function customerSwitch(
  credential: CustomerDeviceCredential,
  options: CustomerLoginOptions,
): Promise<CustomerLoginResult> {
  return customerEstablishSession(
    "/api/customer/sessions/switch",
    credential,
    options,
  );
}

/** Renew the session lease (POST /api/customer/sessions/heartbeat). */
export async function customerHeartbeat(
  credential: CustomerSessionCredential,
  options?: { shouldDispatchLifecycle?: () => boolean },
): Promise<CustomerHeartbeatResponse> {
  const { body } = await customerJson<CustomerHeartbeatResponse>(
    "/api/customer/sessions/heartbeat",
    {
      method: "POST",
      credential,
      shouldDispatchLifecycle: options?.shouldDispatchLifecycle,
    },
  );
  return body;
}

/** End the session (POST /api/customer/sessions/logout → 204). */
export async function customerLogout(
  credential: CustomerSessionCredential,
  options: { idempotencyKey: string; requestId?: string },
): Promise<void> {
  await customerJson<undefined>("/api/customer/sessions/logout", {
    method: "POST",
    credential,
    idempotencyKey: options.idempotencyKey,
    requestId: options.requestId,
  });
}

/** The two-slot status view (GET /api/customer/devices). */
export async function customerListDevices(
  credential: CustomerDeviceCredential,
): Promise<CustomerDeviceListResponse> {
  const { body } = await customerJson<CustomerDeviceListResponse>(
    "/api/customer/devices",
    { credential },
  );
  return body;
}

/** Unbind one of the caller's own devices
 * (DELETE /api/customer/devices/{id} → 204). */
export async function customerUnbindDevice(
  credential: CustomerDeviceCredential,
  deviceId: string,
  options: { idempotencyKey: string; requestId?: string },
): Promise<void> {
  await customerJson<undefined>(
    `/api/customer/devices/${encodeURIComponent(deviceId)}`,
    {
      method: "DELETE",
      credential,
      idempotencyKey: options.idempotencyKey,
      requestId: options.requestId,
    },
  );
}

/**
 * @deprecated 激活码方案已废弃（2026-09-19），改用注册登录 + user_id 绑定。
 * 本类型保留仅为兼容旧代码（DevicePairingPage.tsx / CustomerPairingFlow.tsx），将在后续 PR 中删除。
 */
export type CustomerEnrollInput = {
  activationCode: string;
  deviceFingerprint: string;
  deviceName: string;
  devicePlatform: string;
  idempotencyKey: string;
  /** Optional correlation id; omitted, the transport mints one. */
  requestId?: string;
};

export type CustomerEnrollResult =
  | { status: 202; replayed: boolean; pending: CustomerEnrollPendingResponse }
  | {
      status: 201;
      replayed: boolean;
      credential: CustomerEnrollConsumedResponse;
    };

/**
 * Start (or finish) the second-device pairing
 * (POST /api/customer/devices/enroll): 202 while waiting for the first
 * device's approval, 201 with the one-time device credential once an approved
 * pairing is consumed.
 *
 * @deprecated 激活码方案已废弃（2026-09-19），改用注册登录 + user_id 绑定。
 * 本函数保留仅为兼容旧代码（DevicePairingPage.tsx:44 / CustomerPairingFlow.tsx:135），将在后续 PR 中删除。
 */
export async function customerEnrollDevice(
  input: CustomerEnrollInput,
): Promise<CustomerEnrollResult> {
  const { response, body } = await customerJson<
    CustomerEnrollPendingResponse | CustomerEnrollConsumedResponse
  >("/api/customer/devices/enroll", {
    method: "POST",
    body: {
      activation_code: input.activationCode,
      device_fingerprint: input.deviceFingerprint,
      device_name: input.deviceName,
      device_platform: input.devicePlatform,
    },
    idempotencyKey: input.idempotencyKey,
    requestId: input.requestId,
  });
  const replayed = isReplayed(response);
  if (response.status === 202) {
    return {
      status: 202,
      replayed,
      pending: body as CustomerEnrollPendingResponse,
    };
  }
  return {
    status: 201,
    replayed,
    credential: body as CustomerEnrollConsumedResponse,
  };
}

/** The first bound device approves a PENDING pairing request
 * (POST /api/customer/device-pairings/{id}/approve). */
export async function customerApproveDevicePairing(
  credential: CustomerDeviceCredential,
  pairingId: string,
): Promise<CustomerPairingApproveResponse> {
  const { body } = await customerJson<CustomerPairingApproveResponse>(
    `/api/customer/device-pairings/${encodeURIComponent(pairingId)}/approve`,
    { method: "POST", credential },
  );
  return body;
}

/** Remove an invalid pending/approved pairing request from the customer's
 * active list while the server preserves its audit row. */
export async function customerDismissDevicePairing(
  credential: CustomerDeviceCredential,
  pairingId: string,
): Promise<void> {
  await customerJson<undefined>(
    `/api/customer/device-pairings/${encodeURIComponent(pairingId)}`,
    { method: "DELETE", credential },
  );
}

// CustomerActivationCodeReset / customerResetActivationCode 已删除（激活码方案废弃，2026-09-19）

/** The customer's wallet balance + billing (GET /api/customer/wallet). */
export async function customerGetWallet(
  credential: CustomerSessionCredential,
): Promise<WalletSnapshot> {
  const { body } = await customerJson<OpenApiWalletSnapshot>(
    "/api/customer/wallet",
    { credential },
  );
  return requireWalletPricing(body);
}

function requireWalletPricing(wallet: OpenApiWalletSnapshot): WalletSnapshot {
  if (
    typeof wallet.internal_unit_price_fen !== "number" ||
    wallet.internal_unit_price_fen <= 0 ||
    typeof wallet.min_recharge_fen !== "number" ||
    wallet.min_recharge_fen <= 0 ||
    typeof wallet.recharge_step_fen !== "number" ||
    wallet.recharge_step_fen <= 0
  ) {
    throw new Error("钱包定价配置不完整，请联系管理员");
  }
  return wallet as WalletSnapshot;
}

export type CustomerProfile = components["schemas"]["CustomerProfileResponse"];

export type CustomerPasswordState = {
  user_id: string;
  username: string;
  has_password: boolean;
};
export async function customerPasswordState(
  credential: CustomerSessionCredential,
): Promise<CustomerPasswordState> {
  return (
    await customerJson<CustomerPasswordState>(
      "/api/customer/account/password",
      { credential },
    )
  ).body;
}
export async function customerSetInitialPassword(
  credential: CustomerSessionCredential,
  input: { username: string; password: string },
  idempotencyKey: string,
): Promise<CustomerPasswordState> {
  return (
    await customerJson<CustomerPasswordState>(
      "/api/customer/account/password",
      { credential, method: "POST", body: input, idempotencyKey },
    )
  ).body;
}

export async function customerGetProfile(
  credential: CustomerSessionCredential,
  options?: { shouldDispatchLifecycle?: () => boolean },
): Promise<CustomerProfile> {
  const { body } = await customerJson<CustomerProfile>(
    "/api/customer/profile",
    {
      credential,
      shouldDispatchLifecycle: options?.shouldDispatchLifecycle,
    },
  );
  return body;
}

export async function customerUpdateProfile(
  credential: CustomerSessionCredential,
  displayName: string,
): Promise<CustomerProfile> {
  const { body } = await customerJson<CustomerProfile>(
    "/api/customer/profile",
    {
      method: "PATCH",
      credential,
      body: { display_name: displayName },
    },
  );
  return body;
}

/** CW-062 客户自助子账号（/api/customer/sub-accounts）：后端返回 dict，
 * 契约里是 unknown，故在此手写响应形状（与 admin lane 的先例一致）。 */
export type CustomerSubAccount = {
  id: string;
  username: string;
  display_name: string;
  account_type: "SUB" | "SUB_ADMIN";
  parent_user_id: string;
  is_active: boolean;
  has_password: boolean;
  created_at: string;
  updated_at: string | null;
  /** Phase 3a 月度额度：null = 不限（无额度行）；单位与钱包余额一致（积分）。 */
  monthly_quota_credits: number | null;
  /** 当月已用积分（-SUM(available_delta)，在途预扣计入）。 */
  quota_used_credits: number;
  /** 剩余额度（钳制到 0）；无额度行时为 null。 */
  quota_remaining_credits: number | null;
};

export async function customerListSubAccounts(
  credential: CustomerSessionCredential,
): Promise<CustomerSubAccount[]> {
  const { body } = await customerJson<{
    sub_accounts: CustomerSubAccount[];
    total_count: number;
  }>("/api/customer/sub-accounts", { credential });
  return body.sub_accounts;
}

export async function customerCreateSubAccount(
  credential: CustomerSessionCredential,
  input: {
    username: string;
    display_name: string;
    password?: string;
    /** Phase 3a：可选的初始月度额度；缺省 = 不限。 */
    monthly_quota_credits?: number;
  },
): Promise<CustomerSubAccount> {
  const { body } = await customerJson<CustomerSubAccount>(
    "/api/customer/sub-accounts",
    { method: "POST", credential, body: input },
  );
  return body;
}

export async function customerUpdateSubAccount(
  credential: CustomerSessionCredential,
  subAccountId: string,
  input: { display_name?: string; is_active?: boolean },
): Promise<CustomerSubAccount> {
  const { body } = await customerJson<CustomerSubAccount>(
    `/api/customer/sub-accounts/${encodeURIComponent(subAccountId)}`,
    { method: "PATCH", credential, body: input },
  );
  return body;
}

export async function customerSetSubAccountPassword(
  credential: CustomerSessionCredential,
  subAccountId: string,
  password: string,
): Promise<{ id: string; has_password: boolean }> {
  const { body } = await customerJson<{ id: string; has_password: boolean }>(
    `/api/customer/sub-accounts/${encodeURIComponent(subAccountId)}/password`,
    { method: "POST", credential, body: { password } },
  );
  return body;
}

/** Phase 3a：设置（数字）或清除（null）子账号的月度积分上限。 */
export async function customerSetSubAccountQuota(
  credential: CustomerSessionCredential,
  subAccountId: string,
  monthlyQuotaCredits: number | null,
): Promise<CustomerSubAccount> {
  const { body } = await customerJson<CustomerSubAccount>(
    `/api/customer/sub-accounts/${encodeURIComponent(subAccountId)}/quota`,
    {
      method: "PUT",
      credential,
      body: { monthly_quota_credits: monthlyQuotaCredits },
    },
  );
  return body;
}

export async function customerDeleteSubAccount(
  credential: CustomerSessionCredential,
  subAccountId: string,
): Promise<{ id: string; deleted: boolean; is_active: boolean }> {
  const { body } = await customerJson<{
    id: string;
    deleted: boolean;
    is_active: boolean;
  }>(`/api/customer/sub-accounts/${encodeURIComponent(subAccountId)}`, {
    method: "DELETE",
    credential,
  });
  return body;
}

/** The customer's wallet transaction ledger (GET /api/customer/wallet/transactions). */
export async function customerListWalletTransactions(
  credential: CustomerSessionCredential,
  {
    limit = 20,
    offset = 0,
    filters = {},
  }: {
    limit?: number;
    offset?: number;
    filters?: Record<string, string>;
  } = {},
): Promise<WalletTransactionPage> {
  const { body } = await customerJson<WalletTransactionPage>(
    `/api/customer/wallet/transactions?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
      ...filters,
    })}`,
    { credential },
  );
  return body;
}

/** The customer's own recharge orders (GET /api/customer/recharge-orders). */
export async function customerListRechargeOrders(
  credential: CustomerSessionCredential,
  {
    limit = 20,
    offset = 0,
  }: {
    limit?: number;
    offset?: number;
  } = {},
): Promise<RechargeOrderPage> {
  const { body } = await customerJson<RechargeOrderPage>(
    `/api/customer/recharge-orders?${new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })}`,
    { credential },
  );
  return body;
}

/** 客户可见的充值套餐档位（GET /api/customer/recharge-packages，仅启用行）。 */
export type CustomerRechargePackage = {
  id: string;
  name: string;
  amount_fen: number;
  credits: number;
  /** 折扣率 4 位小数字符串（如 "0.9000"）；null = 无权益档位。 */
  discount_rate: string | null;
  discount_interfaces: string[];
  sort_order: number;
  is_active: boolean;
  version: number;
  created_at: string | null;
  updated_at: string | null;
};

/** 管理员配置的充值套餐（客户只读）。 */
export async function customerListRechargePackages(
  credential: CustomerSessionCredential,
): Promise<CustomerRechargePackage[]> {
  const { body } = await customerJson<{ items?: CustomerRechargePackage[] }>(
    "/api/customer/recharge-packages",
    { credential },
  );
  // 契约异常（缺 items）时按空列表处理：充值页不能因档位列表而整页崩。
  return Array.isArray(body?.items) ? body.items : [];
}

/** Create a customer recharge order
 * (POST /api/customer/recharge-orders → 201 PENDING + ZPay payment form).
 *
 * 传 `packageId` 时为套餐下单：amount_fen 必须等于套餐金额，到账积分与权益
 * 以套餐为准并冻结进订单快照。不传时是自定义金额（基础汇率、无权益）。 */
export async function customerCreateRechargeOrder(
  credential: CustomerSessionCredential,
  amountFen: number,
  options: { idempotencyKey: string; packageId?: string; requestId?: string },
): Promise<CreatedRechargeOrder> {
  const payload: { amount_fen: number; package_id?: string } = {
    amount_fen: amountFen,
  };
  if (options.packageId !== undefined) {
    payload.package_id = options.packageId;
  }
  const { body } = await customerJson<CreatedRechargeOrder>(
    "/api/customer/recharge-orders",
    {
      method: "POST",
      credential,
      body: payload,
      idempotencyKey: options.idempotencyKey,
      requestId: options.requestId,
    },
  );
  return body;
}

export type CustomerPaymentCode =
  components["schemas"]["CustomerPaymentCodeResponse"];

/** Generate a display-only QR code for an owned pending recharge order.
 * Merchant credentials and signed protocol fields remain on the server. */
export async function customerCreateRechargePaymentCode(
  credential: CustomerSessionCredential,
  orderNo: string,
): Promise<CustomerPaymentCode> {
  const { body } = await customerJson<CustomerPaymentCode>(
    `/api/customer/recharge-orders/${encodeURIComponent(orderNo)}/payment-code`,
    { method: "POST", credential },
  );
  return body;
}

/** Poll a customer recharge order
 * (GET /api/customer/recharge-orders/{order_no}). */
export async function customerGetRechargeOrder(
  credential: CustomerSessionCredential,
  orderNo: string,
): Promise<RechargeOrder> {
  const { body } = await customerJson<RechargeOrder>(
    `/api/customer/recharge-orders/${encodeURIComponent(orderNo)}`,
    { credential },
  );
  return body;
}

/** Close an unpaid recharge order. The server keeps the CLOSED row for
 * callback reconciliation and audit instead of physically deleting it. */
export async function customerCloseRechargeOrder(
  credential: CustomerSessionCredential,
  orderNo: string,
): Promise<void> {
  await customerJson<undefined>(
    `/api/customer/recharge-orders/${encodeURIComponent(orderNo)}`,
    { method: "DELETE", credential },
  );
}

// ---------------------------------------------------------------------------
// 爆款视频（C4 重启）：抖音 / 视频号最近 7 天爆款参考库
// ---------------------------------------------------------------------------

const VIRAL_LIST_TIMEOUT_MS = 120_000;
const VIRAL_MEDIA_TIMEOUT_MS = 120_000;
const VIRAL_STATISTICS_TIMEOUT_MS = 150_000;

export type ViralPlatform = "douyin" | "wechat_channels" | "xiaohongshu";
export type ViralSort = "hot" | "latest";

export type ViralVideoItem = {
  homepageFeatured?: boolean;
  platform: ViralPlatform;
  videoId: string;
  category: string;
  title: string;
  sourceDescription?: string | null;
  author: string;
  authorAvatar: string | null;
  verified: boolean;
  coverUrl: string | null;
  durationMs: number;
  likes: number;
  comments: number | null;
  shares: number | null;
  collects: number | null;
  publishedAt: number | null;
  publishedDisplay: string | null;
  likeDisplay: string | null;
  tags: string[];
  hasPlayableAudio: boolean;
  /** 源平台播放地址；真实列表播放统一由服务端媒体管线转存后使用。 */
  playUrl: string | null;
  isFavorite?: boolean;
  availability?: "available" | "unavailable" | "hidden";
};

export type ViralListResponse = {
  platform: ViralPlatform;
  sort: ViralSort;
  categories: string[];
  items: ViralVideoItem[];
  fetchedAt: string | null;
  dataVersion?: string | null;
  source?: "database";
  stale?: boolean;
  refreshing?: boolean;
  refreshError?: string | null;
  hasMore?: boolean;
  nextCursor?: string | null;
  total?: number;
};

export type ViralFavoritesResponse = {
  items: ViralVideoItem[];
  total: number;
  hasMore?: boolean;
  nextCursor?: string | null;
};

export type ViralFavoriteResponse = {
  isFavorite: boolean;
};

export type ViralDetailResponse = { item: ViralVideoItem } | ViralVideoItem;

export type ViralListOptions = {
  featuredOnly?: boolean;
  limit?: number;
  cursor?: string;
};

export type ViralMediaResponse = {
  kind: "audio" | "video";
  url: string;
  contentType: string;
  cacheHit: boolean;
  video?: ViralVideoItem | null;
};

export type ViralStatisticsResponse = {
  items: ViralVideoItem[];
};

export type ViralImportPurpose = "copy" | "replica";
export type ViralLinkResolution = {
  item: ViralVideoItem;
  importIdempotencyKey: string;
};
export type ViralImportTask = {
  id?: string;
  taskId?: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED";
  projectId?: string | null;
  sourceAssetId?: string | null;
  mediaKind?: "audio" | "video" | null;
  canTranscribe?: boolean;
  canAnalyze?: boolean;
  error?: string | null;
  errorMessage?: string | null;
  message?: string | null;
  retryable?: boolean;
};

/** 最近 7 天爆款列表（服务端按分类关键词聚合，带计费护栏缓存）。 */
export function listViralVideos(
  platform: ViralPlatform,
  sort: ViralSort = "hot",
  options: ViralListOptions = {},
): Promise<ViralListResponse> {
  const query = new URLSearchParams({ platform, sort });
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  if (options.featuredOnly) query.set("featured_only", "true");
  // 页面仅读取数据库中的已归档媒体，不触发上游采集。
  return requestApiJson<ViralListResponse>(
    `/api/viral/videos?${query}`,
    "爆款视频列表暂不可用",
    {},
    VIRAL_LIST_TIMEOUT_MS,
  );
}

/** 从数据库读取单条爆款视频，用于详情页刷新恢复。 */
export function fetchViralVideo(
  platform: ViralPlatform,
  videoId: string,
): Promise<ViralDetailResponse> {
  return requestApiJson<ViralDetailResponse>(
    `/api/viral/videos/${encodeURIComponent(platform)}/${encodeURIComponent(videoId)}`,
    "视频详情暂不可用",
  );
}

export function listViralFavorites(
  options: ViralListOptions & { platform?: ViralPlatform } = {},
): Promise<ViralFavoritesResponse> {
  const query = new URLSearchParams();
  if (options.platform) query.set("platform", options.platform);
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const suffix = query.size ? `?${query}` : "";
  return requestApiJson<ViralFavoritesResponse>(
    `/api/viral/favorites${suffix}`,
    "收藏列表暂不可用",
  );
}

async function updateViralFavorite(
  platform: ViralPlatform,
  videoId: string,
  method: "PUT" | "DELETE",
): Promise<ViralFavoriteResponse> {
  const response = await requestApi(
    `/api/viral/favorites/${encodeURIComponent(platform)}/${encodeURIComponent(videoId)}`,
    { method },
  );
  if (!response.ok) {
    const details = await responseErrorDetails(response, "更新收藏失败");
    const error = new Error(details.message) as RequestError;
    error.status = response.status;
    error.code = details.code;
    throw error;
  }
  if (response.status === 204) return { isFavorite: method === "PUT" };
  try {
    return (await response.json()) as ViralFavoriteResponse;
  } catch {
    return { isFavorite: method === "PUT" };
  }
}

export function saveViralFavorite(
  platform: ViralPlatform,
  videoId: string,
): Promise<ViralFavoriteResponse> {
  return updateViralFavorite(platform, videoId, "PUT");
}

export function removeViralFavorite(
  platform: ViralPlatform,
  videoId: string,
): Promise<ViralFavoriteResponse> {
  return updateViralFavorite(platform, videoId, "DELETE");
}

export function createViralImportTask(
  platform: ViralPlatform,
  videoId: string,
  purpose: ViralImportPurpose,
  idempotencyKey: string,
): Promise<ViralImportTask> {
  return requestApiJson<ViralImportTask>(
    "/api/viral/videos/import-tasks",
    "导入爆款视频失败",
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ platform, videoId, purpose }),
    },
    VIRAL_MEDIA_TIMEOUT_MS,
  );
}

export function resolveViralLink(
  url: string,
  purpose: ViralImportPurpose,
  idempotencyKey: string,
): Promise<ViralLinkResolution> {
  return requestApiJson<ViralLinkResolution>(
    "/api/viral/link-resolutions",
    "视频链接解析失败",
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ url, purpose }),
    },
    60_000,
  );
}

// ---------------------------------------------------------------------------
// AI 优化提示词：提示词框右上角的小图标。任意文本 → MiniMax H3 官方结构。
// 同步付费调用（按次计费）；Idempotency-Key 由调用方每次点击生成。
// ---------------------------------------------------------------------------

export type H3Mode = "T2VA" | "I2VA" | "FL2VA" | "L2VA" | "Ref2VA";
export type PromptGenerationContext = {
  route: "text_image" | "reference" | "replica";
  duration_seconds: number;
  ratio?: GenerationRatio;
  project_id?: string | null;
  analysis_version_id?: string | null;
  shot_card_version_id?: string | null;
  script_version_id?: string | null;
  source_asset_id?: string | null;
  first_frame_asset_id?: string | null;
  last_frame_asset_id?: string | null;
  references?: { asset_id: string; purpose: string }[];
  instructions?: string;
};
export type PromptOptimizeInput = PromptGenerationContext & {
  idempotency_key: string;
  editor_revision: number;
  prompt_text: string;
};
export type PromptOptimizeResult = {
  task_id: string;
  status:
    | "PENDING"
    | "RUNNING"
    | "SUCCEEDED"
    | "NEEDS_INPUT"
    | "FAILED"
    | "SUBMISSION_UNCERTAIN";
  mode: H3Mode;
  editor_revision: number;
  context_hash: string;
  formatter_version: string;
  error_message?: string | null;
  result?: {
    prompt_text: string | null;
    warnings: { code: string; message: string }[];
    validation_status: string;
  } | null;
};
export function createPromptOptimization(
  input: PromptOptimizeInput,
): Promise<PromptOptimizeResult> {
  return requestApiJson<PromptOptimizeResult>(
    "/api/prompt-optimizations",
    "启动提示词优化失败",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}
export function getPromptOptimization(
  id: string,
): Promise<PromptOptimizeResult> {
  return requestApiJson<PromptOptimizeResult>(
    `/api/prompt-optimizations/${encodeURIComponent(id)}`,
    "查询提示词优化失败",
  );
}
/** Keep authentication material private; callers only get a session equality check. */
export function capturePromptSession(): () => boolean {
  const token = workspaceAccessToken();
  return () => workspaceAccessToken() === token;
}

export function getViralImportTask(taskId: string): Promise<ViralImportTask> {
  return requestApiJson<ViralImportTask>(
    `/api/viral/import-tasks/${encodeURIComponent(taskId)}`,
    "读取导入任务失败",
  );
}

/** 按需取媒体：缺省音频优先；kind=video 时取低清视频（播放用）。 */
export function fetchViralVideoMedia(
  platform: ViralPlatform,
  videoId: string,
  kind?: "audio" | "video",
): Promise<ViralMediaResponse> {
  return requestApiJson<ViralMediaResponse>(
    "/api/viral/videos/media",
    "视频素材准备失败",
    {
      method: "POST",
      body: JSON.stringify(
        kind ? { platform, videoId, kind } : { platform, videoId },
      ),
    },
    VIRAL_MEDIA_TIMEOUT_MS,
  );
}

export function refreshViralVideoStatistics(
  videoIds: string[],
  key: string,
): Promise<ViralStatisticsResponse> {
  return requestApiJson<ViralStatisticsResponse>(
    "/api/viral/videos/statistics/refresh",
    "刷新视频统计失败",
    {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({ videoIds }),
    },
    VIRAL_STATISTICS_TIMEOUT_MS,
  );
}

/** 按需补齐视频号互动统计；服务端负责缓存与失败退避。 */
export function fetchViralVideoStatistics(
  videoIds: string[],
): Promise<ViralStatisticsResponse> {
  return requestApiJson<ViralStatisticsResponse>(
    "/api/viral/videos/statistics",
    "视频统计暂时无法更新",
    {
      method: "POST",
      body: JSON.stringify({ videoIds }),
    },
    VIRAL_STATISTICS_TIMEOUT_MS,
  );
}

// ---------------------------------------------------------------------------
// C5 发布模块第一阶段（legacy）：手工粘贴 Cookie 的平台发布账号
// （/api/studio/publish/accounts）。第二阶段的正式投递以扫码账号
// （/publish/browser/accounts）与发布记录（/publish/records，见下文）为准。
// ---------------------------------------------------------------------------

export type PublishAccountItem = {
  id: string;
  platform: "douyin" | "wechat_channels";
  display_name: string;
  status: "connected" | "invalid";
  last_verified_at: string | null;
  error_message: string | null;
  security_sdk_required: boolean;
  created_at: string;
};

export async function listPublishAccounts(): Promise<PublishAccountItem[]> {
  return requestApiJson<{ accounts: PublishAccountItem[] }>(
    "/api/studio/publish/accounts",
    "读取发布账号失败",
  ).then((payload) => payload.accounts);
}

export async function createPublishAccount(input: {
  platform: PublishAccountItem["platform"];
  display_name: string;
  cookie: string;
  security_sdk?: string;
}): Promise<PublishAccountItem> {
  return requestApiJson<PublishAccountItem>(
    "/api/studio/publish/accounts",
    "连接发布账号失败",
    {
      method: "POST",
      body: JSON.stringify(input),
    },
  );
}

export async function deletePublishAccount(accountId: string): Promise<void> {
  await requestApiJson<{ deleted: boolean }>(
    `/api/studio/publish/accounts/${encodeURIComponent(accountId)}`,
    "删除发布账号失败",
    { method: "DELETE" },
  );
}

export async function verifyPublishAccount(accountId: string): Promise<void> {
  await requestApiJson<{ submitted: boolean }>(
    `/api/studio/publish/accounts/${encodeURIComponent(accountId)}/verify`,
    "发起登录态校验失败",
    { method: "POST" },
  );
}

export type CustomerApiKey = {
  id: string;
  token_group_id: string;
  credential_version: number;
  key_prefix: string;
  label: string;
  scopes: string[];
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
  is_default: boolean;
  total_consumed_credits: number;
};

export type CustomerCreditConfig = {
  video_768p?: number | null;
  video_2k?: number | null;
  oral?: number | null;
  points_per_yuan: number;
  discount_basis_points?: number;
  consumption_rounding?: "ceil" | "floor";
};
export type CustomerPricing = {
  version: number;
  configured: boolean;
  config: CustomerCreditConfig | null;
  prices: Array<{
    subject: string;
    name: string;
    specification: string;
    unit: string;
    unit_credits: number;
    configurable: boolean;
  }>;
  recharge_rounding: string;
};

export function getWorkspacePricing(): Promise<CustomerPricing> {
  return requestApiJson<CustomerPricing>(
    "/api/customer/pricing",
    "读取功能价格失败",
  );
}

export async function customerGetPricing(
  credential: CustomerSessionCredential,
): Promise<CustomerPricing> {
  return (
    await customerJson<CustomerPricing>("/api/customer/pricing", { credential })
  ).body;
}
export type CreatedCustomerApiKey = CustomerApiKey & {
  plaintext: string | null;
};
export type CustomerCenterSummary = {
  user_id: string;
  available_credits: number;
  reserved_credits: number;
  total_consumed_credits: number;
  active_tokens: number;
};
export async function customerGetCenterSummary(
  credential: CustomerSessionCredential,
): Promise<CustomerCenterSummary> {
  return (
    await customerJson<CustomerCenterSummary>("/api/customer/center-summary", {
      credential,
    })
  ).body;
}
export async function customerListApiKeys(
  credential: CustomerSessionCredential,
): Promise<{ items: CustomerApiKey[]; total: number }> {
  return (
    await customerJson<{ items: CustomerApiKey[]; total: number }>(
      "/api/customer/api-keys",
      { credential },
    )
  ).body;
}
export async function customerInitializeDefaultApiKey(
  credential: CustomerSessionCredential,
  idempotencyKey: string,
): Promise<CreatedCustomerApiKey> {
  return (
    await customerJson<CreatedCustomerApiKey>(
      "/api/customer/api-keys/default",
      { credential, method: "POST", body: {}, idempotencyKey },
    )
  ).body;
}
export async function customerCreateApiKey(
  credential: CustomerSessionCredential,
  label: string,
  idempotencyKey: string,
): Promise<CreatedCustomerApiKey> {
  return (
    await customerJson<CreatedCustomerApiKey>("/api/customer/api-keys", {
      credential,
      method: "POST",
      body: { label },
      idempotencyKey,
    })
  ).body;
}
export async function customerRotateApiKey(
  credential: CustomerSessionCredential,
  id: string,
  idempotencyKey: string,
): Promise<CreatedCustomerApiKey> {
  return (
    await customerJson<CreatedCustomerApiKey>(
      `/api/customer/api-keys/${encodeURIComponent(id)}/rotate`,
      { credential, method: "POST", body: {}, idempotencyKey },
    )
  ).body;
}
export async function customerRevokeApiKey(
  credential: CustomerSessionCredential,
  id: string,
): Promise<void> {
  await customerJson<undefined>(
    `/api/customer/api-keys/${encodeURIComponent(id)}`,
    { credential, method: "DELETE" },
  );
}

// ---------------------------------------------------------------------------
// PUBLISH-DELIVERY-20260917 第二阶段：发布记录（/api/studio/publish/records）
// 立即/定时投递、状态机、汇总；账号引用扫码账号（publish_browser_accounts）。
// ---------------------------------------------------------------------------

export type PublishRecordStatus =
  | "queued"
  | "publishing"
  | "published"
  | "failed"
  | "cancelled";

export type PublishRecordItem = {
  id: string;
  platform: "douyin" | "wechat_channels" | "xiaohongshu";
  account_id: string | null;
  account_username: string | null;
  video_asset_id: string;
  cover_asset_id: string | null;
  title: string;
  description: string;
  tags: string[];
  scheduled_at: string | null;
  status: PublishRecordStatus;
  delivery_mode: "api" | "browser" | null;
  platform_item_id: string | null;
  platform_short_url: string | null;
  platform_status: string | null;
  stats: Record<string, unknown> | null;
  stats_synced_at: string | null;
  sync_requested: boolean;
  error_message: string | null;
  published_at: string | null;
  attempt_count: number;
  created_at: string;
  updated_at: string;
};

export type PublishSummary = {
  published_total: number;
  queued_total: number;
  failed_total: number;
  play_total: number;
  like_total: number;
};

export type PublishRecordCreateInput = {
  account_id: string;
  video_material_id: string;
  cover_material_id?: string | null;
  title: string;
  description: string;
  tags: string[];
  scheduled_at?: string | null;
  options?: Record<string, unknown>;
};

const PUBLISH_RECORDS_BASE = "/api/studio/publish/records";

export async function listPublishRecords(
  filters: {
    status?: PublishRecordStatus;
    platform?: string;
    limit?: number;
  } = {},
): Promise<PublishRecordItem[]> {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.platform) params.set("platform", filters.platform);
  if (filters.limit) params.set("limit", String(filters.limit));
  const query = params.toString();
  return requestApiJson<{ records: PublishRecordItem[] }>(
    query ? `${PUBLISH_RECORDS_BASE}?${query}` : PUBLISH_RECORDS_BASE,
    "读取发布记录失败",
  ).then((payload) => payload.records);
}

export async function createPublishRecord(
  input: PublishRecordCreateInput,
): Promise<PublishRecordItem> {
  return requestApiJson<PublishRecordItem>(
    PUBLISH_RECORDS_BASE,
    "提交发布失败",
    { method: "POST", body: JSON.stringify(input) },
  );
}

async function publishRecordAction(
  recordId: string,
  action: "cancel" | "retry" | "sync",
  errorPrefix: string,
): Promise<PublishRecordItem> {
  return requestApiJson<{ record: PublishRecordItem }>(
    `${PUBLISH_RECORDS_BASE}/${encodeURIComponent(recordId)}/${action}`,
    errorPrefix,
    { method: "POST" },
  ).then((payload) => payload.record);
}

export const cancelPublishRecord = (recordId: string) =>
  publishRecordAction(recordId, "cancel", "取消发布失败");
export const retryPublishRecord = (recordId: string) =>
  publishRecordAction(recordId, "retry", "重试发布失败");
export const syncPublishRecord = (recordId: string) =>
  publishRecordAction(recordId, "sync", "发起数据同步失败");

export async function deletePublishRecord(recordId: string): Promise<void> {
  await requestApiJson<{ deleted: boolean }>(
    `${PUBLISH_RECORDS_BASE}/${encodeURIComponent(recordId)}`,
    "删除发布记录失败",
    { method: "DELETE" },
  );
}

export async function getPublishSummary(): Promise<PublishSummary> {
  return requestApiJson<PublishSummary>(
    `${PUBLISH_RECORDS_BASE}/summary`,
    "读取发布统计失败",
  );
}
