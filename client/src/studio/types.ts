import type {
  CurrentUser,
  IndependentCapabilities,
  Project,
  SavedPromptItem,
  StudioAnalytics,
} from "../api";

export type StudioPage =
  | "workbench"
  | "viral"
  | "viral-detail"
  | "copy"
  | "replica"
  | "replacement"
  | "video"
  | "reference"
  | "oral"
  | "oral-audio"
  | "tasks"
  | "task-detail"
  | "people"
  | "person-ip"
  | "person-photos"
  | "person-avatars"
  | "person-voices"
  | "materials"
  | "publishing"
  | "analytics"
  | "settings"
  | "profile";
export type AssetKind = "image" | "video" | "audio";
export type StudioAsset = {
  id: string;
  materialId?: string;
  assetId?: string;
  generationTaskId?: string;
  name: string;
  kind: AssetKind;
  url?: string;
  poster?: string;
  duration?: string;
  /** 数值时长（秒）：供参考选取/上传路径做 ≤15s 拦截比较；未知时为 undefined。 */
  durationSeconds?: number;
  group: string;
  personId?: string;
  /** MATERIAL-UX-03：归属对象显示名与创建/大小信息（供卡片 meta 与详情三行）。 */
  personName?: string;
  projectId?: string;
  projectTitle?: string;
  createdAt?: string;
  sizeBytes?: number;
  /** MATERIAL-UX-05：用户侧标签（详情可编辑，卡片展示前 2 个）。 */
  tags?: string[];
  /** MATERIAL-UX-08：宽高与比例（存量素材可能缺省，前端优雅降级）。 */
  width?: number;
  height?: number;
  aspectRatio?: number;
  /** MATERIAL-UX-07：音频用途三态（oral_audio / voice_clone / reference）。 */
  audioPurpose?: "oral_audio" | "voice_clone" | "reference";
  composite?: boolean;
  previewAssetId?: string;
  characterViews?: { assetId: string; viewType: string }[];
  contactSheetId?: string;
  contactSheetUrl?: string;
  source: string;
  saved: boolean;
  delivery?: "stored" | "direct";
  allowedUses?: string[];
  allowedActions?: string[];
};
export type StudioAvatar = {
  id: string;
  name: string;
  imageId: string;
  ready: boolean;
  status?: "PENDING" | "RUNNING" | "READY" | "FAILED";
  submissionState?:
    | "LOCAL_PENDING"
    | "SUBMITTING"
    | "SUBMITTED"
    | "SUBMISSION_UNKNOWN"
    | "FAILED";
  error?: string;
  origin: "视频制作" | "照片制作";
  duration: string;
};
export type StudioVoice = {
  id: string;
  name: string;
  confirmed: boolean;
  isDefault?: boolean;
  status?: "PENDING" | "RUNNING" | "READY" | "FAILED";
  submissionState?:
    | "LOCAL_PENDING"
    | "SUBMITTING"
    | "SUBMITTED"
    | "SUBMISSION_UNKNOWN"
    | "FAILED";
  error?: string;
  url?: string;
  /** 试听样本资产 id，供「下载试听」复用签名下载链路。 */
  demoAssetId?: string;
};
export type StudioPerson = {
  id: string;
  name: string;
  role: string;
  portrait?: string;
  version: number;
  scope: string;
  audience: string;
  expression: string;
  audience_needs?: string;
  factual_background?: string;
  sample_script?: string;
  forbidden_claims?: string;

  sheetId?: string;
  sceneLookCount: number;
  photoIds: string[];
  photoCount?: number;
  avatars: StudioAvatar[];
  voices: StudioVoice[];
};
export type StudioVideo = {
  homepageFeatured?: boolean;
  id: string;
  title: string;
  author: string;
  platform: "抖音" | "视频号" | "小红书";
  category: string;
  poster: string;
  videoUrl?: string;
  duration: string;
  likes: number;
  collections: number | null;
  shares: number | null;
  description: string;
  /** C4 重启：爆款数据源规范化字段（列表/详情按平台展示，审核样例可缺省）。 */
  platformKey?: "douyin" | "wechat_channels" | "xiaohongshu";
  nativeId?: string;
  authorAvatar?: string | null;
  verified?: boolean;
  comments?: number | null;
  publishedAt?: number | null;
  publishedDisplay?: string | null;
  likeDisplay?: string | null;
  tags?: string[];
  hasPlayableAudio?: boolean;
  playUrl?: string | null;
};
export type StudioTask = {
  draftSnapshot?: StudioDraft;
  id: string;
  backendKind?: "generation_batch" | "oral_task";
  backendId?: string;
  backendStatus?: string;
  billingStatus?: string;
  retryAction?: "archive-retry";
  title: string;
  type: "视频复刻" | "人物置换" | "视频生成" | "数字人口播";
  status:
    | "running"
    | "queued"
    | "failed"
    | "completed"
    | "uncertain"
    | "cancelled";
  progress?: number;
  submitted: string;
  poster?: string;
  resultId?: string;
  batchId?: string;
  /** 独立创作批次无项目归属（null）。 */
  projectId?: string | null;
  driverMode?: "text" | "audio";
  ipId?: string;
  avatarId?: string;
  voiceId?: string;
  audioId?: string;
  scriptVersion?: number;
  /** 服务端记录的实际口播正文，用于基于历史任务创建新稿。 */
  scriptText?: string;
};
export type StudioStats = {
  today_completed: number;
  running: number;
  queued: number;
  needs_attention: number;
  total_completed: number;
};
export type StudioData = {
  people: StudioPerson[];
  assets: StudioAsset[];
  /** 素材库图片（视频生成页首帧/尾帧/参考素材的素材库选择来源）。 */
  materials: StudioAsset[];
  videos: StudioVideo[];
  /** 独立的人工精选集合，普通榜单/解析结果不能替换它。 */
  homepageVideos?: StudioVideo[];
  tasks: StudioTask[];
  projects: Project[];
  errors: string[];
  loading: boolean;
  /** 平台侧真实统计（/api/studio/stats）；加载失败或审核模式为 null。 */
  stats: StudioStats | null;
  /** 平台侧真实成片聚合（/api/studio/analytics，C6 数据看板），7/30 天双窗口；
   * 加载失败为 null，看板页回退"尚未就绪"空态。 */
  analytics7: StudioAnalytics | null;
  analytics30: StudioAnalytics | null;
  pagination?: {
    people?: { nextCursor: string | null; total: number };
    scenes?: Record<string, { loaded: number; total: number }>;
    generationTasks?: { nextCursor: string | null; total: number };
    oralTasks?: { loaded: number; total: number };
  };
};
export type StudioScript = {
  resultKind?: "extracted" | "rewritten" | "manual";
  rewriteTaskId?: string;
  id: string;
  title: string;
  original: string;
  text: string;
  version: number;
  confirmed: boolean;
  ipId?: string;
  sourceProjectId?: string;
  sourceKind?: "viral" | "project" | "link" | "upload";
};
export type StudioDraft = {
  /** 独立视频生成批次的用户可见名称。 */
  videoName?: string;
  pendingRewrite?: {
    scopeKey: string;
    resultText: string;
    taskId?: string;
    requestKey?: string;
    startedAt?: number;
  };
  rewriteCandidate?: { scopeKey: string; text: string };
  rewriteMethod?: "ip" | "custom";
  rewriteInstructions?: string;
  rewriteLength?: "original" | "100" | "200" | "300" | "custom";
  rewriteWordCount?: number;
  id: string;
  ipId?: string;
  sourceId?: string;
  /** 上传来源视频的资产 id：提取文案（script-from-audio）管线输入。 */
  sourceAssetId?: string;
  analysisTaskId?: string;
  analysisTaskStatus?: string;
  projectId?: string;
  selectedShotId: string;
  originalImageId?: string;
  imageId?: string;
  firstFrameId?: string;
  /** 已确认源画面，用于判断是否需要填写中段帧开场衔接。 */
  sourceFrameSelectionVersionId?: string;
  sourceFrameTimestampSeconds?: number;
  /** 人物置换流程交接的已确认首帧版本。 */
  firstFrameSelectionVersionId?: string;
  tailFrameId?: string;
  avatarId?: string;
  voiceId?: string;
  audioId?: string;
  script: StudioScript;
  /** 当前脚本是否包含尚未发布为项目版本的本地编辑，包括主动清空。 */
  scriptEdited?: boolean;
  prompt: string;
  /** 复刻准备保留拆解依据，最终提示词独立存于 prompt。 */
  replicaSourcePrompt?: string;
  /** 新提示词对应的项目、来源、文案与拆解内容，供交接前校验。 */
  replicaPromptBasis?: string;
  /** 内容准备变更后，须显式交接最新提示词与采用首帧。 */
  replicaPreparationPending?: boolean;
  /** 已合成终稿的最小指纹（不含完整版本对象）：重进页面时据此判断 finalReady，
   * 否则按钮会退回"待合成"，上游其实没有任何变化。 */
  finalSnapshot?: {
    inputKey: string;
    versionId: string;
    scriptVersionId: string;
    shotCardVersionId: string;
  };
  /** 当前 Prompt 是否包含尚未保存为项目版本的本地编辑，包括主动清空。 */
  promptEdited?: boolean;
  referenceIds: string[];
  referencePurposes?: Record<string, string>;
  promptBindingsStale?: boolean;
  importedPromptContext?: SavedPromptItem["generation_context"];
  resolution: string;
  ratio: string;
  duration: number;
  count: number;
  frameConfirmed: boolean;
  style: "standard";
  subtitles: boolean;
  quoteRevision: number;
  /** 最近一次独立创作提交的批次 id：预览区就地展示生成进度。 */
  videoBatchId?: string;
};
export type PickerKind =
  | "person"
  | "image"
  | "original-frame"
  | "first-frame"
  | "tail-frame"
  | "reference"
  | "avatar"
  | "voice"
  | "audio"
  | "voice-audio"
  | "avatar-photo";
