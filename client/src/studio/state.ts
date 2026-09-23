import type { GenerationRatio, ShotCard } from "./../api";
import type {
  StudioAsset,
  StudioDraft,
  StudioPage,
  StudioScript,
  StudioState,
  StudioTask,
} from "./types";

export function hasCopyResult(script: StudioScript): boolean {
  if (script.resultKind === "extracted") return false;
  if (script.resultKind === "rewritten" || script.resultKind === "manual")
    return true;
  return (
    script.confirmed ||
    Boolean(script.text.trim() && script.text.trim() !== script.original.trim())
  );
}

export const pageTitles: Record<StudioPage, string> = {
  workbench: "工作台",
  viral: "爆款视频",
  "viral-detail": "爆款视频详情",
  copy: "文案工坊",
  replica: "视频复刻",
  replacement: "人物置换",
  video: "视频生成 · 文/图生视频",
  reference: "视频生成 · 参考生视频",
  oral: "数字人口播",
  "oral-audio": "数字人口播",
  tasks: "任务中心",
  "task-detail": "任务详情与结果",
  people: "人物库",
  "person-ip": "人物详情",
  "person-photos": "人物详情",
  "person-avatars": "人物详情",
  "person-voices": "人物详情",
  materials: "素材库",
  publishing: "发布管理",
  analytics: "数据看板",
  settings: "系统设置",
  profile: "用户档案",
};

export function routeFromHash(hash: string): StudioPage {
  return studioRouteFromHash(hash).page;
}

const selectableStateKeys = [
  "selectedVideoId",
  "selectedTaskId",
  "selectedTaskKind",
  "selectedTaskBackendId",
  "selectedAssetId",
  "selectedPersonId",
  "returnTo",
] as const;

function decodeRouteId(value: string) {
  try {
    return decodeURIComponent(value);
  } catch {
    return "";
  }
}

export function studioRouteFromHash(hash: string): Pick<
  StudioState,
  (typeof selectableStateKeys)[number]