export type StudioPublishDraft = {
  id: string;
  assetId: string;
  coverId?: string;
  platform: "抖音" | "视频号" | "小红书";
  /** Server-side publish account id (publish_browser_accounts); "" when unset. */
  account: string;
  title: string;
  description: string;
  tags: string[];
  /** ISO timestamp for a scheduled delivery; absent = publish immediately. */
  scheduledAt?: string;
};
export type StudioPublishAccount = {
  id: string;
  platform: "douyin" | "wechat_channels";
  displayName: string;
  status: "connected" | "invalid";
  lastVerifiedAt: string | null;
  errorMessage: string | null;
  securitySdkRequired: boolean;
  createdAt: string;
};
export type StudioState = {
  page: StudioPage;
  draft: StudioDraft;
  selectedVideoId?: string;
  selectedTaskId?: string;
  selectedTaskKind?: "generation_batch" | "oral_task";
  selectedTaskBackendId?: string;
  selectedAssetId?: string;
  returnTo?: StudioPage;
  selectedPersonId?: string;
  savedScripts: StudioScript[];
  favorites: string[];
  publishDrafts?: StudioPublishDraft[];
  /**
   * C5 第一阶段：云端发布账号（正式模式由档案页「发布账号」tab 自行加载）。
   * 发布记录 publishRecords 属第二阶段，本轮不引入。
   */
  publishAccounts?: StudioPublishAccount[];
};
export type LivePanel =
  | "projects"
  | "characters"
  | "tasks"
  | "profile"
  | "wallet"
  | "analysis";
export type StudioContextValue = {
  requireLogin?: () => void;
  state: StudioState;
  data: StudioData;
  review: boolean;
  user: CurrentUser;
  videoCapabilities?: IndependentCapabilities;
  videoCapabilitiesStatus?: "loading" | "ready" | "error";
  retryVideoCapabilities?: () => void;
  referenceAssetsPending?: boolean;
  referenceAssetsError?: boolean;
  retryReferenceAssets?: () => void;
  draftSaveStatus?: "idle" | "dirty" | "saving" | "saved" | "error";
  navigate: (page: StudioPage, patch?: Partial<StudioState>) => void;
  patchDraft: (
    patch: Partial<StudioDraft>,
    options?: { persistNow?: boolean },
  ) => void;
  patchState: (patch: Partial<StudioState>) => void;
  updateData: (update: (data: StudioData) => StudioData) => void;
  notify: (message: string) => void;
  openPicker: (kind: PickerKind) => void;
  openLive: (
    panel: LivePanel,
    character?: { identityId: string; tab: "base" | "scenes" },
  ) => void;
  requestGeneration: (kind: StudioTask["type"]) => void;
  saveDraft: () => void;
  /** 放弃云端存着的「上次内容」（含本地未保存文本）。重来入口必须调它，
   * 只清内存的话下次打开软件旧内容又会被恢复回来。 */
  discardSavedDraft: () => void;
  /** 确认终稿：云端保存成功后置 confirmed；项目已有分镜时再同步项目脚本。 */
  confirmFinalDraft: () => void;
  /** 上传来源视频 → 提取文案（script-from-audio）→ 回填草稿并跳文案工坊。 */
  extractScriptFromUpload: (projectId?: string, assetId?: string) => void;
  refresh: () => void;
};