> & {
  page: StudioPage;
} {
  const value = hash.replace(/^#/, "");
  const [path, query = ""] = value.split("?", 2);
  const raw = path.replace(/^studio\//, "");
  const aliases: Record<string, StudioPage> = {
    projects: "replica",
    characters: "people",
    wallet: "profile",
    "oral-audio": "oral",
  };
  const params = new URLSearchParams(query);
  const result: ReturnType<typeof studioRouteFromHash> = {
    page: Object.hasOwn(aliases, raw)
      ? aliases[raw]
      : Object.hasOwn(pageTitles, raw)
        ? (raw as StudioPage)
        : "workbench",
    selectedVideoId: undefined,
    selectedTaskId: undefined,
    selectedTaskKind: undefined,
    selectedTaskBackendId: undefined,
    selectedAssetId: undefined,
    selectedPersonId: undefined,
    returnTo: undefined,
  };
  const taskMatch = raw.match(
    /^task-detail\/(generation_batch|oral_task)\/([^/]+)$/,
  );
  if (taskMatch) {
    const kind = taskMatch[1] as "generation_batch" | "oral_task";
    const backendId = decodeRouteId(taskMatch[2] ?? "");
    result.page = "task-detail";
    result.selectedTaskKind = kind;
    result.selectedTaskBackendId = backendId;
    result.selectedTaskId =
      kind === "oral_task" ? `oral-${backendId}` : backendId;
  }
  result.selectedVideoId = params.get("video") ?? undefined;
  result.selectedAssetId = params.get("asset") ?? undefined;
  result.selectedPersonId = params.get("person") ?? undefined;
  const returnTo = params.get("returnTo");
  if (returnTo && Object.hasOwn(pageTitles, returnTo)) {
    result.returnTo = returnTo as StudioPage;
  }
  return result;
}

export function studioHashForState(state: StudioState): string {
  const params = new URLSearchParams();
  if (state.selectedVideoId && state.page === "viral-detail")
    params.set("video", state.selectedVideoId);
  if (state.selectedAssetId && state.page === "materials")
    params.set("asset", state.selectedAssetId);
  if (state.selectedPersonId && state.page.startsWith("person-"))
    params.set("person", state.selectedPersonId);
  if (state.returnTo) params.set("returnTo", state.returnTo);
  const path =
    state.page === "task-detail" &&
    state.selectedTaskKind &&
    state.selectedTaskBackendId
      ? `task-detail/${state.selectedTaskKind}/${encodeURIComponent(state.selectedTaskBackendId)}`
      : state.page;
  const query = params.toString();
  return `#studio/${path}${query ? `?${query}` : ""}`;
}

export function navigateStudioState(
  state: StudioState,
  page: StudioPage,
  patch: Partial<StudioState> = {},
): StudioState {
  const keepTaskContext =
    page === "task-detail" || patch.returnTo === "task-detail";
  const keepPersonContext = page.startsWith("person-");
  return {
    ...state,
    selectedVideoId: undefined,
    selectedTaskId: keepTaskContext ? state.selectedTaskId : undefined,
    selectedTaskKind: keepTaskContext ? state.selectedTaskKind : undefined,
    selectedTaskBackendId: keepTaskContext
      ? state.selectedTaskBackendId
      : undefined,
    selectedAssetId: undefined,
    selectedPersonId: keepPersonContext ? state.selectedPersonId : undefined,
    returnTo: undefined,
    ...patch,
    page,
  };
}

export function createDraft(): StudioDraft {
  return {
    id: `draft-${crypto.randomUUID()}`,
    videoName: "未命名视频",
    selectedShotId: "shot-2",
    script: {
      id: `script-${crypto.randomUUID()}`,
      title: "",
      original: "",
      text: "",
      version: 1,
      confirmed: false,
    },
    scriptEdited: false,
    prompt: "",
    promptEdited: false,
    referenceIds: [],
    resolution: "768P",
    ratio: "9:16",
    duration: 8,
    count: 1,
    frameConfirmed: false,
    style: "standard",
    subtitles: false,
    quoteRevision: 0,
  };
}

export function createState(page: StudioPage = "workbench"): StudioState {
  return { page, draft: createDraft(), savedScripts: [], favorites: [] };
}

export function withImportedProject(
  state: StudioState,
  imported: StudioDraft,
): StudioState {
  if (imported.projectId && imported.projectId === state.draft.projectId) {
    const current = state.draft.script;
    const newer =
      !state.draft.scriptEdited &&
      imported.script.text &&
      (imported.script.id !== current.id ||
        imported.script.version > current.version);
    return {
      ...state,
      draft: {
        ...state.draft,
        sourceId: imported.sourceId,
        script: newer ? imported.script : current,
      },
    };
  }
  return {
    ...state,
    draft: imported,
    selectedVideoId: undefined,
    selectedTaskId: undefined,
    selectedTaskKind: undefined,
    selectedTaskBackendId: undefined,
    selectedAssetId: undefined,
    selectedPersonId: undefined,
    returnTo: undefined,
  };
}

export function draftFromTask(task: StudioTask): StudioDraft {
  const fresh = createDraft();
  const snapshot = task.draftSnapshot;
  const ipId = task.ipId ?? snapshot?.ipId;
  return {
    ...fresh,
    ...snapshot,
    id: fresh.id,
    projectId: task.projectId ?? snapshot?.projectId,
    ipId,
    avatarId: task.avatarId ?? snapshot?.avatarId,
    voiceId:
      task.driverMode === "audio"
        ? undefined
        : (task.voiceId ?? snapshot?.voiceId),
    audioId: task.audioId ?? snapshot?.audioId,
    script: {
      ...(snapshot?.script ?? fresh.script),
      id: fresh.script.id,
      title: task.title,
      text: task.scriptText ?? snapshot?.script.text ?? "",
      ipId,
      version: 1,
      confirmed: false,
    },
    scriptEdited: true,
    quoteRevision: 0,
  };
}

export function patchStudioDraft(
  draft: StudioDraft,
  patch: Partial<StudioDraft>,
): StudioDraft {
  const sourceChanged =
    Object.hasOwn(patch, "sourceId") && patch.sourceId !== draft.sourceId;
  const projectChanged =
    Object.hasOwn(patch, "projectId") && patch.projectId !== draft.projectId;
  const identityChanged =
    sourceChanged ||
    projectChanged ||
    (Object.hasOwn(patch, "ipId") && patch.ipId !== draft.ipId) ||
    (Object.hasOwn(patch, "imageId") && patch.imageId !== draft.imageId);
  const firstFrameChanged =
    Object.hasOwn(patch, "firstFrameId") &&
    patch.firstFrameId !== draft.firstFrameId;
  const next = {
    ...draft,
    ...patch,
    id: draft.id,
    quoteRevision: draft.quoteRevision + 1,
  };
  const bindingChanged =
    firstFrameChanged ||
    (Object.hasOwn(patch, "tailFrameId") &&
      patch.tailFrameId !== draft.tailFrameId) ||
    (patch.referenceIds !== undefined &&
      JSON.stringify(patch.referenceIds) !==
        JSON.stringify(draft.referenceIds));
  if (bindingChanged && /<(Picture|Video|Audio)\s+\d+>|@\d+/.test(draft.prompt))
    next.promptBindingsStale = true;
  if (Object.hasOwn(patch, "prompt") && patch.promptBindingsStale === undefined)
    next.promptBindingsStale = false;
  if (
    sourceChanged ||
    projectChanged ||
    (Object.hasOwn(patch, "ipId") && patch.ipId !== draft.ipId) ||
    (
      [
        "rewriteMethod",
        "rewriteInstructions",
        "rewriteLength",
        "rewriteWordCount",
      ] as const
    ).some((key) => Object.hasOwn(patch, key) && patch[key] !== draft[key])
  ) {
    next.pendingRewrite = undefined;
    next.rewriteCandidate = undefined;
  }
  if (sourceChanged) {
    if (!Object.hasOwn(patch, "projectId")) next.projectId = undefined;
    if (!Object.hasOwn(patch, "sourceAssetId")) next.sourceAssetId = undefined;
    if (!Object.hasOwn(patch, "originalImageId"))
      next.originalImageId = undefined;
    if (!Object.hasOwn(patch, "tailFrameId")) next.tailFrameId = undefined;
    if (!Object.hasOwn(patch, "audioId")) next.audioId = undefined;
    if (!Object.hasOwn(patch, "referenceIds")) next.referenceIds = [];
  }
  if (projectChanged || sourceChanged) {
    const blank = createDraft();
    if (!Object.hasOwn(patch, "replicaSourcePrompt"))
      next.replicaSourcePrompt = undefined;
    if (!Object.hasOwn(patch, "replicaPromptBasis"))
      next.replicaPromptBasis = undefined;
    if (!Object.hasOwn(patch, "replicaPreparationPending"))
      next.replicaPreparationPending = undefined;
    // 终稿指纹属于上一个项目/来源，换项目后必然对不上，留着只会随草稿一起复活。
    if (!Object.hasOwn(patch, "finalSnapshot")) next.finalSnapshot = undefined;
    if (!Object.hasOwn(patch, "prompt")) next.prompt = "";
    if (!Object.hasOwn(patch, "promptEdited")) next.promptEdited = false;
    if (!patch.script) next.script = blank.script;
    if (!Object.hasOwn(patch, "scriptEdited")) next.scriptEdited = false;
    next.script = { ...next.script, confirmed: false };
  }
  // Asset ownership must be reselected when identity changes, never relabelled.
  if (Object.hasOwn(patch, "ipId") && patch.ipId !== draft.ipId) {
    if (!Object.hasOwn(patch, "imageId")) next.imageId = undefined;
    next.voiceId = undefined;
    next.avatarId = undefined;
    next.script = { ...next.script, confirmed: false };
  }
  if (identityChanged || firstFrameChanged) {
    if (!Object.hasOwn(patch, "firstFrameId")) next.firstFrameId = undefined;
    if (!Object.hasOwn(patch, "firstFrameSelectionVersionId"))
      next.firstFrameSelectionVersionId = undefined;
    if (!Object.hasOwn(patch, "frameConfirmed")) next.frameConfirmed = false;
  }
  if (patch.script && patch.script.text !== draft.script.text)
    next.script = { ...patch.script, confirmed: false };
  if (
    patch.script &&
    !projectChanged &&
    !sourceChanged &&
    patch.scriptEdited === undefined &&
    (patch.script.title !== draft.script.title ||
      patch.script.original !== draft.script.original ||
      patch.script.text !== draft.script.text)
  )
    next.scriptEdited = true;
  if (
    Object.hasOwn(patch, "prompt") &&
    !projectChanged &&
    !sourceChanged &&
    patch.promptEdited === undefined &&
    patch.prompt !== draft.prompt
  )
    next.promptEdited = true;
  if (
    !Object.hasOwn(patch, "replicaPreparationPending") &&
    next.replicaSourcePrompt !== undefined &&
    ((Object.hasOwn(patch, "replicaSourcePrompt") &&
      patch.replicaSourcePrompt !== draft.replicaSourcePrompt) ||
      (patch.script && patch.script.text !== draft.script.text))
  )
    next.replicaPreparationPending = true;
  return next;
}

export function buildOralInput(draft: StudioDraft, mode: "text" | "audio") {
  if (mode === "text" && (!draft.script.confirmed || !draft.script.text.trim()))
    throw new Error("请先在文案工坊确认终稿");
  if (!draft.ipId || !draft.avatarId)
    throw new Error("请选择人物与可用口播分身");
  if (mode === "audio") {
    if (!draft.audioId) throw new Error("请选择完整口播音频");
    return {
      draftId: draft.id,
      mode,
      ipId: draft.ipId,
      avatarId: draft.avatarId,
      audioAssetId: draft.audioId,
    };
  }
  if (!draft.voiceId) throw new Error("请选择已确认声音");
  return {
    draftId: draft.id,
    mode,
    ipId: draft.ipId,
    avatarId: draft.avatarId,
    voiceId: draft.voiceId,
    scriptId: draft.script.id,
    scriptVersion: draft.script.version,
    style: "standard",
    subtitles: draft.subtitles,
  };
}

// ---- C2 独立创作（视频生成页）----

/** 把新读取的素材并入列表：保留已有签名地址，避免同一次会话内重复取地址。 */
export function mergeStudioAssets(
  current: StudioAsset[],
  incoming: StudioAsset[],
): StudioAsset[] {
  const merged = new Map(current.map((asset) => [asset.id, asset]));
  for (const asset of incoming) {
    const existing = merged.get(asset.id);
    merged.set(asset.id, {
      ...existing,
      ...asset,
      url: existing?.url ?? asset.url,
    });
  }
  return [...merged.values()];
}

export const SUPPORTED_VIDEO_RATIOS = [
  "adaptive",
  "21:9",
  "16:9",
  "4:3",
  "1:1",
  "3:4",
  "9:16",
] as const;

export const DEFAULT_MAX_REFERENCE_IMAGES = 8;
export const DEFAULT_MAX_REFERENCE_VIDEOS = 3;
export const DEFAULT_MAX_REFERENCE_AUDIOS = 3;

/** R2V 参考视频/音频时长上限（秒）：上传前前端探测拦截，选取时对已知时长拦截。 */
export const MAX_REFERENCE_MEDIA_SECONDS = 15;
export const MAX_REFERENCE_FILES = 12;

export type ReferenceLimits = {
  maxReferenceImages?: number;
  maxReferenceVideos?: number;
  maxReferenceAudios?: number;
};

export function isReferenceAsset(asset: StudioAsset): boolean {
  return (
    asset.delivery !== "direct" &&
    (asset.allowedUses === undefined || asset.allowedUses.includes("reference"))
  );
}

/**
 * R2V 参考素材统一混合列表校验：一个 referenceIds 里可混合图片/视频/音频，
 * 按资产 kind 分流后各自计数、各自套用上限（默认图 8 / 视频 3 / 音频 3），
 * 去重、剔除失效引用，并按选择顺序裁剪出可提交的整理结果。与后端
 * independent.py 的 _validate_reference_kind_limits 保持一致的每类上限语义。
 */
export function validateReferences(
  referenceIds: string[],
  availableAssets: StudioAsset[],
  limits: ReferenceLimits = {},
) {
  const assetById = new Map(availableAssets.map((asset) => [asset.id, asset]));
  const seen = new Set<string>();
  const assets: StudioAsset[] = [];
  const images: StudioAsset[] = [];
  const videos: StudioAsset[] = [];
  const audios: StudioAsset[] = [];
  const overDurationMedia: StudioAsset[] = [];
  let invalidCount = 0;
  let duplicateCount = 0;

  for (const id of referenceIds) {
    if (seen.has(id)) {
      duplicateCount += 1;
      continue;
    }
    seen.add(id);
    const asset = assetById.get(id);
    if (!asset || !isReferenceAsset(asset)) {
      invalidCount += 1;
      continue;
    }
    assets.push(asset);
    if (asset.kind === "image") images.push(asset);
    else if (asset.kind === "video") videos.push(asset);
    else audios.push(asset);
    // 视频/音频参考时长超过上限即视为问题素材；时长未知（undefined）放行。
    if (
      asset.kind !== "image" &&
      asset.durationSeconds !== undefined &&
      asset.durationSeconds > MAX_REFERENCE_MEDIA_SECONDS
    ) {
      overDurationMedia.push(asset);
    }
  }

  const imageLimit = Math.max(
    0,
    Math.floor(limits.maxReferenceImages ?? DEFAULT_MAX_REFERENCE_IMAGES),
  );
  const videoLimit = Math.max(
    0,
    Math.floor(limits.maxReferenceVideos ?? DEFAULT_MAX_REFERENCE_VIDEOS),
  );
  const audioLimit = Math.max(
    0,
    Math.floor(limits.maxReferenceAudios ?? DEFAULT_MAX_REFERENCE_AUDIOS),
  );

  const imageOverCount = Math.max(0, images.length - imageLimit);
  const videoOverCount = Math.max(0, videos.length - videoLimit);
  const audioOverCount = Math.max(0, audios.length - audioLimit);
  const overLimitCount = imageOverCount + videoOverCount + audioOverCount;

  const issues: string[] = [];
  if (assets.length > MAX_REFERENCE_FILES)
    issues.push(`参考素材合计最多 ${MAX_REFERENCE_FILES} 项，请移除部分素材。`);
  if (invalidCount > 0)
    issues.push(
      `参考素材仅支持图片、视频或音频，旧草稿中有 ${invalidCount} 项无效素材。`,
    );
  if (duplicateCount > 0)
    issues.push(
      `参考素材不能重复选择，旧草稿中有 ${duplicateCount} 项重复素材。`,
    );
  if (imageOverCount > 0)
    issues.push(
      `当前最多选择 ${imageLimit} 张参考图，旧草稿已超出 ${imageOverCount} 张。`,
    );
  if (videoOverCount > 0)
    issues.push(
      `当前最多选择 ${videoLimit} 个参考视频，旧草稿已超出 ${videoOverCount} 个。`,
    );
  if (audioOverCount > 0)
    issues.push(
      `当前最多选择 ${audioLimit} 个参考音频，旧草稿已超出 ${audioOverCount} 个。`,
    );
  const overDurationCount = overDurationMedia.length;
  if (overDurationCount > 0)
    issues.push(
      `参考视频/音频时长不能超过 ${MAX_REFERENCE_MEDIA_SECONDS} 秒，旧草稿中有 ${overDurationCount} 项超时素材。`,
    );
  for (const [label, media] of [
    ["视频", videos],
    ["音频", audios],
  ] as const) {
    const seconds = media.reduce(
      (total, asset) => total + (asset.durationSeconds ?? 0),
      0,
    );
    if (seconds > MAX_REFERENCE_MEDIA_SECONDS)
      issues.push(
        `参考${label}累计时长不能超过 15 秒，请移除部分素材或裁剪后重试。`,
      );
  }

  // 整理：按选择顺序保留，每类裁剪到各自上限；超时素材一并移除。
  const overDurationIds = new Set(overDurationMedia.map((asset) => asset.id));
  let keptImages = 0;
  let keptVideos = 0;
  let keptAudios = 0;
  let videoSeconds = 0;
  let audioSeconds = 0;
  const repairIds: string[] = [];
  for (const asset of assets) {
    if (repairIds.length >= MAX_REFERENCE_FILES) break;
    if (overDurationIds.has(asset.id)) continue;
    if (asset.kind === "image") {
      if (keptImages < imageLimit) {
        keptImages += 1;
        repairIds.push(asset.id);
      }
    } else if (asset.kind === "video") {
      const seconds = asset.durationSeconds ?? 0;
      if (
        keptVideos < videoLimit &&
        videoSeconds + seconds <= MAX_REFERENCE_MEDIA_SECONDS
      ) {
        keptVideos += 1;
        videoSeconds += seconds;
        repairIds.push(asset.id);
      }
    } else if (
      keptAudios < audioLimit &&
      audioSeconds + (asset.durationSeconds ?? 0) <= MAX_REFERENCE_MEDIA_SECONDS
    ) {
      keptAudios += 1;
      audioSeconds += asset.durationSeconds ?? 0;
      repairIds.push(asset.id);
    }
  }

  return {
    assets,
    images,
    videos,
    audios,
    referenceIds: assets.map((asset) => asset.id),
    imageIds: images.map((asset) => asset.id),
    videoIds: videos.map((asset) => asset.id),
    audioIds: audios.map((asset) => asset.id),
    imageCount: images.length,
    videoCount: videos.length,
    audioCount: audios.length,
    imageLimit,
    videoLimit,
    audioLimit,
    invalidCount,
    duplicateCount,
    overLimitCount,
    overDurationCount,
    issues,
    repairIds,
  };
}

/** 文图生页签有首帧即 I2V（可选尾帧），无首帧为 T2V；参考生页签为 R2V。 */
export function resolveVideoMode(
  page: StudioPage,
  hasFirstFrame = false,
  hasLastFrame = false,
): "t2v" | "i2v" | "l2v" | "r2v" {
  if (page === "reference") return "r2v";
  return hasFirstFrame ? "i2v" : hasLastFrame ? "l2v" : "t2v";
}

/**
 * 按生成模式算出真正提交给后端的画面比例，与供应商契约及后端
 * build_h3_request 的归一逻辑保持一致（2026-09-19 对 MiniMax-H3 真实核对）：
 *  - 图生（i2v/l2v）：恒为 adaptive——供应商按首帧图片比例渲染，用户选具体
 *    值会被静默忽略，这里直接归一，避免提交一个不会生效的比例（BUG-2）。
 *  - 文生（t2v）：供应商要求具体比例、不接受 adaptive；用户值为自动/无效时
 *    回落默认竖屏 9:16（BUG-1）。
 *  - 参考生（r2v）：尊重用户选择，默认/无效时为 adaptive。
 */
export function resolveSubmittedRatio(
  mode: "t2v" | "i2v" | "l2v" | "r2v",
  draftRatio: string,
): GenerationRatio {
  const selected = (SUPPORTED_VIDEO_RATIOS as readonly string[]).includes(
    draftRatio,
  )
    ? (draftRatio as GenerationRatio)
    : null;
  const concrete =
    selected !== null && selected !== "adaptive" ? selected : null;
  if (mode === "i2v" || mode === "l2v") return "adaptive";
  if (mode === "t2v") return concrete ?? "9:16";
  return selected ?? "adaptive";
}

/** 把分镜卡拼成可读的反推提示词文本（可编辑、可另存为自定义提示词）。 */
export function buildReplicaPromptText(
  shots: ShotCard[],
  originalScript: string,
): string {
  const lines: string[] = [];
  shots.forEach((shot, index) => {
    lines.push(
      `【镜头 ${index + 1}】${shot.start_time.toFixed(1)}s–${shot.end_time.toFixed(1)}s`,
    );
    if (shot.shot_type) {
      lines.push(
        `景别/构图：${shot.shot_type}${shot.composition ? ` · ${shot.composition}` : ""}`,
      );
    }
    if (shot.camera_motion) lines.push(`运镜：${shot.camera_motion}`);
    if (shot.subject) lines.push(`主体：${shot.subject}`);
    if (shot.action) lines.push(`动作：${shot.action}`);
    if (shot.scene) lines.push(`场景：${shot.scene}`);
    if (shot.spoken_text) lines.push(`台词：${shot.spoken_text}`);
    if (shot.transition) lines.push(`转场：${shot.transition}`);
    lines.push("");
  });
  if (originalScript.trim()) {
    lines.push("【原片口播稿】", originalScript.trim());
  }
  return lines.join("\n").trim();
}
