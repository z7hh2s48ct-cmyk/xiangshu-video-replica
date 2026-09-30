import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import {
  type AnalysisTask,
  type AnalysisVersion,
  analysisFailureReference,
  type CharacterReferenceSelection,
  cancelAnalysisTask,
  capturePromptSession,
  customerVisibleErrorMessage,
  type GenerationRatio,
  type GenerationVersionState,
  getAnalysisTask,
  getAssetDownloadUrl,
  getLatestGenerationPrompt,
  getLatestProjectAnalysis,
  getLatestProjectShotCards,
  getLatestScriptRewriteTask,
  getLatestScriptVersion,
  getMaterialBatchPreviews,
  getScriptRewriteTask,
  type H3Mode,
  listUserSavedPrompts,
  type Project,
  type ProjectMainCharacter,
  readAnalysisPayload,
  readFirstFrameSelectionPayload,
  readSourceFrameCandidates,
  rewriteProjectScript,
  type SavedPromptItem,
  type ScriptRewriteTask,
  ScriptRewriteTaskError,
  type ShotCard,
  type ShotCardPayload,
  saveGenerationPrompt,
  saveShotCards,
  selectCharacterReferences,
  startVideoAnalysis,
  waitForAnalysisTask,
  waitForScriptRewriteTask,
} from "../api";
import { CharacterSelection } from "../CharacterSelection";
import { FirstFrameSelection } from "../FirstFrameSelection";
import { isInsufficientCredits } from "../insufficientCredits";
import {
  formatShotCardsForClipboard,
  promptTextFromShotTableClipboard,
  ShotCardEditor,
} from "../ShotCardEditor";
import { SourceFrameSelection } from "../SourceFrameSelection";
import { CreationNavigation } from "./CreationNavigation";
import { useStudio } from "./context";
import { useCopyExtractionProgress } from "./copyExtractionProgress";
import {
  loadDraftMaterials,
  loadSavedScriptList,
  readAudioDuration,
  readVideoDuration,
  readVideoFirstFrame,
  uploadReferenceAudioMaterial,
  uploadVideoMaterial,
  uploadWorkbenchSourceVideo,
} from "./live";
import {
  type FinalReplicaSnapshot,
  PromptEditor,
  ReplicaFinalPromptControls,
  type ReplicaPreflightCheck,
  ReplicaPreflightChecklist,
  replicaInputKey,
} from "./PromptEditor";
import { ReplicaNarration } from "./ReplicaPreparation";
import {
  clearReferencePreview,
  markReferencePreviewsLoading,
  mergeReferencePreviews,
  type ReferencePreviewMap,
  referencePreviewAssetId,
  referencePreviewEntries,
  referencePreviewTargets,
  referencePreviewView,
  releaseReferencePreviews,
} from "./referenceMaterialPreview";
import { hasRef2vaStructure } from "./referencePrompt";
import {
  clearScriptRewriteIdempotencyKey,
  resolvePendingRewrite,
  type ScriptRewriteScope,
  scriptRewriteIdempotencyKey,
  shouldClearScriptRewriteIdempotencyKey,
} from "./scriptRewrite";
import {
  createDraft,
  DEFAULT_MAX_REFERENCE_AUDIOS,
  DEFAULT_MAX_REFERENCE_IMAGES,
  DEFAULT_MAX_REFERENCE_VIDEOS,
  hasCopyResult,
  MAX_REFERENCE_FILES,
  MAX_REFERENCE_MEDIA_SECONDS,
  MIN_REFERENCE_MEDIA_SECONDS,
  mergeStudioAssets,
  resolveSubmittedRatio,
  resolveVideoMode,
  validateReferences,
} from "./state";
import { cancelledTaskCreditsNote, failedTaskCreditsNote } from "./taskCredits";
import type {
  AssetKind,
  StudioAsset,
  StudioDraft,
  StudioPerson,
  StudioScript,
  StudioTask,
  StudioVideo,
} from "./types";
import {
  Button,
  Empty,
  Field,
  Hint,
  Icon,
  Media,
  Panel,
  StudioDialog,
  Tabs,
} from "./ui";
import { type MediaRowStyle, useMediaRowFit } from "./useMediaRowFit";
import { readAppliedOptimization } from "./usePromptOptimization";
import "./creation.css";
import { OralJourney } from "./OralJourney";

function findAsset(assets: StudioAsset[], id?: string) {
  return id ? assets.find((asset) => asset.id === id) : undefined;
}

async function getProjectAnalysisOrNull(
  projectId: string,
): Promise<AnalysisVersion | null> {
  try {
    return await getLatestProjectAnalysis(projectId);
  } catch (cause: unknown) {
    const error = cause as { code?: string; status?: number };
    if (error.status === 404 && error.code === "ANALYSIS_NOT_FOUND")
      return null;
    throw cause;
  }
}

// S14（恢复容错）：历史接口局部失败时的降级空态，语义等同「该项目尚无该版本」。
const EMPTY_GENERATION_VERSION_STATE: GenerationVersionState = {
  stale: false,
  stale_reasons: [],
  version: null,
};

// S10（本地预检）：参考视频的硬性门槛，与 server/app/media.py 同源
// （MAX_UPLOAD_BYTES = 50MB，MIN/MAX_DURATION_SECONDS = 4.0 / 15.0，
// DURATION_ROUNDING_TOLERANCE_SECONDS = 0.1）。超限文件不必整段上传完、
// 等服务端 ffprobe 才拒绝；取整容差同样对齐，本地预检不比服务端更严。
const MAX_REPLICA_SOURCE_BYTES = 50 * 1024 * 1024;
const MIN_REPLICA_SOURCE_SECONDS = 4;
const MAX_REPLICA_SOURCE_SECONDS = 15;
const REPLICA_DURATION_TOLERANCE_SECONDS = 0.1;

// S11（换源稳定性）：换源 / 重来前取消旧的排队中 / 运行中拆解任务，服务端在
// 取消事务里即时释放预留积分；否则旧任务既看不到结果又继续计费。取消是尽力
// 而为——任何失败都不得挡住换源 / 清空这一主操作，服务端对账仍会兜底结算。
async function cancelPreviousAnalysisTask(
  taskId: string | undefined,
  status: string | undefined,
): Promise<void> {
  if (!taskId || (status !== "PENDING" && status !== "RUNNING")) return;
  try {
    await cancelAnalysisTask(taskId);
  } catch {
    // 静默：换源是主操作，取消只是清理。
  }
}

// S13（画幅对齐）：复刻页没有画幅控件，draft.ratio 若不跟随真实输出画幅，
// 最终提示词编译与 AI 优化上下文就会停在 createDraft 的内置默认 9:16 上，
// 而首帧默认「跟随原视频」——源视频是横屏时两边直接对不上。媒体上报的
// 真实宽高比在这里归一到最近的受支持画幅。取对数距离：1.78 到 2.33 与
// 0.56 的差距按比例衡量，跨横竖比较不失真。
const SOURCE_RATIO_OPTIONS: ReadonlyArray<{
  ratio: GenerationRatio;
  aspect: number;
}> = [
  { ratio: "21:9", aspect: 21 / 9 },
  { ratio: "16:9", aspect: 16 / 9 },
  { ratio: "4:3", aspect: 4 / 3 },
  { ratio: "1:1", aspect: 1 },
  { ratio: "3:4", aspect: 3 / 4 },
  { ratio: "9:16", aspect: 9 / 16 },
];

function nearestSourceRatio(aspect: number): GenerationRatio {
  if (!Number.isFinite(aspect) || aspect <= 0) return "9:16";
  let best: GenerationRatio = "9:16";
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const option of SOURCE_RATIO_OPTIONS) {
    const distance = Math.abs(Math.log(aspect / option.aspect));
    if (distance < bestDistance) {
      bestDistance = distance;
      best = option.ratio;
    }
  }
  return best;
}

function findSource(
  assets: StudioAsset[],
  videos: StudioVideo[],
  id?: string,
): StudioAsset | undefined {
  const asset = findAsset(assets, id);
  if (asset) return asset;
  const video = id ? videos.find((item) => item.id === id) : undefined;
  return video
    ? {
        id: video.id,
        name: video.title,
        kind: "video",
        poster: video.poster,
        duration: video.duration,
        group: video.category,
        source: video.platform,
        saved: true,
      }
    : undefined;
}

function activePerson(
  people: StudioPerson[],
  selectedId?: string,
): StudioPerson | undefined {
  return selectedId
    ? people.find((person) => person.id === selectedId)
    : undefined;
}

function SourceStrip({ source }: { source?: StudioAsset }) {
  return (
    <div className="creation-source-strip">
      <span className="creation-status-dot" />
      <span>
        来源：
        {source ? `${source.name} · ${source.source}` : "尚未选择来源视频"}
      </span>
    </div>
  );
}

/**
 * 场景形象行的媒体基准高度：默认基准（clamp(240px, 34dvh, 360px)）只有
 * 240~360px，两栏媒体框都偏小。该行抬到 480px——两栏媒体框统一按参考视频
 * 画幅取形状，列宽不够时由 max-height 反推收窄（见 .media-row .media-frame）。
 */
const SCENE_ROW_MIN_HEIGHT = 480;

/**
 * 媒体行：左右两栏的媒体框等高（底部对齐）由 useMediaRowFit 迭代测量保证。
 * 必须独立成组件——hook 不能在循环或条件中调用，每行需各持一个实例。
 */
function ReplicaMediaRow({
  ratio,
  minHeight,
  className,
  children,
}: {
  ratio: number;
  /** 覆盖媒体基准高度（默认 clamp(240px, 34dvh, 360px)）。场景形象行需要更大的
   *  基准来放大小比例素材的显示尺寸。 */
  minHeight?: number;
  /** 追加到 .media-row 的修饰类（如 media-row--scene 用于右列限宽）。 */
  className?: string;
  children: ReactNode;
}) {
  const { rowRef, rowStyle } = useMediaRowFit({ ratio, minHeight });
  return (
    <div
      className={className ? `media-row ${className}` : "media-row"}
      ref={rowRef}
      style={rowStyle}
    >
      {children}
    </div>
  );
}

/**
 * 面板级比例覆盖：两栏媒体框都显式钉成参考视频画幅。场景图与首帧的素材比例
 * 并不相同（竖长场景图 vs 9:16 首帧），跟随素材自身比例会把两栏拉成不同形状。
 */
function panelRatioStyle(ratio: number): MediaRowStyle {
  return { "--row-ratio": `${ratio}` };
}

function ControlGroup({
  label,
  children,
}: {
  label: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="studio-field">
      <span>{label}</span>
      {children}
    </div>
  );
}

function copyProfile(person?: StudioPerson) {
  if (!person) return "";
  return JSON.stringify({
    display_name: person.name.trim(),
    role: person.role.trim(),
    service_scope: person.scope.trim(),
    target_audience: person.audience.trim(),
    expression_style: person.expression.trim(),
    audience_needs: person.audience_needs?.trim() ?? "",
    factual_background: person.factual_background?.trim() ?? "",
    sample_script: person.sample_script?.trim() ?? "",
    forbidden_claims: person.forbidden_claims?.trim() ?? "",
  });
}
function taskProfileMatches(task: ScriptRewriteTask, fingerprint: string) {
  if (!task.identity_id) return !fingerprint;
  const snapshot = task.ip_profile_snapshot;
  if (!snapshot || !fingerprint) return false;
  const expected = JSON.parse(fingerprint) as Record<string, string>;
  return Object.entries(expected).every(
    ([key, value]) =>
      ((snapshot as unknown as Record<string, unknown>)[key] ?? "") === value,
  );
}
function pendingRewriteStorageKey(accountId: string) {
  return `studio:pending-copy:${accountId}`;
}
function readPendingRewrite(accountId: string): StudioDraft["pendingRewrite"] {
  try {
    const value = JSON.parse(
      sessionStorage.getItem(pendingRewriteStorageKey(accountId)) ?? "null",
    );
    return value &&
      typeof value.scopeKey === "string" &&
      typeof value.resultText === "string"
      ? value
      : undefined;
  } catch {
    return undefined;
  }
}
function storePendingRewrite(
  accountId: string,
  value: StudioDraft["pendingRewrite"],
) {
  try {
    if (value)
      sessionStorage.setItem(
        pendingRewriteStorageKey(accountId),
        JSON.stringify(value),
      );
    else sessionStorage.removeItem(pendingRewriteStorageKey(accountId));
  } catch {
    /* Cloud draft still persists recovery metadata. */
  }
}
function copySource(draft: StudioDraft) {
  return (
    draft.script.original || (draft.script.resultKind ? "" : draft.script.text)
  ).trim();
}
function copyInstructions(draft: StudioDraft) {
  return [
    draft.rewriteLength && draft.rewriteLength !== "original"
      ? `目标约 ${draft.rewriteLength === "custom" ? (draft.rewriteWordCount ?? "") : draft.rewriteLength} 字。`
      : "",
    draft.rewriteInstructions?.trim() ?? "",
  ]
    .filter(Boolean)
    .join("\n");
}

/** 转写通常一到两分钟：进度按已耗时估算（约 95 秒走到 95%），封顶等待真实终态。 */
const EXTRACTION_ESTIMATE_SECONDS = 1.4;

const formatExtractionElapsed = (totalSeconds: number) =>
  totalSeconds < 60
    ? `${totalSeconds} 秒`
    : `${Math.floor(totalSeconds / 60)} 分 ${totalSeconds % 60} 秒`;

export function CopyPage() {
  const {
    state,
    data,
    patchDraft,
    navigate,
    saveDraft,
    confirmFinalDraft,
    openPicker,
    patchState,
    notify,
    review,
    user,
    draftSaveStatus,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const method = state.draft.rewriteMethod ?? "ip";
  const hasResult = hasCopyResult(state.draft.script);
  const extraction = useCopyExtractionProgress();
  const [extractionNow, setExtractionNow] = useState(() => Date.now());
  useEffect(() => {
    if (!extraction.active) return;
    setExtractionNow(Date.now());
    const timer = window.setInterval(() => setExtractionNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [extraction.active]);
  const extractionSeconds = extraction.active
    ? Math.max(0, Math.round((extractionNow - extraction.startedAt) / 1000))
    : 0;
  const extractionPercent = Math.min(
    95,
    Math.round(extractionSeconds / EXTRACTION_ESTIMATE_SECONDS),
  );
  const profileFingerprint =
    method === "custom"
      ? ""
      : copyProfile(data.people.find((item) => item.id === state.draft.ipId));
  const [rewriteError, setRewriteError] = useState("");
  const [tab, setTab] = useState<"rewrite" | "saved">("rewrite");
  const [rewriting, setRewriting] = useState(false);
  const [candidate, setCandidate] = useState(state.draft.rewriteCandidate);
  const [savedLoading, setSavedLoading] = useState(false);
  const [savedError, setSavedError] = useState("");
  const savedOperation = useRef(0);
  const current = useRef({ state, patchDraft, patchState, notify });
  current.current = { state, patchDraft, patchState, notify };
  const rewritePendingRef = useRef(false);
  const rewriteOperationRef = useRef(0);
  const scopeGenerationRef = useRef(0);
  const currentRef = useRef({ state, patchDraft, notify });
  currentRef.current = { state, patchDraft, notify };
  const rewriteScopeKey = JSON.stringify([
    user.id,
    state.draft.id,
    state.draft.projectId,
    state.draft.sourceId,
    state.draft.sourceAssetId,
    state.draft.ipId,
    state.draft.script.id,
    copySource(state.draft),
    method,
    copyInstructions(state.draft),
    profileFingerprint,
  ]);
  const activeScopeRef = useRef({ key: rewriteScopeKey, generation: 0 });
  if (activeScopeRef.current.key !== rewriteScopeKey) {
    activeScopeRef.current = {
      key: rewriteScopeKey,
      generation: ++scopeGenerationRef.current,
    };
  }
  const activeScope = activeScopeRef.current;

  const finishRewrite = useCallback(
    async (
      task: ScriptRewriteTask,
      expectedScope: { key: string; generation: number },
      operation: number,
      requestScope: ScriptRewriteScope,
      idempotencyKey: string,
    ) => {
      try {
        const completed =
          task.status === "PENDING" || task.status === "RUNNING"
            ? await waitForScriptRewriteTask(task.id)
            : task;
        if (
          rewriteOperationRef.current !== operation ||
          activeScopeRef.current !== expectedScope
        )
          return;
        const current = currentRef.current.state.draft;
        const rewritten = completed.result?.rewritten_text?.trim();
        const currentSourceAssetId = current.sourceAssetId ?? current.sourceId;
        if (
          completed.status !== "SUCCEEDED" ||
          completed.project_id !== current.projectId ||
          completed.identity_id !==
            (current.rewriteMethod === "custom" ? null : current.ipId) ||
          completed.source_asset_id !== currentSourceAssetId ||
          completed.source_text !== copySource(current) ||
          (completed.instructions ?? "") !== copyInstructions(current) ||
          !taskProfileMatches(
            completed,
            requestScope.profileFingerprint ?? "",
          ) ||
          !rewritten
        )
          throw completed.status === "FAILED" ||
            completed.status === "SUBMISSION_UNCERTAIN"
            ? new ScriptRewriteTaskError(completed)
            : new Error(
                completed.error_message || "改写未返回完整正文，请重试。",
              );
        clearScriptRewriteIdempotencyKey(requestScope, idempotencyKey);
        storePendingRewrite(requestScope.accountId, undefined);
        if (current.script.text !== requestScope.resultText) {
          // 改写期间用户手改了正文：保留人工稿，结果转候选待显式应用。
          const scopedCandidate = {
            scopeKey: expectedScope.key,
            text: rewritten,
          };
          setCandidate(scopedCandidate);
          currentRef.current.patchDraft({
            pendingRewrite: undefined,
            rewriteCandidate: scopedCandidate,
          });
          currentRef.current.notify(
            "改写已完成，当前正文保持不变；可对照后应用候选稿。",
          );
          return;
        }
        currentRef.current.patchDraft({
          pendingRewrite: undefined,
          rewriteCandidate: undefined,
          script: {
            ...current.script,
            text: rewritten,
            confirmed: false,
            resultKind: "rewritten",
            rewriteTaskId: completed.id,
          },
          scriptEdited: true,
        });
        currentRef.current.notify("改写已完成，请核对并保存当前版本。");
      } catch (cause) {
        if (shouldClearScriptRewriteIdempotencyKey(cause)) {
          clearScriptRewriteIdempotencyKey(requestScope, idempotencyKey);
          if (
            rewriteOperationRef.current === operation &&
            activeScopeRef.current === expectedScope
          ) {
            storePendingRewrite(requestScope.accountId, undefined);
            currentRef.current.patchDraft({ pendingRewrite: undefined });
          }
        }
        if (
          rewriteOperationRef.current === operation &&
          activeScopeRef.current === expectedScope
        ) {
          const message = customerVisibleErrorMessage(
            cause,
            "文案改写失败，请重试。",
          );
          setRewriteError(message);
          currentRef.current.notify(message);
        }
      } finally {
        if (
          rewriteOperationRef.current === operation &&
          activeScopeRef.current === expectedScope
        ) {
          rewritePendingRef.current = false;
          setRewriting(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    const operation = ++rewriteOperationRef.current;
    rewritePendingRef.current = false;
    setRewriting(false);
    const draft = currentRef.current.state.draft;
    setCandidate(
      draft.rewriteCandidate?.scopeKey === activeScope.key
        ? draft.rewriteCandidate
        : undefined,
    );
    setRewriteError("");
    const localPending = readPendingRewrite(user.id);
    const recoverPending = resolvePendingRewrite(
      activeScope.key,
      draft.pendingRewrite,
      localPending,
    );
    if (localPending && localPending.scopeKey !== activeScope.key)
      storePendingRewrite(user.id, undefined);
    if (
      !review &&
      user.role !== "auditor" &&
      draft.projectId &&
      draft.sourceId &&
      (draft.rewriteMethod === "custom" || draft.ipId) &&
      copySource(draft) &&
      (!recoverPending || recoverPending.taskId) &&
      (recoverPending || !hasCopyResult(draft.script))
    ) {
      const sourceAssetId = draft.sourceAssetId ?? draft.sourceId;
      const requestScope: ScriptRewriteScope = {
        accountId: user.id,
        projectId: draft.projectId,
        sourceAssetId,
        identityId: draft.rewriteMethod === "custom" ? "" : (draft.ipId ?? ""),
        scriptId: draft.script.id,
        scriptVersion: draft.script.version,
        text: copySource(draft),
        instructions: copyInstructions(draft),
        resultText: recoverPending?.resultText ?? draft.script.text,
        profileFingerprint,
      };
      const idempotencyKey = scriptRewriteIdempotencyKey(requestScope);
      void (
        recoverPending?.taskId
          ? getScriptRewriteTask(recoverPending.taskId)
          : getLatestScriptRewriteTask(
              draft.projectId,
              draft.rewriteMethod === "custom" ? null : draft.ipId,
              sourceAssetId,
            )
      )
        .then((task) => {
          if (
            rewriteOperationRef.current !== operation ||
            activeScopeRef.current !== activeScope ||
            !task ||
            task.source_asset_id !== sourceAssetId ||
            task.source_text !== copySource(draft) ||
            (task.instructions ?? "") !== copyInstructions(draft) ||
            !taskProfileMatches(task, profileFingerprint) ||
            task.id === draft.script.rewriteTaskId
          )
            return;
          rewritePendingRef.current = true;
          setRewriting(true);
          void finishRewrite(
            task,
            activeScope,
            operation,
            requestScope,
            idempotencyKey,
          );
        })
        .catch((cause) => {
          if (
            rewriteOperationRef.current === operation &&
            activeScopeRef.current === activeScope
          )
            currentRef.current.notify(
              customerVisibleErrorMessage(
                cause,
                "读取上次改写任务失败，可直接重新提交。",
              ),
            );
        });
    }
    return () => {
      rewriteOperationRef.current += 1;
    };
  }, [
    activeScope,
    finishRewrite,
    review,
    user.id,
    user.role,
    profileFingerprint,
  ]);

  const refreshSaved = useCallback(async () => {
    const operation = ++savedOperation.current;
    setSavedLoading(true);
    setSavedError("");
    try {
      const scripts = await loadSavedScriptList();
      if (savedOperation.current === operation)
        current.current.patchState({ savedScripts: scripts });
    } catch (cause) {
      if (savedOperation.current === operation)
        setSavedError(
          customerVisibleErrorMessage(cause, "读取文案失败，请重试。"),
        );
    } finally {
      if (savedOperation.current === operation) setSavedLoading(false);
    }
  }, []);
  useEffect(() => {
    if (tab === "saved" && !review) void refreshSaved();
    return () => {
      savedOperation.current += 1;
    };
  }, [tab, review, refreshSaved]);

  const customWordCountInvalid =
    state.draft.rewriteLength === "custom" &&
    (!Number.isInteger(state.draft.rewriteWordCount) ||
      (state.draft.rewriteWordCount ?? 0) < 1 ||
      (state.draft.rewriteWordCount ?? 0) > 5000);
  const rewriteUnavailableReason = customWordCountInvalid
    ? "请输入1至5000之间的整数文案字数。"
    : review
      ? "审核示例不调用业务接口。"
      : readOnly
        ? "当前账号为只读权限，不能改写文案。"
        : !state.draft.projectId
          ? "当前文案缺少来源项目。"
          : !state.draft.sourceId
            ? "当前文案缺少来源视频，请重新选择来源。"
            : method === "ip" && !state.draft.ipId
              ? "请先选择参与二创的人物 IP。"
              : !copySource(state.draft)
                ? "请先提取待改写正文。"
                : copySource(state.draft).length > 20000
                  ? "待改写正文不能超过 20000 字符。"
                  : copyInstructions(state.draft).length > 2000
                    ? "本次改写要求不能超过 2000 字符。"
                    : method === "custom" &&
                        !state.draft.rewriteInstructions?.trim()
                      ? "请填写本次改写要求，或选择一个快捷要求。"
                      : "";

  const rewrite = async () => {
    if (rewriteUnavailableReason || rewriting || rewritePendingRef.current)
      return;
    const draft = state.draft;
    const projectId = draft.projectId;
    const identityId = method === "custom" ? undefined : draft.ipId;
    const sourceAssetId = draft.sourceAssetId ?? draft.sourceId;
    if (!projectId || !sourceAssetId) return;
    const operation = ++rewriteOperationRef.current;
    const expectedScope = activeScopeRef.current;
    const retryPending = resolvePendingRewrite(
      expectedScope.key,
      draft.pendingRewrite,
      readPendingRewrite(user.id),
    );
    const requestScope: ScriptRewriteScope = {
      accountId: user.id,
      projectId,
      sourceAssetId,
      identityId: identityId ?? "",
      scriptId: draft.script.id,
      scriptVersion: draft.script.version,
      text: copySource(draft),
      instructions: copyInstructions(draft),
      resultText: retryPending?.resultText ?? draft.script.text,
      profileFingerprint,
    };
    const idempotencyKey =
      retryPending?.requestKey ?? scriptRewriteIdempotencyKey(requestScope);
    const pending = {
      scopeKey: expectedScope.key,
      resultText: requestScope.resultText ?? draft.script.text,
      requestKey: idempotencyKey,
      startedAt: retryPending?.startedAt ?? Date.now(),
    };
    storePendingRewrite(user.id, pending);
    patchDraft({ pendingRewrite: pending, rewriteCandidate: undefined });
    rewritePendingRef.current = true;
    setRewriteError("");
    setCandidate(undefined);
    setRewriting(true);
    try {
      const task = await rewriteProjectScript(
        projectId,
        copySource(draft),
        identityId,
        sourceAssetId,
        idempotencyKey,
        copyInstructions(draft),
      );
      if (
        rewriteOperationRef.current !== operation ||
        activeScopeRef.current !== expectedScope
      )
        return;
      const acceptedPending = { ...pending, taskId: task.id };
      storePendingRewrite(user.id, acceptedPending);
      currentRef.current.patchDraft({ pendingRewrite: acceptedPending });
      await finishRewrite(
        task,
        expectedScope,
        operation,
        requestScope,
        idempotencyKey,
      );
    } catch (cause) {
      if (shouldClearScriptRewriteIdempotencyKey(cause)) {
        clearScriptRewriteIdempotencyKey(requestScope, idempotencyKey);
        if (activeScopeRef.current === expectedScope) {
          storePendingRewrite(user.id, undefined);
          currentRef.current.patchDraft({ pendingRewrite: undefined });
        }
      }
      if (
        rewriteOperationRef.current === operation &&
        activeScopeRef.current === expectedScope
      ) {
        rewritePendingRef.current = false;
        setRewriting(false);
        const message = customerVisibleErrorMessage(
          cause,
          "提交文案改写失败，请重试。",
        );
        setRewriteError(message);
        notify(message);
      }
    }
  };
  const applySavedScript = (script: StudioScript) => {
    const project = data.projects.find(
      (item) => item.id === script.sourceProjectId,
    );
    patchDraft({
      ...createDraft(),
      projectId: script.sourceProjectId,
      ipId: script.ipId,
      sourceId: project?.reference_asset_id ?? undefined,
      sourceAssetId: project?.reference_asset_id ?? undefined,
      originalImageId: undefined,
      imageId: undefined,
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      tailFrameId: undefined,
      avatarId: undefined,
      voiceId: undefined,
      audioId: undefined,
      videoBatchId: undefined,
      script: { ...script, confirmed: false, resultKind: "manual" },
      scriptEdited: true,
    });
    patchState({
      selectedVideoId: undefined,
      selectedTaskId: undefined,
      selectedAssetId: undefined,
      selectedPersonId: script.ipId,
      returnTo: undefined,
    });
    setTab("rewrite");
  };
  const source = findSource(
    data.assets,
    data.videos,
    state.draft.sourceId ?? state.selectedVideoId,
  );
  const person = activePerson(data.people, state.draft.ipId);
  const saved = state.savedScripts;
  const oralLengthExceeded =
    state.draft.script.text.length > 10000 ||
    state.draft.script.title.length > 120;

  return (
    <section className="creation-page creation-copy">
      <header className="creation-heading">
        <h1>文案工坊</h1>
        <p>核对视频原文，设置二创要求，生成后编辑定稿</p>
        {state.returnTo && state.returnTo !== "copy" ? (
          <Button
            variant="quiet"
            onClick={() =>
              navigate(state.returnTo ?? "workbench", { returnTo: undefined })
            }
          >
            返回上一步
          </Button>
        ) : null}
      </header>
      {extraction.active ? (
        <div className="creation-extraction-banner" role="status">
          <div className="creation-extraction-banner__copy">
            <strong>{extraction.detail}</strong>
            <span>
              已等待 {formatExtractionElapsed(extractionSeconds)}
              ；通常一到两分钟完成，原文会自动填入下方文本框。
            </span>
          </div>
          <div aria-hidden="true" className="creation-extraction-banner__track">
            <div
              className="creation-extraction-banner__bar"
              style={{ width: `${extractionPercent}%` }}
            />
          </div>
        </div>
      ) : null}
      <Tabs
        items={[
          { id: "rewrite", label: "文案改写" },
          { id: "saved", label: "我的文案" },
        ]}
        value={tab}
        onChange={(value) => setTab(value as "rewrite" | "saved")}
      />

      {tab === "saved" ? (
        <Panel className="creation-saved-list">
          {!review ? (
            <Button
              variant="outline"
              disabled={savedLoading}
              onClick={() => void refreshSaved()}
            >
              刷新文案
            </Button>
          ) : null}
          {savedLoading ? <Hint>正在读取文案…</Hint> : null}
          {savedError ? (
            <Empty
              title={savedError}
              action={
                <Button onClick={() => void refreshSaved()}>
                  重试读取文案
                </Button>
              }
            />
          ) : saved.length ? (
            saved.map((script) => (
              <button
                className="creation-script-row"
                disabled={readOnly}
                key={script.id}
                onClick={() => applySavedScript(script)}
                type="button"
              >
                <span>{script.title}</span>
                <small>{script.confirmed ? "已确认" : "草稿"}</small>
              </button>
            ))
          ) : !savedLoading ? (
            <Empty
              title="还没有保存的文案"
              description="保存当前稿后，文案会集中显示在这里；再次保存会更新同一篇。"
            />
          ) : null}
        </Panel>
      ) : (
        <>
          <SourceStrip source={source} />
          <p id="copy-resize-hint" className="creation-resize-hint">
            拖动文本框右下角可调整高度
          </p>

          <div className="creation-copy-grid">
            <Panel className="creation-copy-source">
              <h2 className="creation-panel-title">
                <span className="creation-step-number">01</span>
                原文（提取自来源）
              </h2>
              {state.draft.script.original ? (
                <textarea
                  aria-label="来源原文"
                  aria-describedby="copy-resize-hint"
                  className="creation-textarea creation-source-textarea"
                  readOnly
                  value={state.draft.script.original}
                />
              ) : (
                <Empty
                  title="尚未提取文案"
                  description="可先从爆款视频或视频来源进入文案工坊。"
                  action={
                    <Button
                      variant="outline"
                      onClick={() => navigate("replica")}
                    >
                      去视频拆解
                    </Button>
                  }
                />
              )}
            </Panel>
            <Panel className="creation-copy-settings">
              <h2 className="creation-panel-title">
                <span className="creation-step-number">02</span>设置二创
              </h2>
              <fieldset
                className="copy-methods"
                disabled={readOnly || rewriting}
              >
                <legend>选择二创方式</legend>
                <label>
                  <input
                    type="radio"
                    name="copy-method"
                    checked={method === "ip"}
                    onChange={() => patchDraft({ rewriteMethod: "ip" })}
                  />
                  <strong>按人物 IP</strong>
                  <small>带入人物定位、受众和表达风格</small>
                </label>
                <label>
                  <input
                    type="radio"
                    name="copy-method"
                    checked={method === "custom"}
                    onChange={() => patchDraft({ rewriteMethod: "custom" })}
                  />
                  <strong>按要求二创</strong>
                  <small>直接描述这次想怎么改</small>
                </label>
              </fieldset>
              {method === "ip" ? (
                <div className="copy-person-summary">
                  <div>
                    <strong>
                      {person
                        ? `${person.name} · ${person.role}`
                        : "未选择人物 IP"}
                    </strong>
                    <p>
                      {person
                        ? `${person.audience} · ${person.expression}`
                        : "选择人物后自动带入已保存的 IP 档案。"}
                    </p>
                  </div>
                  <Button
                    disabled={readOnly || rewriting}
                    variant="outline"
                    onClick={() => openPicker("person")}
                  >
                    更换人物
                  </Button>
                  {person ? (
                    <Button
                      variant="quiet"
                      onClick={() =>
                        navigate("person-ip", {
                          selectedPersonId: person.id,
                          returnTo: "copy",
                        })
                      }
                    >
                      完善 IP 档案
                    </Button>
                  ) : null}
                </div>
              ) : null}
              <div className="copy-requirements">
                <label>
                  文案字数
                  <select
                    aria-label="文案字数"
                    disabled={readOnly || rewriting}
                    value={state.draft.rewriteLength ?? "original"}
                    onChange={(event) =>
                      patchDraft({
                        rewriteLength: event.target
                          .value as StudioDraft["rewriteLength"],
                      })
                    }
                  >
                    <option value="original">接近原文</option>
                    <option value="100">约100字</option>
                    <option value="200">约200字</option>
                    <option value="300">约300字</option>
                    <option value="custom">自定义字数</option>
                  </select>
                  {state.draft.rewriteLength === "custom" ? (
                    <input
                      aria-label="自定义文案字数"
                      aria-invalid={customWordCountInvalid}
                      className="creation-input"
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={5000}
                      step={1}
                      disabled={readOnly || rewriting}
                      placeholder="输入1–5000字"
                      value={state.draft.rewriteWordCount ?? ""}
                      onChange={(event) =>
                        patchDraft({
                          rewriteWordCount:
                            event.target.value === ""
                              ? undefined
                              : Number(event.target.value),
                        })
                      }
                    />
                  ) : null}
                  <small>生成字数为近似值，可在结果中调整。</small>
                </label>
                <label>
                  {method === "ip" ? "补充要求（选填）" : "本次改写要求"}
                  <textarea
                    aria-label="本次改写要求"
                    className="creation-textarea copy-instructions"
                    maxLength={1900}
                    disabled={readOnly || rewriting}
                    value={state.draft.rewriteInstructions ?? ""}
                    onChange={(event) =>
                      patchDraft({ rewriteInstructions: event.target.value })
                    }
                    placeholder="例如：面向准备回乡建房的家庭，语气朴实，先讲问题再给建议，不添加报价。"
                  />
                </label>
              </div>
              <div className="copy-quick-actions">
                {["更口语化", "精简内容", "知识讲解", "调整开头"].map(
                  (label) => (
                    <Button
                      key={label}
                      variant="outline"
                      disabled={readOnly || rewriting}
                      onClick={() =>
                        patchDraft({
                          rewriteInstructions: [
                            state.draft.rewriteInstructions?.trim(),
                            label,
                          ]
                            .filter(Boolean)
                            .join("；")
                            .slice(0, 1900),
                        })
                      }
                    >
                      {label}
                    </Button>
                  ),
                )}
              </div>
              <div className="copy-generate-row">
                <Hint>
                  保留原文信息，生成后可继续编辑；发布前请核对事实、案例与承诺。
                </Hint>
                <Button
                  variant="primary"
                  onClick={() => void rewrite()}
                  disabled={Boolean(rewriteUnavailableReason) || rewriting}
                >
                  {rewriting ? "正在生成…" : "生成二创文案"}
                </Button>
              </div>
              {rewriteUnavailableReason ? (
                <Hint>{rewriteUnavailableReason}</Hint>
              ) : null}
              {rewriteError ? <p role="alert">{rewriteError}</p> : null}
            </Panel>
            <Panel className="creation-copy-editor">
              <div className="creation-panel-title-row">
                <h2 className="creation-panel-title">
                  <span className="creation-step-number">03</span>
                  二创结果
                </h2>
                {hasResult ? (
                  <small>
                    {state.draft.script.confirmed ? "终稿" : "草稿"} V
                    {state.draft.script.version}
                  </small>
                ) : null}
              </div>
              {hasResult ? (
                <>
                  <input
                    aria-label="作品名称"
                    className="creation-input"
                    disabled={readOnly}
                    onChange={(event) =>
                      patchDraft({
                        script: {
                          ...state.draft.script,
                          title: event.target.value,
                          confirmed: false,
                        },
                      })
                    }
                    placeholder="作品名称"
                    value={state.draft.script.title}
                  />
                  <textarea
                    aria-label="二创文案"
                    aria-describedby="copy-resize-hint"
                    className="creation-textarea creation-copy-textarea"
                    disabled={readOnly}
                    onChange={(event) =>
                      patchDraft({
                        script: {
                          ...state.draft.script,
                          text: event.target.value,
                          confirmed: false,
                          resultKind: "manual",
                        },
                      })
                    }
                    placeholder="在这里编辑乡墅口播文案"
                    value={state.draft.script.text}
                  />
                  <div className="creation-saved-state" aria-live="polite">
                    {draftSaveStatus === "dirty"
                      ? "有未保存修改"
                      : draftSaveStatus === "saving"
                        ? "正在保存到云端…"
                        : draftSaveStatus === "saved"
                          ? "已保存到云端"
                          : draftSaveStatus === "error"
                            ? "云端保存失败，可点击保存版本重试"
                            : "内容变动后需重新确认终稿"}
                  </div>
                  <Hint>
                    实际字数：
                    {state.draft.script.text.replace(/\s/g, "").length}；
                    {state.draft.script.text.length} / 10000
                    字符（数字人口播上限）；标题{" "}
                    {state.draft.script.title.length} / 120 字符。AI 改写最多
                    20000 字符。
                  </Hint>
                </>
              ) : (
                <Empty
                  title={rewriting ? "正在生成二创文案" : "等待生成二创文案"}
                  description="在上方选择方式并点击生成，结果将在这里出现。"
                  action={
                    !state.draft.script.original && !rewriting ? (
                      <Button
                        variant="quiet"
                        disabled={readOnly}
                        onClick={() =>
                          patchDraft({
                            script: {
                              ...state.draft.script,
                              resultKind: "manual",
                              text: "",
                              confirmed: false,
                            },
                          })
                        }
                      >
                        手动写稿
                      </Button>
                    ) : undefined
                  }
                />
              )}
              {candidate?.scopeKey === activeScope.key ? (
                <Panel>
                  <p>{candidate.text}</p>
                  <Button
                    onClick={() => {
                      patchDraft({
                        rewriteCandidate: undefined,
                        pendingRewrite: undefined,
                        script: {
                          ...state.draft.script,
                          text: candidate.text,
                          confirmed: false,
                          resultKind: "rewritten",
                        },
                      });
                      setCandidate(undefined);
                    }}
                  >
                    应用候选稿
                  </Button>
                </Panel>
              ) : null}
            </Panel>
          </div>
          {hasResult ? (
            <footer className="creation-action-bar">
              {/* 缺人物不再是拦阻条件：数字人口播页自带「更换 IP」入口，
                  进入后补选即可。文案链路（爆款视频提取）不携带 ipId，
                  若在这里拦死，按钮会永久置灰且无任何原因提示。 */}
              {!person ? (
                <div className="creation-action-note">
                  <small>未选择人物 IP，进入数字人口播页后可选择</small>
                </div>
              ) : null}
              <Button
                variant="primary"
                disabled={
                  readOnly ||
                  !state.draft.script.text.trim() ||
                  oralLengthExceeded
                }
                onClick={() => confirmFinalDraft()}
              >
                确认终稿
              </Button>
              <Button
                variant="outline"
                disabled={readOnly}
                onClick={() => {
                  if (readOnly) return;
                  saveDraft();
                }}
              >
                保存版本
              </Button>
              <Button
                variant="primary"
                disabled={!state.draft.script.confirmed || oralLengthExceeded}
                onClick={() => navigate("oral", { returnTo: "copy" })}
              >
                用于数字人口播
              </Button>
            </footer>
          ) : null}
        </>
      )}
    </section>
  );
}

type ReplicaStage = "source" | "analyzing" | "ready";

function sourceFrameTimestamp(selection: AnalysisVersion | null): number {
  if (!selection) return 0;
  const assetId = selection.payload.source_frame_asset_id;
  if (typeof assetId !== "string") return 0;
  return (
    readSourceFrameCandidates(selection)?.candidates.find(
      (candidate) => candidate.asset_id === assetId,
    )?.timestamp_seconds ?? 0
  );
}

// 审核包样例分镜：仅 review 视觉演示，不参与真实拆解流程。
const REVIEW_SAMPLE_SHOTS: ShotCard[] = [
  {
    shot_id: "shot-1",
    start_time: 0,
    end_time: 5,
    shot_type: "中景",
    composition: "",
    camera_motion: "推进",
    subject: "院落",
    action: "镜头缓推庭院",
    scene: "乡墅庭院",
    spoken_text: "",
    transition: "切镜",
  },
  {
    shot_id: "shot-2",
    start_time: 5,
    end_time: 10,
    shot_type: "近景",
    composition: "",
    camera_motion: "固定",
    subject: "讲解人物",
    action: "人物出镜讲解",
    scene: "庭院",
    spoken_text: "这栋房子的采光设计",
    transition: "切镜",
  },
  {
    shot_id: "shot-3",
    start_time: 10,
    end_time: 15,
    shot_type: "特写",
    composition: "",
    camera_motion: "摇移",
    subject: "外立面",
    action: "外立面细节展示",
    scene: "建筑外立面",
    spoken_text: "",
    transition: "切镜",
  },
];

/** 归一到后端契约的 4–15 秒整数区间（`output_duration_seconds` 是
 * `ge=4, le=15`）。这里曾把时长折叠成 4/15 两档，那是 H3 早期只支持两档时的
 * 权宜；PR #160 放开到每秒后，折叠会让提示词上下文里的时长与实际生成时长
 * 对不上——按 12 秒生成、却按 15 秒写提示词。
 * 导出供独立创作页共用：非法时长按钳位处理，不允许任何路径静默回落 8 秒
 * （上线前检查 P1-4）。 */
export function normalizeCustomerDuration(seconds: number): number {
  if (!Number.isFinite(seconds)) return 15;
  return Math.min(15, Math.max(4, Math.round(seconds)));
}

export function ReplicaPage() {
  const {
    state,
    data,
    review,
    discardSavedDraft,
    patchDraft,
    updateData,
    navigate,
    notify,
    openLive,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const project = data.projects.find(
    (item) => item.id === state.draft.projectId,
  );
  const source = findSource(
    data.assets,
    data.videos,
    state.draft.sourceId ?? state.selectedVideoId,
  );
  const sourceMediaKey = source?.url || source?.poster || source?.id || "";
  const [sourceRatio, setSourceRatio] = useState<{
    source: string;
    ratio: number;
  }>();
  const previewRatio =
    sourceRatio?.source === sourceMediaKey ? sourceRatio.ratio : 9 / 16;
  const selectedFirstFrame = findAsset(data.assets, state.draft.firstFrameId);
  const firstFrameMediaKey =
    selectedFirstFrame?.url ||
    selectedFirstFrame?.poster ||
    selectedFirstFrame?.id ||
    "";
  const [firstFrameRatio, setFirstFrameRatio] = useState<{
    source: string;
    ratio: number;
  }>();
  const firstFramePreviewRatio =
    firstFrameRatio?.source === firstFrameMediaKey
      ? firstFrameRatio.ratio
      : 9 / 16;
  const [stage, setStage] = useState<ReplicaStage>(() =>
    state.draft.projectId ? "ready" : "source",
  );
  const [shots, setShots] = useState<ShotCard[]>([]);
  const [shotCardVersionId, setShotCardVersionId] = useState<string>();
  const [analysisVersionId, setAnalysisVersionId] = useState<string>();
  const [analysisVersionCreatedAt, setAnalysisVersionCreatedAt] =
    useState<string>();
  const [shotsDirty, setShotsDirty] = useState(false);
  const [savingShots, setSavingShots] = useState(false);
  const [shotSaveError, setShotSaveError] = useState("");
  const [originalScript, setOriginalScript] = useState("");
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [analysisStatus, setAnalysisStatus] =
    useState<AnalysisTask["status"]>();
  const [analysisError, setAnalysisError] = useState("");
  const [analysisElapsed, setAnalysisElapsed] = useState(0);
  // S8（拆解进度跨会话恢复）：接回的任务用服务端 created_at 当等待起点，
  // 跨会话已等过的分钟数不会从 0 重来；拿到任务前保持 undefined，回落本机起点。
  const analysisWaitStartedRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!analysisBusy) return;
    const started = Date.now();
    setAnalysisElapsed(0);
    const timer = window.setInterval(() => {
      const base = analysisWaitStartedRef.current ?? started;
      setAnalysisElapsed(Math.max(0, Math.floor((Date.now() - base) / 1000)));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [analysisBusy]);
  const [promptText, setPromptText] = useState(state.draft.prompt);
  const [finalSnapshot, setFinalSnapshot] =
    useState<FinalReplicaSnapshot | null>(() =>
      state.draft.projectId ? (state.draft.finalSnapshot ?? null) : null,
    );
  const finalInput = {
    projectId: state.draft.projectId ?? "",
    scriptText: state.draft.script.text,
    firstFrameAssetId: state.draft.firstFrameId ?? "",
    // 时长逐秒下发。早期 H3 只支持 4/15 两档，这里曾折叠成
    // `duration <= 9 ? 4 : 15`；PR #160 把契约放开到 4–15 每秒后该映射即失效，
    // 默认 8 秒会被悄悄发成 4 秒。与 generation_context 共用同一个归一函数，
    // 免得两处对「目标时长」的理解再次走岔。
    duration: normalizeCustomerDuration(state.draft.duration),
    resolution: (state.draft.resolution === "2K" ? "2K" : "768P") as
      | "768P"
      | "2K",
    ratio: state.draft.ratio as GenerationRatio,
    shotCardVersionId,
  };
  // 正文为空时不算就绪：快照还在、文本被清空（或草稿恢复出脱节数据）时，
  // 「已完成」徽标与「去 AI 视频创作」不能凭空放行一个空交付物。
  const finalReady =
    Boolean(promptText.trim()) &&
    finalSnapshot?.inputKey === replicaInputKey(finalInput);
  // finalSnapshot 只在挂载时读一次草稿：草稿是异步恢复的，挂载时 projectId 还没到，
  // 刷新后草稿里的快照就再也读不进来，「已完成」状态凭空消失。项目 id 变化时补读一次。
  // 不能无差别跟随草稿：改镜头会就地清空快照（不落库），跟随会把清空结果复原。
  const snapshotProjectIdRef = useRef(state.draft.projectId);
  useEffect(() => {
    if (snapshotProjectIdRef.current === state.draft.projectId) {
      return;
    }
    snapshotProjectIdRef.current = state.draft.projectId;
    setFinalSnapshot(
      state.draft.projectId ? (state.draft.finalSnapshot ?? null) : null,
    );
  }, [state.draft.projectId, state.draft.finalSnapshot]);
  const [promptNameOpen, setPromptNameOpen] = useState(false);
  const [promptName, setPromptName] = useState("");
  const [savingPrompt, setSavingPrompt] = useState(false);

  const [restoreBusy, setRestoreBusy] = useState(false);
  const [restoreError, setRestoreError] = useState("");
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadOperationRef = useRef(0);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const restoreOperationRef = useRef(0);
  const restoredProjectIdRef = useRef<string | undefined>(undefined);
  const restoreSuppressedRef = useRef(false);
  const promptSaveOperationRef = useRef(0);
  const shotSaveOperationRef = useRef(0);
  const promptEditVersionRef = useRef(0);
  // 拆解完成回调用：比对发起时的项目，防止换视频后的旧结果覆盖新状态。
  const analysisProjectRef = useRef<string | undefined>(undefined);
  const promptTextRef = useRef(promptText);
  const promptEditedRef = useRef(
    state.draft.projectId === project?.id && state.draft.promptEdited === true,
  );
  const promptTypedThisMountRef = useRef(false);
  const latestDraftRef = useRef(state.draft);
  const patchDraftRef = useRef(patchDraft);
  const replicaOperationRef = useRef(0);
  // S8：挂载期自动接回在途拆解任务（定义 startAnalysis 后立即回填）。
  const startAnalysisRef = useRef<() => Promise<void>>(async () => {});
  // S8：同一 taskId 只自动接回一次——StrictMode 双执行、状态刷新都不重复触发。
  const autoResumeTaskRef = useRef<string | undefined>(undefined);
  // S8：拆解会话号。换源 / 重来会前进一位并立即释放忙碌态：旧会话迟到收尾
  // （取消失败时轮询可能还要跑很久）不得再把界面按在“拆解中”。
  const analysisSessionRef = useRef(0);
  // 历史恢复的在途标记走 ref 镜像：挂载期自动接回要在同一个 effect 批次里
  // 判断恢复是否还在途（state 更新要到下一轮渲染才可见）。
  const restoreBusyRef = useRef(false);
  const replicaContextRef = useRef("");
  replicaContextRef.current = JSON.stringify({
    draftId: state.draft.id,
    projectId: state.draft.projectId,
    sourceId: state.draft.sourceId,
    shotCardVersionId,
    promptText,
    originalScript,
    shots,
    scriptText: state.draft.script.text,
    scriptConfirmed: state.draft.script.confirmed,
    duration: state.draft.duration,
    resolution: state.draft.resolution,
    count: state.draft.count,
    ratio: state.draft.ratio,
  });
  if (
    !promptTypedThisMountRef.current &&
    state.draft.projectId === project?.id &&
    state.draft.promptEdited === true
  ) {
    promptTextRef.current = state.draft.prompt;
    promptEditedRef.current = true;
  } else {
    promptTextRef.current = promptText;
  }
  latestDraftRef.current = state.draft;
  patchDraftRef.current = patchDraft;

  // hasShots 前置计算以避免前向引用问题（用于满足 biome 的完整性检查）
  const displayShots =
    shots.length > 0
      ? shots
      : review && stage === "ready"
        ? REVIEW_SAMPLE_SHOTS
        : [];
  const hasShots = stage === "ready" && displayShots.length > 0;

  useEffect(
    () => () => {
      analysisProjectRef.current = undefined;
      analysisSessionRef.current += 1;
      uploadOperationRef.current += 1;
      uploadAbortRef.current?.abort();
      restoreOperationRef.current += 1;
      promptSaveOperationRef.current += 1;
      shotSaveOperationRef.current += 1;
      restoredProjectIdRef.current = undefined;
      restoreSuppressedRef.current = false;
    },
    [],
  );

  // 终稿指纹只存在内存里的话，刷新或重进页面就会退回"待合成"，而上游其实没有任何变化。
  // 与草稿一起持久化，恢复时按 inputKey 比对即可判定是否仍然有效。
  const applyFinalSnapshot = (snapshot: FinalReplicaSnapshot | null) => {
    setFinalSnapshot(snapshot);
    patchDraftRef.current({ finalSnapshot: snapshot ?? undefined });
  };

  // promptText 是否由"合成最终提示词"写入（而非用户手敲）：上游变化时只作废这一种。
  const compiledPromptRef = useRef(false);

  // 上游内容变化后旧稿已作废：留着它会让用户在"待合成"下点击送生成并撞 409。
  // 返回是否真的清掉了由合成写入的正文，交给调用方决定怎么提示。
  const invalidateStaleFinalPrompt = () => {
    applyFinalSnapshot(null);
    if (!compiledPromptRef.current) return false;
    compiledPromptRef.current = false;
    setPromptText("");
    promptTextRef.current = "";
    promptEditedRef.current = false;
    patchDraftRef.current({ prompt: "", promptEdited: false });
    return true;
  };

  const resetReplicaState = () => {
    setAnalysisError("");
    setAnalysisStatus(undefined);
    setShots([]);
    shotSaveOperationRef.current += 1;
    setSavingShots(false);
    setShotCardVersionId(undefined);
    setAnalysisVersionId(undefined);
    setAnalysisVersionCreatedAt(undefined);
    setShotsDirty(false);
    setShotSaveError("");
    setOriginalScript("");
    compiledPromptRef.current = false;
    applyFinalSnapshot(null);
    analysisProjectRef.current = undefined;
  };

  // 重来出路（LEFTOVER-ON-OPEN）：绑上项目后，空态的渲染条件
  // `stage === "source" && !project` 就再也满足不了了——project 是从草稿的
  // projectId 推出来的，页面此前没有任何入口能解开这个绑定，用户想重新开始
  // 只能「更换来源视频」（得先有个新文件）。这里把绑定连同云端存的上次内容
  // 一起清掉，页面才真的回到起点。
  const startFreshReplica = () => {
    if (readOnly) return;
    // S11：清空绑定前取消旧的排队中 / 运行中任务（含跨会话恢复的 PENDING/
    // RUNNING 草稿），否则任务继续计费且结果再也看不到。
    const previousTaskId = state.draft.analysisTaskId;
    const previousTaskStatus = state.draft.analysisTaskStatus;
    // S8：重来接管会话——旧会话号前进一位并立即释放忙碌态，旧任务迟到的
    // 轮询收尾不能再把重来后的界面按在“拆解中”。
    analysisSessionRef.current += 1;
    setAnalysisBusy(false);
    restoreOperationRef.current += 1;
    restoredProjectIdRef.current = undefined;
    restoreSuppressedRef.current = false;
    restoreBusyRef.current = false;
    setRestoreBusy(false);
    setRestoreError("");
    resetReplicaState();
    setPromptText("");
    promptTextRef.current = "";
    promptEditedRef.current = false;
    promptTypedThisMountRef.current = false;
    setStage("source");
    patchDraft({
      projectId: undefined,
      sourceId: undefined,
      sourceAssetId: undefined,
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      analysisTaskId: undefined,
      analysisTaskStatus: undefined,
      prompt: "",
      promptEdited: false,
      script: createDraft().script,
      scriptEdited: false,
    });
    discardSavedDraft();
    void cancelPreviousAnalysisTask(previousTaskId, previousTaskStatus);
    notify("已清空本次复刻，上传新的参考视频即可重新开始。");
  };

  const restoreSavedProject = useCallback(
    async (target: Project, force = false) => {
      if (restoreSuppressedRef.current) return;
      if (!force && restoredProjectIdRef.current === target.id) return;
      const operation = ++restoreOperationRef.current;
      const promptVersionAtStart = promptEditVersionRef.current;
      restoredProjectIdRef.current = target.id;
      restoreBusyRef.current = true;
      setRestoreBusy(true);
      setRestoreError("");
      try {
        // S14：四个历史接口分开结算——任一项失败只降级该项，不再连累已经拿到的
        // 分镜 / 文案 / 提示词（此前 Promise.all 任一失败，整页只剩错误提示）。
        const [shotResult, analysisResult, promptResult, scriptResult] =
          await Promise.allSettled([
            getLatestProjectShotCards(target.id),
            getProjectAnalysisOrNull(target.id),
            getLatestGenerationPrompt(target.id),
            getLatestScriptVersion(target.id),
          ]);
        if (operation !== restoreOperationRef.current) return;

        // 分镜是历史恢复的主体：它失败仍走「整体失败 + 重试」路径。
        if (shotResult.status === "rejected") {
          restoredProjectIdRef.current = undefined;
          setRestoreError(
            customerVisibleErrorMessage(
              shotResult.reason,
              "历史分镜读取失败，请重试。",
            ),
          );
          return;
        }

        const shotVersion = shotResult.value;
        const analysisVersion =
          analysisResult.status === "fulfilled" ? analysisResult.value : null;
        const promptState =
          promptResult.status === "fulfilled"
            ? promptResult.value
            : EMPTY_GENERATION_VERSION_STATE;
        const scriptState =
          scriptResult.status === "fulfilled"
            ? scriptResult.value
            : EMPTY_GENERATION_VERSION_STATE;
        const partialFailures: string[] = [];
        if (analysisResult.status === "rejected")
          partialFailures.push("视频拆解");
        if (promptResult.status === "rejected")
          partialFailures.push("视频生成提示词");
        if (scriptResult.status === "rejected")
          partialFailures.push("口播文案");

        const shotPayload = shotVersion
          ? (shotVersion.payload as ShotCardPayload)
          : null;
        const restoredShots = shotPayload?.shots ?? [];
        const analysis = analysisVersion
          ? readAnalysisPayload(analysisVersion)
          : null;
        const original = analysis?.original_script ?? "";
        const savedPromptText = promptState.version?.payload.prompt_text;
        const savedPrompt =
          !promptState.stale && typeof savedPromptText === "string"
            ? savedPromptText
            : "";
        const prompt = savedPrompt;
        const savedScriptText = scriptState.version?.payload.full_text;
        const savedScript =
          !scriptState.stale && typeof savedScriptText === "string"
            ? scriptState.version
            : null;
        const currentDraft = latestDraftRef.current;
        const keepLocalDraft = currentDraft.projectId === target.id;
        const keepLocalScript =
          keepLocalDraft && currentDraft.scriptEdited === true;
        const keepLocalPrompt =
          keepLocalDraft &&
          (currentDraft.promptEdited === true ||
            promptEditedRef.current ||
            promptEditVersionRef.current !== promptVersionAtStart);
        const promptStillEdited =
          keepLocalDraft &&
          (currentDraft.promptEdited === true || promptEditedRef.current);
        const blankScript = createDraft().script;
        const script = keepLocalScript
          ? currentDraft.script
          : {
              ...blankScript,
              id: savedScript?.id ?? blankScript.id,
              title: target.name,
              original,
              text: savedScript
                ? (savedScript.payload.full_text as string)
                : original,
              version: savedScript?.version_number ?? 1,
              confirmed: false,
            };
        const restoredPrompt = keepLocalPrompt ? promptTextRef.current : prompt;

        setShots(restoredShots);
        setShotCardVersionId(shotVersion?.id || undefined);
        setAnalysisVersionId(
          shotPayload?.source_analysis_version_id ?? analysisVersion?.id,
        );
        setAnalysisVersionCreatedAt(analysisVersion?.created_at);
        setShotsDirty(false);
        setShotSaveError("");
        setOriginalScript(original);
        setPromptText(restoredPrompt);
        promptTextRef.current = restoredPrompt;
        patchDraftRef.current({
          projectId: target.id,
          sourceId: target.reference_asset_id ?? undefined,
          sourceAssetId: target.reference_asset_id ?? undefined,
          prompt: restoredPrompt,
          promptEdited: promptStillEdited,
          script,
          scriptEdited: keepLocalScript,
        });
        setStage("ready");
        // S14：局部失败时告知缺了哪一块，并保留「重试读取历史分镜」入口补齐。
        if (partialFailures.length > 0) {
          setRestoreError(
            `部分历史内容读取失败（${partialFailures.join("、")}），已恢复其余内容，可重试补齐。`,
          );
        }
      } catch (cause: unknown) {
        if (operation !== restoreOperationRef.current) return;
        restoredProjectIdRef.current = undefined;
        setRestoreError(
          customerVisibleErrorMessage(cause, "历史分镜读取失败，请重试。"),
        );
      } finally {
        if (operation === restoreOperationRef.current) {
          restoreBusyRef.current = false;
          setRestoreBusy(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    if (
      promptTypedThisMountRef.current ||
      state.draft.projectId !== project?.id ||
      state.draft.promptEdited !== true
    )
      return;
    promptTextRef.current = state.draft.prompt;
    promptEditedRef.current = true;
    setPromptText(state.draft.prompt);
  }, [
    project?.id,
    state.draft.projectId,
    state.draft.prompt,
    state.draft.promptEdited,
  ]);

  useEffect(() => {
    if (review || !project) return;
    void restoreSavedProject(project);
  }, [project, restoreSavedProject, review]);

  useEffect(
    () => () => {
      replicaOperationRef.current += 1;
    },
    [],
  );

  const replicaProjectId = state.draft.projectId;
  const replicaDuration = normalizeCustomerDuration(state.draft.duration);

  // S13：真实画幅一到手就把草稿画幅对齐过去。首帧已选时以首帧图片为准
  // （i2v 由首帧决定画面比例，用户在首帧面板显式选过 9:16 也要跟随），
  // 否则跟随参考视频；值没变就不写草稿，避免无谓的 inputKey 抖动。
  const alignDraftRatio = (aspect: number) => {
    if (readOnly || review) return;
    const aligned = nearestSourceRatio(aspect);
    if (latestDraftRef.current.ratio !== aligned)
      patchDraft({ ratio: aligned });
  };

  const handleUpload = async (file: File) => {
    if (review || readOnly) {
      notify("审核示例不上传视频。");
      return;
    }
    // S10：选文件后先本地预检，避免整段文件传完、服务端 ffprobe 后才被拒绝。
    // 元数据探测失败只放行——服务端仍会兜底校验，不能因本地读不了就挡住上传。
    // 预检是异步的：先记下当前上传会话，期间用户若已改选来源（「选择已有项目」
    // / 换源等会推进会话号），本次选择在预检完成后整体作废，不抢占新会话。
    const sessionAtPick = uploadOperationRef.current;
    if (file.size > MAX_REPLICA_SOURCE_BYTES) {
      notify("参考视频不能超过 50 MB，请压缩后再上传。");
      return;
    }
    let duration: number | undefined;
    try {
      duration = await readVideoDuration(file);
    } catch {
      duration = undefined;
    }
    if (typeof duration === "number") {
      if (
        duration >
        MAX_REPLICA_SOURCE_SECONDS + REPLICA_DURATION_TOLERANCE_SECONDS
      ) {
        notify("参考视频时长不能超过 15 秒，请裁剪后再上传。");
        return;
      }
      if (
        duration <
        MIN_REPLICA_SOURCE_SECONDS - REPLICA_DURATION_TOLERANCE_SECONDS
      ) {
        notify("参考视频时长不能少于 4 秒，请更换素材。");
        return;
      }
    }
    if (uploadOperationRef.current !== sessionAtPick) return;
    // S11：先记下旧任务的 id / 状态，等上传成功、确认真的换源后再取消；
    // 上传失败时旧任务原样保留，用户仍能看到它的进度或结果。
    const previousTaskId = state.draft.analysisTaskId;
    const previousTaskStatus = state.draft.analysisTaskStatus;
    const operation = ++uploadOperationRef.current;
    promptSaveOperationRef.current += 1;
    setSavingPrompt(false);
    uploadAbortRef.current?.abort();
    restoreSuppressedRef.current = true;
    setRestoreError("");
    const abortController = new AbortController();
    uploadAbortRef.current = abortController;
    notify("正在上传参考视频…");
    try {
      // 进度事件每秒可达数十次：按整十档节流，避免逐条 notify 把整棵树
      // 拖进重渲染。档位只前进不后退，进度回调乱序时也保持单调。
      let notifiedStep = -1;
      const uploaded = await uploadWorkbenchSourceVideo(
        file,
        (percent) => {
          if (operation !== uploadOperationRef.current) return;
          const step = Math.min(
            100,
            Math.max(0, Math.floor(percent / 10) * 10),
          );
          if (step <= notifiedStep) return;
          notifiedStep = step;
          notify(`参考视频上传中 ${step}%`);
        },
        abortController.signal,
      );
      if (operation !== uploadOperationRef.current) return;
      // S11：换源在即，取消旧的排队中 / 运行中拆解任务并即时释放预留积分。
      await cancelPreviousAnalysisTask(previousTaskId, previousTaskStatus);
      restoreOperationRef.current += 1;
      restoreBusyRef.current = false;
      setRestoreBusy(false);
      // S8：换源接管会话——立即释放旧会话的忙碌态，旧任务迟到的轮询收尾
      // （取消失败时可能还要跑很久）不再占着新项目的拆解入口。
      analysisSessionRef.current += 1;
      setAnalysisBusy(false);
      resetReplicaState();
      setPromptText("");
      promptTextRef.current = "";
      promptEditedRef.current = false;
      promptTypedThisMountRef.current = false;
      const blankScript = {
        ...createDraft().script,
        title: uploaded.project?.name ?? file.name,
      };
      patchDraft({
        firstFrameId: undefined,
        firstFrameSelectionVersionId: undefined,
        projectId: uploaded.projectId,
        sourceId: uploaded.assetId,
        sourceAssetId: uploaded.assetId,
        analysisTaskId: uploaded.analysisTaskId,
        analysisTaskStatus: uploaded.analysisTaskStatus,
        prompt: "",
        promptEdited: false,
        script: blankScript,
        scriptEdited: false,
      });
      if (uploaded.project || uploaded.asset) {
        updateData((current) => ({
          ...current,
          projects: uploaded.project
            ? [
                uploaded.project,
                ...current.projects.filter(
                  (project) => project.id !== uploaded.project?.id,
                ),
              ]
            : current.projects,
          assets: uploaded.asset
            ? [
                uploaded.asset,
                ...current.assets.filter(
                  (asset) => asset.id !== uploaded.asset?.id,
                ),
              ]
            : current.assets,
        }));
      }
      restoredProjectIdRef.current = uploaded.projectId;
      restoreSuppressedRef.current = false;
      setStage("ready");
      notify("参考视频已上传，继续拆解会恢复已有任务，不重复创建分析。");
      // 上传即进入复刻流程：来源视频已在手，留在原页只是多一次手工跳转。
      if (state.page !== "replica") navigate("replica");
    } catch (cause: unknown) {
      if (operation !== uploadOperationRef.current) return;
      restoreSuppressedRef.current = false;
      notify(
        customerVisibleErrorMessage(cause, "参考视频上传失败，请稍后重试。"),
      );
    }
  };

  const selectExistingProject = (selectedId: string) => {
    const selected = data.projects.find((item) => item.id === selectedId);
    if (!selected) {
      return;
    }
    uploadOperationRef.current += 1;
    promptSaveOperationRef.current += 1;
    setSavingPrompt(false);
    uploadAbortRef.current?.abort();
    restoreSuppressedRef.current = false;
    restoreOperationRef.current += 1;
    restoredProjectIdRef.current = undefined;
    restoreBusyRef.current = false;
    setRestoreBusy(false);
    resetReplicaState();
    setPromptText("");
    promptTextRef.current = "";
    promptEditedRef.current = false;
    promptTypedThisMountRef.current = false;
    setRestoreError("");
    const blankScript = { ...createDraft().script, title: selected.name };
    patchDraft({
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      analysisTaskId: undefined,
      analysisTaskStatus: undefined,
      projectId: selected.id,
      sourceId: selected.reference_asset_id ?? undefined,
      sourceAssetId: selected.reference_asset_id ?? undefined,
      prompt: "",
      promptEdited: false,
      script: blankScript,
      scriptEdited: false,
    });
    setStage("ready");
    void restoreSavedProject(selected, true).then(() => {
      if (restoredProjectIdRef.current === selected.id)
        notify(`已载入项目「${selected.name}」的历史分镜，可继续编辑。`);
    });
  };

  const startAnalysis = async () => {
    if (review || readOnly) {
      notify("审核示例不调用真实接口。");
      return;
    }
    const projectId = state.draft.projectId;
    const assetId =
      state.draft.sourceAssetId ?? project?.reference_asset_id ?? null;
    if (!projectId || !assetId) {
      notify("请先上传或选择来源视频。");
      return;
    }
    // S9：重新拆解完成会整体覆盖分镜表并清掉未保存标记，等待期里手改的镜头
    // （动作、口播等）会被静默替换。有未保存修改时先让用户明确取舍。
    if (
      shotsDirty &&
      !window.confirm(
        "分镜有未保存的修改，重新拆解完成后会覆盖这些修改。确定继续？如需保留，请先点“保存为自定义”。",
      )
    ) {
      return;
    }
    restoreOperationRef.current += 1;
    restoredProjectIdRef.current = projectId;
    restoreSuppressedRef.current = false;
    restoreBusyRef.current = false;
    setRestoreBusy(false);
    setRestoreError("");
    analysisProjectRef.current = projectId;
    const sessionCurrent = capturePromptSession();
    const isCurrentAnalysis = () =>
      analysisProjectRef.current === projectId &&
      latestDraftRef.current.projectId === projectId &&
      sessionCurrent();
    // S8：会话号 + 等待起点。手动启动与挂载期自动接回共用这一条路径。
    const session = ++analysisSessionRef.current;
    analysisWaitStartedRef.current = undefined;
    setAnalysisBusy(true);
    setAnalysisError("");
    setAnalysisStatus(undefined);
    setStage("analyzing");
    notify("AI 拆解进行中，离开页面后仍可恢复原任务。");
    try {
      // 在途任务一律复用（跨会话自动接回也走这条分支）：已有分镜只说明上一版
      // 结果还在，不代表可以重复提交——新建会为同一份拆解计费两次。SUCCEEDED
      // 却没拿到分镜（视频过短、无有效镜头）时必须强制重跑，否则「重新拆解」
      // 永远复用已完成的任务，陷入拿不到分镜的死循环；FAILED 与「已有分镜的
      // 显式重跑」同样强制新建。
      const taskInFlight =
        state.draft.analysisTaskStatus === "PENDING" ||
        state.draft.analysisTaskStatus === "RUNNING";
      const force =
        !taskInFlight &&
        (shots.length > 0 ||
          Boolean(
            state.draft.analysisTaskId || state.draft.analysisTaskStatus,
          ));
      const task =
        !force && state.draft.analysisTaskId
          ? await getAnalysisTask(state.draft.analysisTaskId)
          : await startVideoAnalysis(projectId, assetId, undefined, force);
      if (!isCurrentAnalysis()) return;
      patchDraft({ analysisTaskId: task.id, analysisTaskStatus: task.status });
      setAnalysisStatus(task.status);
      // S8：接回的任务以服务端 created_at 为等待起点（本机时钟偏差由计时端钳零）。
      const createdAtMs = task.created_at
        ? Date.parse(task.created_at)
        : Number.NaN;
      analysisWaitStartedRef.current = Number.isFinite(createdAtMs)
        ? createdAtMs
        : undefined;
      await waitForAnalysisTask(task.id, (updated) => {
        if (isCurrentAnalysis()) setAnalysisStatus(updated.status);
      });
      if (!isCurrentAnalysis()) {
        return; // 等待期间用户更换了来源视频，丢弃旧项目的拆解结果。
      }
      patchDraft({ analysisTaskStatus: "SUCCEEDED" });
      const analysisVersion = await getLatestProjectAnalysis(projectId).catch(
        () => undefined,
      );
      setAnalysisVersionCreatedAt(analysisVersion?.created_at);
      const analysisShots: ShotCard[] = analysisVersion
        ? (readAnalysisPayload(analysisVersion)?.shots ?? [])
        : [];
      const script = analysisVersion
        ? (readAnalysisPayload(analysisVersion)?.original_script ?? "")
        : "";
      // 分镜由分析 Worker 原子落库，页面只恢复已完成结果。
      const shotVersion = await getLatestProjectShotCards(projectId).catch(
        () => null,
      );
      const shotPayload = shotVersion
        ? (shotVersion.payload as ShotCardPayload)
        : null;
      const shotsFresh =
        shotPayload &&
        shotPayload.source_analysis_version_id === analysisVersion?.id;
      if (!isCurrentAnalysis()) return;
      if (!shotsFresh)
        throw new Error("分镜尚未就绪，请刷新结果；历史项目可重新拆解。");
      const finalShots = shotVersion
        ? ((shotVersion.payload as ShotCardPayload).shots ?? [])
        : analysisShots;
      setShots(finalShots);
      setShotCardVersionId(shotVersion?.id || undefined);
      setAnalysisVersionId(analysisVersion?.id);
      setShotsDirty(false);
      setShotSaveError("");
      setOriginalScript(script);
      // Analysis contains source facts only. Preserve any user-edited final draft.
      // 以内容判断"是否用户改过"：scriptEdited 一旦置位就不会自动复位，
      // 用它做守卫会让后续拆解结果永远回填不进草稿。
      const draftScript = latestDraftRef.current.script;
      const scriptUntouched = draftScript.text === draftScript.original;
      if (
        !draftScript.confirmed &&
        scriptUntouched &&
        draftScript.original !== script
      ) {
        patchDraft({
          script: {
            ...draftScript,
            original: script,
            text: script,
            confirmed: false,
          },
          // 回填的是拆解原文，不是用户改动；显式复位否则下一次守卫会误判为"已编辑"。
          scriptEdited: false,
        });
      }
      invalidateStaleFinalPrompt();
      setStage("ready");
      notify("拆解完成。请确认文案和置换首帧，再合成最终提示词。");
    } catch (cause: unknown) {
      if (!isCurrentAnalysis()) return;
      const taskId = latestDraftRef.current.analysisTaskId;
      if (taskId) {
        const task = await getAnalysisTask(taskId).catch(() => null);
        if (!isCurrentAnalysis()) return;
        if (task?.status === "FAILED")
          patchDraft({ analysisTaskStatus: "FAILED" });
      }
      setStage("ready");
      const message = customerVisibleErrorMessage(
        cause,
        "AI 拆解失败，请稍后重试。",
      );
      // P1-4：失败卡片带「任务编号 + 问题编号」，用户截图报障即可直查。
      const reference = analysisFailureReference(cause);
      const failureText = reference ? `${message}（${reference}）` : message;
      setAnalysisError(failureText);
      notify(failureText);
      // 服务端已给出需要多少积分；把钱包侧栏一并打开，省掉用户自己找入口。
      if (isInsufficientCredits(cause)) openLive("wallet");
    } finally {
      // 守卫提前返回时也要解除忙碌态，否则按钮会永久停在“拆解中”。
      // 已被换源 / 重来接管（会话号前进）时不清：忙碌指示已属于新会话。
      if (analysisSessionRef.current === session) setAnalysisBusy(false);
    }
  };
  startAnalysisRef.current = startAnalysis;

  // S8（拆解进度跨会话恢复）：应用重启 / 重新进入后，草稿里排队中 / 运行中的
  // 任务此前没有任何消费者——界面退回「启动 AI 拆解」，而任务其实在服务端照跑。
  // 挂载后自动接回：走 startAnalysis 的在途分支（getAnalysisTask + 等待终态），
  // 不新建任务、也不重复计费。要等历史恢复结束再跑——本 effect 声明在恢复
  // effect 之后，同一批次里先由它发起恢复并把 restoreBusyRef 置位，因此守卫
  // 必须读 ref 镜像让路（state 要到下一轮渲染才可见；只读 state 会放行接回，
  // startAnalysis 前进 restoreOperationRef 把在途恢复静默作废）；restoreBusy
  // 同时保留：恢复结束置回 false 时触发本 effect 重跑，接回才真正开始。
  useEffect(() => {
    if (
      review ||
      readOnly ||
      analysisBusy ||
      restoreBusy ||
      restoreBusyRef.current
    )
      return;
    if (restoreSuppressedRef.current) return;
    const taskId = state.draft.analysisTaskId;
    const taskStatus = state.draft.analysisTaskStatus;
    if (!taskId || (taskStatus !== "PENDING" && taskStatus !== "RUNNING"))
      return;
    if (autoResumeTaskRef.current === taskId) return;
    if (!project || !(state.draft.sourceAssetId ?? project.reference_asset_id))
      return;
    autoResumeTaskRef.current = taskId;
    void startAnalysisRef.current();
  }, [
    analysisBusy,
    project,
    readOnly,
    restoreBusy,
    review,
    state.draft.analysisTaskId,
    state.draft.analysisTaskStatus,
    state.draft.sourceAssetId,
  ]);

  // customName 为空串时按日期自动命名：供「存入我的提示词」一键直存。
  const saveAsCustomPrompt = async (customName: string) => {
    if (review || readOnly) {
      notify("审核示例不调用真实接口。");
      return;
    }
    const projectId = state.draft.projectId;
    if (!projectId) {
      notify("请先上传或选择来源视频。");
      return;
    }
    setSavingPrompt(true);
    const operation = ++promptSaveOperationRef.current;
    const editVersion = promptEditVersionRef.current;
    const submittedPrompt = promptTextRef.current;
    try {
      await saveGenerationPrompt(projectId, {
        name:
          customName.trim() ||
          `复刻提示词 ${new Date().toLocaleDateString("zh-CN")}`,
        prompt_text: submittedPrompt,
        generation_context: {
          route: "replica",
          project_id: projectId,
          source_asset_id: latestDraftRef.current.sourceAssetId,
          first_frame_asset_id: latestDraftRef.current.firstFrameId,
          duration_seconds: replicaDuration,
          ratio: latestDraftRef.current.ratio as GenerationRatio,
        },
      });
      const stillCurrent =
        operation === promptSaveOperationRef.current &&
        editVersion === promptEditVersionRef.current &&
        latestDraftRef.current.projectId === projectId &&
        promptTextRef.current === submittedPrompt;
      if (!stillCurrent) {
        if (latestDraftRef.current.projectId === projectId)
          notify("提交时的 Prompt 已保存，当前修改仍需再次保存。");
        return;
      }
      // Saving a reusable template does not revert the authoritative local draft.
      patchDraftRef.current({ prompt: submittedPrompt, promptEdited: true });
      notify("已保存到我的提示词，视频生成页可直接导入。");
      setPromptNameOpen(false);
    } catch (cause: unknown) {
      if (operation === promptSaveOperationRef.current)
        notify(
          customerVisibleErrorMessage(
            cause,
            "保存自定义提示词失败，请稍后重试。",
          ),
        );
    } finally {
      if (operation === promptSaveOperationRef.current) setSavingPrompt(false);
    }
  };

  // 复刻链路到此交付提示词；生成与付费在 AI 视频创作页完成。
  // 与按钮 disabled 同源：上游变化会清空 finalReady，这里兜底挡住把过期稿
  // 带进生成页的其他调用路径。
  const goToVideoCreation = () => {
    const text = promptTextRef.current;
    if (!finalReady || !text) {
      notify("请先合成最终提示词，并确保与当前设置一致。");
      return;
    }
    // 立即持久化：跳转后用户可能立刻刷新或退出，这份交付稿不能只停在
    // 2 秒防抖窗口里。
    patchDraft({ prompt: text, promptEdited: true }, { persistNow: true });
    navigate("video");
    notify("最终提示词已带入 AI 视频创作页。");
  };

  const updateShot = (index: number, patch: Partial<ShotCard>) => {
    if (readOnly || savingShots) return;
    setShots((current) =>
      current.map((shot, shotIndex) =>
        shotIndex === index ? { ...shot, ...patch } : shot,
      ),
    );
    setShotsDirty(true);
    setShotSaveError("");
    setFinalSnapshot(null);
    // TODO: H3 草稿功能的 UI 组件
  };

  const saveShotEdits = async () => {
    if (readOnly || savingShots || !analysisVersionId || !shotsDirty) return;
    if (shots.some((shot) => shot.end_time < shot.start_time)) {
      setShotSaveError("结束时间不能早于开始时间。");
      return;
    }
    setSavingShots(true);
    setShotSaveError("");
    const operation = ++shotSaveOperationRef.current;
    const projectId = latestDraftRef.current.projectId;
    try {
      const saved = await saveShotCards(analysisVersionId, shots);
      if (
        operation !== shotSaveOperationRef.current ||
        latestDraftRef.current.projectId !== projectId
      )
        return;
      setShotCardVersionId(saved.id);
      setShotsDirty(false);
      // 分镜换了新版本，之前合成的终稿已不对应，留着只会误导用户去点送生成。
      notify(
        invalidateStaleFinalPrompt()
          ? "分镜已保存；上游内容已变化，请重新合成最终提示词。"
          : "分镜已保存。",
      );
    } catch (cause: unknown) {
      if (
        operation !== shotSaveOperationRef.current ||
        latestDraftRef.current.projectId !== projectId
      )
        return;
      setShotSaveError(customerVisibleErrorMessage(cause, "分镜保存失败。"));
    } finally {
      if (operation === shotSaveOperationRef.current) setSavingShots(false);
    }
  };

  const sourceDuration = displayShots.reduce(
    (duration, shot) => Math.max(duration, shot.end_time),
    0,
  );
  // 输出时长跟随源视频。复刻页没有时长控件（时长下拉框属于独立创作页的
  // ParameterControls），草稿默认的 8 秒对用户既不可见也不可改；源视频在上传时
  // 已被限定为 4–15 秒，与 output_duration_seconds 的契约值域完全重合，所以按源
  // 时长生成总是可行的。不对齐的话，15 秒的源在默认 8 秒下必然撞上压缩门禁，而
  // 用户手上没有任何把时长调上去的入口。向上取整是为了让对齐后的值不会反过来
  // 落进 PromptEditor 那 0.25 秒容差的压缩判定里。
  const alignedDuration = sourceDuration
    ? normalizeCustomerDuration(Math.ceil(sourceDuration))
    : 0;
  useEffect(() => {
    if (readOnly || !alignedDuration) return;
    if (state.draft.duration === alignedDuration) return;
    // 走 ref：patchDraft 每次 render 都是新引用，直接依赖会让本 effect 自激。
    patchDraftRef.current({ duration: alignedDuration });
  }, [alignedDuration, readOnly, state.draft.duration]);
  const analysisCheck: ReplicaPreflightCheck = {
    id: "analysis-ready",
    label: "AI 视频拆解",
    blocking: false,
    passed: hasShots && !analysisBusy && !analysisError && !restoreError,
    reason: analysisBusy
      ? "视频仍在拆解，请等待分镜读取完成。"
      : analysisError || restoreError
        ? "拆解结果暂不可用，请按上方错误提示重试。"
        : "尚未生成可用分镜，请先完成 AI 视频拆解。",
  };
  const shotSaveCheck: ReplicaPreflightCheck = {
    id: "shot-edits-saved",
    label: "分镜保存",
    blocking: false,
    passed:
      Boolean(shotCardVersionId) &&
      !shotsDirty &&
      !savingShots &&
      !shotSaveError,
    reason: savingShots
      ? "正在保存分镜，请稍候。"
      : shotSaveError ||
        (shotsDirty
          ? "分镜有未保存修改，请先点击“保存为自定义”。"
          : "尚无有效分镜版本，请先完成视频拆解。"),
  };
  const workflowChecks: ReplicaPreflightCheck[] = [
    {
      id: "workflow-source",
      label: "来源视频",
      passed: Boolean(replicaProjectId && state.draft.sourceAssetId),
      reason: "请先上传参考视频或选择已有项目。",
    },
    analysisCheck,
    shotSaveCheck,
    {
      id: "workflow-first-frame",
      label: "首帧选择",
      passed: Boolean(state.draft.firstFrameId),
      reason: "请完成首帧置换并选定一张图片。",
    },
    {
      id: "workflow-script",
      label: "口播文案",
      passed: state.draft.script.confirmed,
      // 与合成控件同口径：口播确认只作建议（#165 已明确“只提醒，不阻止合成
      // 最终提示词”）。页面级清单把它算成硬性缺失会让用户先被告知“还差
      // 这一项”，进到合成区却发现不确认也能合成。
      blocking: false,
      reason: state.draft.script.text.trim()
        ? "请在口播文案区域点击“确认”。"
        : "请确认本视频无口播。",
    },
    {
      id: "workflow-final",
      label: "最终提示词",
      passed: finalReady,
      reason: "前置项目完成后，请合成并核对最终提示词。",
    },
  ];
  const copyShotTable = async () => {
    if (!displayShots.length) {
      notify("暂无可复制的分镜表。");
      return;
    }
    try {
      await navigator.clipboard.writeText(
        formatShotCardsForClipboard(displayShots),
      );
      notify("分镜表已复制，可直接粘贴到 Excel 或在线表格。");
    } catch {
      notify("复制分镜表失败，请检查剪贴板权限后重试。");
    }
  };
  // 复刻页 ①：三段式长页的步骤导航，滚动中吸顶可见，点击跳转到对应区块。
  const replicaSteps = [
    { id: "replica-step-1", label: "1 视频拆解", done: hasShots },
    {
      id: "replica-step-2",
      label: "2 首帧置换",
      done: Boolean(state.draft.firstFrameId),
    },
    { id: "replica-step-3", label: "3 文案与生成", done: finalReady },
  ];
  const currentStepIndex = replicaSteps.findIndex((step) => !step.done);
  // hash 路由下 href="#…" 会改写页面路由，步骤跳转只用 JS 滚动。
  const scrollToReplicaStep = (id: string) => {
    document.getElementById(id)?.scrollIntoView({
      behavior: "smooth",
      block: "start",
    });
  };
  // ② 信息卡的「末次拆解」：当天显示「今天 HH:mm」，跨天带月日。
  const analysisCreatedLabel = (() => {
    if (!analysisVersionCreatedAt) return "—";
    const created = new Date(analysisVersionCreatedAt);
    if (Number.isNaN(created.getTime())) return "—";
    const time = created.toLocaleTimeString("zh-CN", {
      hour: "2-digit",
      hour12: false,
      minute: "2-digit",
    });
    return created.toDateString() === new Date().toDateString()
      ? `今天 ${time}`
      : `${created.getMonth() + 1}月${created.getDate()}日 ${time}`;
  })();
  // 「首帧未就绪」在面板内（有文案没首帧）和面板外（两者都没有）两处出现：
  // 分支结构不同、位置也不同，共用一份文案，免得两处措辞各自漂移。
  const missingFirstFrameHint = (
    <Hint>请先完成首帧置换并选定图片，再合成最终提示词。</Hint>
  );
  return (
    <section className="creation-page creation-replica">
      <CreationNavigation />
      <nav className="replica-steps" aria-label="复刻步骤导航">
        {replicaSteps.map((step, index) => (
          <button
            key={step.id}
            type="button"
            className={
              step.done
                ? "replica-steps__item is-done"
                : index === currentStepIndex
                  ? "replica-steps__item is-current"
                  : "replica-steps__item"
            }
            aria-current={index === currentStepIndex ? "step" : undefined}
            onClick={() => scrollToReplicaStep(step.id)}
          >
            <span className="replica-steps__dot" aria-hidden="true">
              {step.done ? "✓" : index + 1}
            </span>
            {step.label}
          </button>
        ))}
      </nav>
      <div className="creation-replica-flow">
        <section className="creation-workflow-section" id="replica-step-1">
          <h2>1 视频拆解</h2>
          {stage === "source" && !project ? (
            <Panel className="creation-empty-workspace">
              <Empty
                title="先导入参考视频"
                description="上传视频后拆解分镜与文案，选定新首帧后合成最终提示词；也可以选择已有项目继续。"
                action={
                  <div className="creation-upload-row">
                    <Button
                      disabled={readOnly}
                      variant="primary"
                      onClick={() => uploadInputRef.current?.click()}
                    >
                      上传参考视频
                    </Button>
                    {data.projects.length > 0 && (
                      <select
                        aria-label="选择已有项目"
                        className="creation-project-select"
                        defaultValue=""
                        disabled={readOnly}
                        onChange={(event) => {
                          if (event.target.value) {
                            selectExistingProject(event.target.value);
                          }
                        }}
                      >
                        <option value="" disabled>
                          选择已有项目
                        </option>
                        {data.projects.map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                    )}
                  </div>
                }
              />
            </Panel>
          ) : (
            <>
              <ReplicaMediaRow ratio={previewRatio}>
                <Panel className="creation-replica-video">
                  <div className="creation-panel-title-row">
                    <span>参考视频</span>
                  </div>
                  {/* 不显示来源标注：平台原标题对复刻工作没有信息价值，项目名
                      已在右侧「拆解控制」面板给出定位。 */}
                  <div className="media-frame">
                    <Media
                      asset={source}
                      alt="参考视频"
                      className="creation-replica-video__media"
                      onAspectRatioChange={(ratio) => {
                        setSourceRatio({ source: sourceMediaKey, ratio });
                        // 首帧已选时它的真实比例优先，源视频只在没有首帧时兜底。
                        if (!state.draft.firstFrameId) alignDraftRatio(ratio);
                      }}
                      presentation="video"
                      aspectRatio="adaptive"
                    />
                  </div>
                </Panel>
                <Panel className="creation-replica-analyze">
                  <div className="creation-panel-title-row">
                    <span>拆解控制</span>
                    <span className="creation-replica-meta">
                      <span className="creation-replica-project-name">
                        {project?.name}
                      </span>
                      {displayShots.length > 0 && (
                        <small>已拆解 {displayShots.length} 个镜头</small>
                      )}
                    </span>
                  </div>
                  <div className="creation-replica-stats">
                    <div className="creation-replica-stat is-gold">
                      <span>视频时长</span>
                      <strong>
                        {sourceDuration > 0
                          ? `${sourceDuration.toFixed(1)} 秒`
                          : "—"}
                      </strong>
                    </div>
                    <div className="creation-replica-stat is-gold">
                      <span>拆解镜头</span>
                      <strong>
                        {displayShots.length > 0
                          ? `${displayShots.length} 个`
                          : "—"}
                      </strong>
                    </div>
                    <div className="creation-replica-stat">
                      <span>末次拆解</span>
                      <strong title={analysisVersionCreatedAt ?? undefined}>
                        {analysisCreatedLabel}
                      </strong>
                    </div>
                  </div>
                  <div className="creation-upload-row">
                    <Button
                      variant="primary"
                      disabled={readOnly || analysisBusy}
                      onClick={() => void startAnalysis()}
                    >
                      {analysisBusy
                        ? "AI 拆解进行中…"
                        : displayShots.length > 0
                          ? "重新拆解"
                          : "启动 AI 拆解"}
                    </Button>
                    {/* S8：换源 / 重来是拆解进行中的取消式出口（S11 保证它们
                        会取消在途任务），不能被忙碌态锁死；只有拆解按钮自身
                        禁用，防重复提交。 */}
                    <Button
                      disabled={readOnly}
                      variant="outline"
                      onClick={() => uploadInputRef.current?.click()}
                    >
                      更换来源视频
                    </Button>
                    <Button
                      disabled={readOnly}
                      variant="quiet"
                      onClick={startFreshReplica}
                    >
                      开始新的复刻
                    </Button>
                  </div>
                  {restoreBusy && (
                    <Hint>正在读取已保存的分镜、文案和 Prompt…</Hint>
                  )}
                  {analysisBusy && (
                    <p role="status" aria-live="polite">
                      {analysisStatus === "PENDING"
                        ? "拆解任务已排队，等待开始"
                        : analysisStatus === "RUNNING"
                          ? "正在分析视频画面与口播"
                          : analysisStatus === "SUCCEEDED"
                            ? "拆解已完成，正在读取分镜"
                            : "正在连接拆解服务"}
                      {` · 已等待 ${Math.floor(analysisElapsed / 60)}分${analysisElapsed % 60}秒。`}
                      视频拆解可能需要数分钟，请勿重复提交。
                    </p>
                  )}
                  {analysisError && (
                    <div className="creation-inline-error" role="alert">
                      {analysisError}
                    </div>
                  )}
                  {restoreError && (
                    <div className="creation-inline-error" role="alert">
                      <span>{restoreError}</span>
                      <Button
                        variant="outline"
                        onClick={() => {
                          if (project) void restoreSavedProject(project, true);
                        }}
                      >
                        重试读取历史分镜
                      </Button>
                    </div>
                  )}
                </Panel>
              </ReplicaMediaRow>
              {hasShots && (
                <Panel className="creation-shot-list">
                  <div className="creation-panel-title-row">
                    <span className="creation-shot-list-title">
                      分镜表 · {displayShots.length} 个镜头
                      {sourceDuration > 0 ? (
                        <small>时间轴 0.0–{sourceDuration.toFixed(1)} 秒</small>
                      ) : null}
                    </span>
                    <span className="creation-shot-list-actions">
                      {/* 有已保存版本且无未保存改动，才算「已保存为自定义」。 */}
                      {!shotsDirty && shotCardVersionId ? (
                        <small>已保存为自定义 ✓</small>
                      ) : null}
                      <Button
                        disabled={displayShots.length === 0}
                        onClick={() => void copyShotTable()}
                        variant="outline"
                      >
                        复制分镜表
                      </Button>
                      <Button
                        disabled={readOnly || savingShots || !shotsDirty}
                        onClick={() => void saveShotEdits()}
                        variant="outline"
                      >
                        {savingShots ? "保存中…" : "保存为自定义"}
                      </Button>
                    </span>
                  </div>
                  {shotSaveError ? (
                    <p className="settings-error" role="alert">
                      {shotSaveError}
                    </p>
                  ) : null}
                  <details className="creation-shot-details">
                    <summary>展开编辑完整分镜</summary>
                    <ShotCardEditor
                      shots={displayShots}
                      readOnly={readOnly || savingShots}
                      onChange={(index, shot) => updateShot(index, shot)}
                    />
                  </details>
                </Panel>
              )}
            </>
          )}
        </section>
        <section className="creation-workflow-section" id="replica-step-2">
          <h2>2 首帧置换</h2>
          <ReplicaFirstFrameSection
            ratio={firstFramePreviewRatio}
            videoDurationSeconds={sourceDuration || null}
          />
        </section>
        <section className="creation-workflow-section" id="replica-step-3">
          <h2>3 文案与生成</h2>
          {!replicaProjectId || !state.draft.firstFrameId ? (
            <ReplicaPreflightChecklist checks={workflowChecks} />
          ) : null}
          {/* 门禁按项目判定：拆解未完成时也要挂载口播文案占位，否则用户看不到还差哪一步。 */}
          {!replicaProjectId ? (
            <Hint>先完成视频拆解，再确认首帧与文案。</Hint>
          ) : (
            <>
              <ReplicaMediaRow ratio={firstFramePreviewRatio}>
                <Panel className="creation-final-preview">
                  <div className="creation-panel-title-row">
                    <span>已选首帧</span>
                  </div>
                  <div className="media-frame">
                    <Media
                      asset={selectedFirstFrame}
                      alt="已选首帧"
                      aspectRatio="adaptive"
                      className="creation-final-preview__media"
                      onAspectRatioChange={(ratio) => {
                        setFirstFrameRatio({
                          source: firstFrameMediaKey,
                          ratio,
                        });
                        alignDraftRatio(ratio);
                      }}
                    />
                  </div>
                </Panel>
                <div className="creation-final-copy">
                  <ReplicaNarration />
                </div>
              </ReplicaMediaRow>
              {/* 最终提示词独立成横向整宽框：首帧与口播先齐平排布，提示词
                在其下方整宽展示；上游变化但未重新合成时在标题行给出显性
                标记，避免"上游变了、正文还是旧稿"的静默错位。 */}
              {/* 提示词渲染与「合成」是两条独立的可用性：拆解完成即有文案可看，
                但必须选定首帧才能合成，故门禁分开判定。 */}
              {state.draft.firstFrameId || promptText ? (
                <Panel className="creation-prompt-output">
                  <div className="creation-panel-title-row">
                    <span>最终提示词</span>
                    {displayShots.length === 0 ? (
                      <small>确认文案与首帧后合成</small>
                    ) : promptText && !finalReady ? (
                      <small>上游已变化，请重新合成</small>
                    ) : null}
                  </div>
                  {/* 单一文本框：展示与编辑合一，不再另设只读渲染与折叠编辑框。
                      无首帧时也展示（配合占位提示），有首帧后才出现合成控件。 */}
                  <PromptEditor
                    label="最终提示词"
                    readOnly={readOnly}
                    optimizationDisabled={review}
                    scope={`${user.id}:${state.draft.projectId ?? ""}`}
                    context={{
                      route: "replica",
                      project_id: state.draft.projectId,
                      source_asset_id: state.draft.sourceAssetId,
                      shot_card_version_id: shotCardVersionId,
                      script_version_id: finalSnapshot?.scriptVersionId,
                      first_frame_asset_id: state.draft.firstFrameId,
                      duration_seconds: replicaDuration,
                      ratio: state.draft.ratio as GenerationRatio,
                    }}
                    rows={12}
                    value={promptText}
                    onChange={(text) => {
                      compiledPromptRef.current = false;
                      setPromptText(text);
                      promptTextRef.current = text;
                      promptEditedRef.current = true;
                      promptTypedThisMountRef.current = true;
                      promptEditVersionRef.current += 1;
                      patchDraft({
                        prompt: text,
                        promptEdited: true,
                      });
                    }}
                    placeholder="确认文案和新首帧后合成最终提示词。"
                  />
                  {state.draft.firstFrameId ? (
                    <ReplicaFinalPromptControls
                      input={finalInput}
                      sourceDuration={sourceDuration}
                      sourceFrameTimestamp={
                        state.draft.sourceFrameTimestampSeconds
                      }
                      showScriptPreview={false}
                      scriptConfirmed={state.draft.script.confirmed}
                      upstreamChecks={[analysisCheck, shotSaveCheck]}
                      value={promptText}
                      snapshot={finalSnapshot}
                      onPrepared={(snapshot) => {
                        compiledPromptRef.current = true;
                        applyFinalSnapshot(snapshot);
                      }}
                      readOnly={readOnly || review}
                      restoreEnabled={!review}
                      onChange={(text) => {
                        compiledPromptRef.current = false;
                        setPromptText(text);
                        promptTextRef.current = text;
                        promptEditedRef.current = true;
                        promptTypedThisMountRef.current = true;
                        promptEditVersionRef.current += 1;
                        patchDraft({ prompt: text, promptEdited: true });
                      }}
                    />
                  ) : (
                    missingFirstFrameHint
                  )}
                  <div className="creation-upload-row">
                    {promptNameOpen ? (
                      <>
                        <input
                          aria-label="自定义提示词名称"
                          className="creation-project-select"
                          disabled={readOnly || review}
                          onChange={(event) =>
                            setPromptName(event.target.value)
                          }
                          placeholder="提示词名称"
                          value={promptName}
                        />
                        <Button
                          disabled={readOnly || review || savingPrompt}
                          onClick={() => void saveAsCustomPrompt(promptName)}
                          variant="primary"
                        >
                          {savingPrompt ? "保存中…" : "确认保存"}
                        </Button>
                        <Button
                          onClick={() => setPromptNameOpen(false)}
                          variant="quiet"
                        >
                          取消
                        </Button>
                      </>
                    ) : (
                      <>
                        <Button
                          disabled={readOnly || review}
                          onClick={() => setPromptNameOpen(true)}
                          variant="outline"
                        >
                          保存为自定义提示词
                        </Button>
                        <Button
                          disabled={
                            readOnly || review || savingPrompt || !promptText
                          }
                          onClick={() => void saveAsCustomPrompt("")}
                          variant="outline"
                        >
                          {savingPrompt ? "保存中…" : "存入我的提示词"}
                        </Button>
                        {/* ⑧ 主次分层：右组携带就绪徽标，「去 AI 视频创作」是唯一主按钮。 */}
                        <span className="creation-prompt-actions">
                          {finalReady ? (
                            <span className="creation-final-badge">
                              ✓ 已就绪 · 上游一致
                            </span>
                          ) : (
                            // S3 门禁的可见原因：禁用按钮的原生 title 在触屏与
                            // 键盘操作下不可达，补一个与 title 同源的提示，
                            // 并用 aria-describedby 关联到按钮。
                            <span
                              className="creation-final-badge is-pending"
                              id="replica-final-gate-hint"
                            >
                              请先合成最终提示词，并确保与当前设置一致。
                            </span>
                          )}
                          <Button
                            aria-describedby={
                              finalReady ? undefined : "replica-final-gate-hint"
                            }
                            disabled={readOnly || review || !finalReady}
                            onClick={goToVideoCreation}
                            title={
                              finalReady
                                ? undefined
                                : "请先合成最终提示词，并确保与当前设置一致。"
                            }
                            variant="primary"
                          >
                            去 AI 视频创作
                          </Button>
                        </span>
                      </>
                    )}
                  </div>
                </Panel>
              ) : (
                missingFirstFrameHint
              )}
            </>
          )}
        </section>
      </div>
      <input
        accept="video/mp4,video/quicktime"
        aria-label="上传参考视频"
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void handleUpload(file);
          event.target.value = "";
        }}
        ref={uploadInputRef}
        type="file"
      />
    </section>
  );
}

function ReplicaFirstFrameSection({
  ratio,
  videoDurationSeconds,
}: {
  /** 首帧素材宽高比，供媒体行推算左栏宽度；未知时为 9:16。 */
  ratio: number;
  videoDurationSeconds: number | null;
}) {
  const { state, data, review, patchDraft, notify, updateData, user } =
    useStudio();
  const readOnly = user.role === "auditor";
  const project = data.projects.find(
    (item) => item.id === state.draft.projectId,
  );
  const [characterSelection, setCharacterSelection] =
    useState<ProjectMainCharacter | null>(null);
  const [sourceFrameSelection, setSourceFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [referenceSelection, setReferenceSelection] =
    useState<CharacterReferenceSelection | null>(null);
  const [referenceError, setReferenceError] = useState("");
  const [referenceMatching, setReferenceMatching] = useState(false);
  const [referenceMatchRevision, setReferenceMatchRevision] = useState(0);
  const [referenceInputRevision, setReferenceInputRevision] = useState(0);
  const [firstFrameSelection, setFirstFrameSelection] =
    useState<AnalysisVersion | null>(null);
  const [, setLeafBusy] = useState(false);
  const referenceMatchInFlightRef = useRef<string | undefined>(undefined);
  const referenceRetryScheduledRef = useRef(false);
  const readOnlyRef = useRef(readOnly);
  readOnlyRef.current = readOnly;
  const characterVersionIdRef = useRef<string | null | undefined>(undefined);
  const sourceFrameSelectionIdRef = useRef<string | undefined>(undefined);
  const referenceMatchPromiseRef = useRef<
    | {
        key: string;
        promise: ReturnType<typeof selectCharacterReferences>;
      }
    | undefined
  >(undefined);
  // patchDraft 每次壳层渲染都是新引用，effect 依赖一律走 ref，避免无限置位循环。
  const patchDraftRef = useRef(patchDraft);
  patchDraftRef.current = patchDraft;
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const updateDataRef = useRef(updateData);
  updateDataRef.current = updateData;
  const draftRef = useRef(state.draft);
  draftRef.current = state.draft;
  const confirmedSelectionKeyRef = useRef<string | undefined>(undefined);
  const previousProjectIdRef = useRef<string | undefined>(undefined);
  const projectEffectMountedRef = useRef(false);
  /** 草稿素材补取的世代号：换项目或重选首帧后，旧请求的结果都作废。 */
  const draftMaterialsRequestRef = useRef(0);

  const firstFrameAssetId = firstFrameSelection
    ? (readFirstFrameSelectionPayload(firstFrameSelection)
        ?.first_frame_asset_id ?? null)
    : null;

  const clearConfirmedFirstFrame = useCallback(() => {
    if (readOnlyRef.current) return;
    confirmedSelectionKeyRef.current = undefined;
    patchDraftRef.current({
      firstFrameId: undefined,
      firstFrameSelectionVersionId: undefined,
      frameConfirmed: false,
    } as Partial<StudioDraft>);
  }, []);

  // 只认真实存在于 data.projects 的项目。早先会在列表非空时拿草稿里的 id 兜底，
  // 但那个 id 可能已经被删/换号，于是用一份不存在的项目渲染出空壳界面（缺陷 A4）。
  const projectId = project?.id;
  const referenceAssetId =
    project?.reference_asset_id ?? state.draft.sourceAssetId ?? null;
  // 换项目时重置全部下游状态与草稿中的旧首帧。
  useEffect(() => {
    const projectChanged =
      projectEffectMountedRef.current &&
      previousProjectIdRef.current !== projectId;
    projectEffectMountedRef.current = true;
    previousProjectIdRef.current = projectId;
    setCharacterSelection(null);
    setSourceFrameSelection(null);
    setReferenceSelection(null);
    setFirstFrameSelection(null);
    setReferenceError("");
    setReferenceMatching(false);
    referenceMatchInFlightRef.current = undefined;
    referenceRetryScheduledRef.current = false;
    characterVersionIdRef.current = undefined;
    sourceFrameSelectionIdRef.current = undefined;
    confirmedSelectionKeyRef.current = undefined;
    draftMaterialsRequestRef.current += 1;
    if (projectChanged && projectId && !review && !readOnlyRef.current) {
      patchDraftRef.current({
        firstFrameId: undefined,
        firstFrameSelectionVersionId: undefined,
        sourceFrameSelectionVersionId: undefined,
        sourceFrameTimestampSeconds: undefined,
        frameConfirmed: false,
      } as Partial<StudioDraft>);
    }
  }, [projectId, review]);

  const characterVersionId = characterSelection?.character_version_id ?? "";
  const sourceFrameSelectionId = sourceFrameSelection?.id ?? "";
  const referenceSelectionId = referenceSelection?.id ?? "";

  // 人物参考自动匹配：仅业务输入或明确重试变化时重新请求。
  useEffect(() => {
    if (
      review ||
      readOnly ||
      !projectId ||
      !characterVersionId ||
      !sourceFrameSelectionId ||
      referenceSelectionId
    ) {
      setReferenceMatching(false);
      referenceRetryScheduledRef.current = false;
      return;
    }
    const matchKey = `${projectId}:${characterVersionId}:${sourceFrameSelectionId}`;
    const requestKey = `${matchKey}:${referenceInputRevision}:${referenceMatchRevision}`;
    let active = true;
    referenceMatchInFlightRef.current = requestKey;
    setReferenceMatching(true);
    const existing = referenceMatchPromiseRef.current;
    const promise =
      existing?.key === requestKey
        ? existing.promise
        : selectCharacterReferences(projectId, {
            character_version_id: characterVersionId,
            source_frame_selection_version_id: sourceFrameSelectionId,
          });
    referenceMatchPromiseRef.current = { key: requestKey, promise };
    promise
      .then((selection) => {
        if (active) {
          setReferenceSelection(selection);
          setReferenceError("");
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setReferenceError(
            cause instanceof Error ? cause.message : "自动匹配人物参考失败。",
          );
        }
      })
      .finally(() => {
        if (!active) return;
        if (referenceMatchInFlightRef.current === requestKey)
          referenceMatchInFlightRef.current = undefined;
        referenceRetryScheduledRef.current = false;
        setReferenceMatching(false);
      });
    return () => {
      active = false;
      if (referenceMatchInFlightRef.current === requestKey) {
        referenceMatchInFlightRef.current = undefined;
        referenceRetryScheduledRef.current = false;
      }
    };
  }, [
    review,
    readOnly,
    projectId,
    characterVersionId,
    sourceFrameSelectionId,
    referenceSelectionId,
    referenceInputRevision,
    referenceMatchRevision,
  ]);

  // 叶子组件的 effect 依赖回调身份：必须 useCallback 保持稳定，否则引发重取风暴。
  const handleCharacterChange = useCallback(
    (selection: ProjectMainCharacter | null) => {
      const nextVersionId = selection?.character_version_id;
      if (characterVersionIdRef.current === nextVersionId) return;
      characterVersionIdRef.current = nextVersionId;
      setCharacterSelection(selection);
      setReferenceInputRevision((revision) => revision + 1);
      setReferenceSelection(null);
      setReferenceError("");
      setFirstFrameSelection(null);
      clearConfirmedFirstFrame();
    },
    [clearConfirmedFirstFrame],
  );

  const handleSourceFrameChange = useCallback(
    (selection: AnalysisVersion | null) => {
      const nextSelectionId = selection?.id;
      if (sourceFrameSelectionIdRef.current === nextSelectionId) return;
      sourceFrameSelectionIdRef.current = nextSelectionId;
      setSourceFrameSelection(selection);
      if (!readOnlyRef.current) {
        patchDraftRef.current({
          sourceFrameSelectionVersionId: nextSelectionId,
          sourceFrameTimestampSeconds: sourceFrameTimestamp(selection),
        });
      }
      setReferenceInputRevision((revision) => revision + 1);
      setReferenceSelection(null);
      setReferenceError("");
      setFirstFrameSelection(null);
      clearConfirmedFirstFrame();
    },
    [clearConfirmedFirstFrame],
  );

  const handleFirstFrameChange = useCallback(
    (selection: AnalysisVersion | null) => {
      setFirstFrameSelection(selection);
      if (readOnlyRef.current) return;
      if (!selection) {
        clearConfirmedFirstFrame();
        return;
      }
      const assetId =
        readFirstFrameSelectionPayload(selection)?.first_frame_asset_id;
      if (!assetId) {
        clearConfirmedFirstFrame();
        return;
      }
      const selectionKey = `${selection.id}:${assetId}`;
      if (confirmedSelectionKeyRef.current === selectionKey) return;
      confirmedSelectionKeyRef.current = selectionKey;
      const firstFramePatch = {
        firstFrameId: assetId,
        firstFrameSelectionVersionId: selection.id,
        frameConfirmed: true,
      };
      patchDraftRef.current(firstFramePatch);
      // 确认首帧此前只写了 id，asset 从未注册进 data.assets，「已选首帧」与左栏就
      // 永远取不到图。复用草稿素材读取（会解析 firstFrameId 并取签名地址）补上，
      // 按 id 合并以免覆盖已有地址。
      const requestId = ++draftMaterialsRequestRef.current;
      void loadDraftMaterials({ ...draftRef.current, ...firstFramePatch })
        .then((loaded) => {
          if (draftMaterialsRequestRef.current !== requestId) return;
          if (!loaded.assets.length) return;
          updateDataRef.current((current) => ({
            ...current,
            assets: mergeStudioAssets(current.assets, loaded.assets),
          }));
        })
        .catch(() => {
          // 过期请求的失败与当前首帧无关，弹提示会把用户误导去重选一次。
          if (draftMaterialsRequestRef.current !== requestId) return;
          // 已确认的首帧不因取图失败回滚；下次进页面或重选首帧会再取一次。
          notifyRef.current("首帧预览暂时加载失败，重新选择首帧可重试。");
        });
    },
    [clearConfirmedFirstFrame],
  );

  const retryReferenceMatch = () => {
    if (
      readOnly ||
      referenceMatching ||
      referenceMatchInFlightRef.current ||
      referenceRetryScheduledRef.current
    )
      return;
    referenceRetryScheduledRef.current = true;
    setReferenceError("");
    setReferenceMatchRevision((revision) => revision + 1);
  };

  const reviewOriginal =
    findAsset(data.assets, state.draft.sourceAssetId) ??
    data.assets.find((asset) => asset.name.includes("原始画面"));
  const reviewScene =
    findAsset(data.assets, state.draft.imageId) ??
    data.assets.find((asset) => asset.group === "人物照片");
  const reviewSceneOptions = data.people.flatMap((person) =>
    data.assets
      .filter(
        (asset) =>
          asset.kind === "image" &&
          asset.personId === person.id &&
          asset.source === "人物库场景造型" &&
          !asset.composite,
      )
      .map((asset) => ({
        id: asset.id,
        label: `${person.name}·${asset.name.replace(/\.[^.]+$/, "")}`,
      })),
  );
  const reviewCandidates = [
    {
      id: "confirmed",
      asset: findAsset(data.assets, state.draft.firstFrameId),
    },
    { id: "scene", asset: reviewScene },
    {
      id: "reference",
      asset:
        data.assets.find((asset) => asset.group === "参考素材") ??
        reviewOriginal,
    },
  ];

  return (
    <div className="creation-replacement">
      {!projectId ? (
        <Panel className="creation-empty-workspace">
          <Empty
            title="先在视频复刻中准备好项目"
            description="人物替换需要一个已完成 AI 拆解的项目：选择下方项目即可开始提取源画面并生成置换首帧。"
            action={
              data.projects.length > 0 ? (
                <select
                  aria-label="选择项目"
                  className="creation-project-select"
                  defaultValue=""
                  disabled={readOnly}
                  onChange={(event) => {
                    if (readOnly) return;
                    const selected = data.projects.find(
                      (item) => item.id === event.target.value,
                    );
                    if (selected) {
                      patchDraft({ projectId: selected.id });
                      notify(`已切换到项目「${selected.name}」。`);
                    }
                  }}
                >
                  <option value="" disabled>
                    选择已有项目
                  </option>
                  {data.projects
                    .filter((item) => item.analysis_status === "READY")
                    .map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                </select>
              ) : undefined
            }
          />
        </Panel>
      ) : review ? (
        <>
          <Hint>审核示例 · 只读</Hint>
          {/* 审核示例必须与生产分支同构（三带 + .media-frame），否则复刻页在
              审核入口下的真实布局无法被验证；这里只把真实组件换成静态示例数据。 */}
          <Panel className="creation-replacement-source">
            <div className="creation-panel-title-row">
              <span>源画面</span>
            </div>
            <div className="media-frame">
              <Media
                asset={reviewOriginal}
                alt="审核示例原画面"
                aspectRatio="adaptive"
              />
            </div>
          </Panel>
          {/* 与生产分支同构：两栏媒体框都钉成参考视频画幅，不再跟随素材比例。 */}
          <ReplicaMediaRow
            className="media-row--scene"
            minHeight={SCENE_ROW_MIN_HEIGHT}
            ratio={ratio}
          >
            <Panel
              className="creation-replacement-step banded"
              style={panelRatioStyle(ratio)}
            >
              <div className="creation-panel-title-row">
                <span>场景形象</span>
              </div>
              <section className="flow-character-row flow-character-row--banded">
                <div className="band-ctrl">
                  <select
                    aria-label="人物场景形象"
                    className="creation-project-select"
                    disabled
                    value={reviewScene?.id ?? ""}
                  >
                    {reviewSceneOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="media-frame">
                  <Media
                    asset={reviewScene}
                    alt="审核示例场景图"
                    aspectRatio="adaptive"
                  />
                </div>
                <div className="band-act" />
              </section>
            </Panel>
            <Panel
              className="creation-replacement-step banded"
              style={panelRatioStyle(ratio)}
            >
              <div className="creation-panel-title-row">
                <span>置换首帧</span>
              </div>
              <section className="first-frame-selection first-frame-selection--compact first-frame-selection--banded">
                {/* 首个子元素必须是标题块：creation.css 用它做视觉隐藏，
                    同时把它排除在带布局之外，示例这里也要保持同构。 */}
                <div>
                  <h3 id="first-frame-title">人物置换首帧</h3>
                </div>
                <div className="band-ctrl">
                  <select
                    aria-label="画面比例"
                    className="creation-project-select"
                    disabled
                    value="9:16"
                  >
                    <option value="9:16">9:16 竖屏</option>
                  </select>
                </div>
                <div className="first-frame-media-band">
                  <div className="media-frame">
                    <Media
                      asset={reviewCandidates[0]?.asset}
                      alt="审核示例候选 1"
                      aspectRatio="adaptive"
                    />
                  </div>
                  <div className="creation-review-candidates">
                    {reviewCandidates.map(({ id, asset }, index) => (
                      <figure key={id}>
                        <Media
                          asset={asset}
                          alt={`审核示例候选 ${index + 1}`}
                          aspectRatio="adaptive"
                        />
                        <figcaption>候选 {index + 1}</figcaption>
                      </figure>
                    ))}
                  </div>
                </div>
                <div className="band-act center">
                  <span className="status-note">审核示例</span>
                </div>
              </section>
            </Panel>
          </ReplicaMediaRow>
        </>
      ) : (
        <>
          {/* 源画面自带三个候选帧与取帧工具，独占一行横排，不再挤在置换首帧左栏。 */}
          <Panel className="creation-replacement-source">
            <div className="creation-panel-title-row">
              <span>源画面</span>
            </div>
            <SourceFrameSelection
              candidatesAlwaysVisible
              onBusyChange={setLeafBusy}
              onSelectionChange={handleSourceFrameChange}
              projectId={projectId}
              readOnly={readOnly}
              referenceAssetId={referenceAssetId}
              simplified
              videoDurationSeconds={videoDurationSeconds}
            />
          </Panel>
          {/* 两栏媒体框统一按参考视频画幅取形状：场景图不再跟随素材自身比例
              （竖长细条），与首帧一栏同形；行与面板取同一比例，列宽推导
              与框体高度上限同源。 */}
          <ReplicaMediaRow
            className="media-row--scene"
            minHeight={SCENE_ROW_MIN_HEIGHT}
            ratio={ratio}
          >
            <Panel
              className="creation-replacement-step banded"
              style={panelRatioStyle(ratio)}
            >
              <div className="creation-panel-title-row">
                <span>场景形象</span>
              </div>
              <CharacterSelection
                banded
                onBusyChange={setLeafBusy}
                onVersionChange={handleCharacterChange}
                projectId={projectId}
                readOnly={readOnly}
                sceneOnly
                variant="inline"
              />
            </Panel>
            <Panel
              className="creation-replacement-step banded"
              style={panelRatioStyle(ratio)}
            >
              <div className="creation-panel-title-row">
                <span>置换首帧</span>
              </div>
              {characterSelection && sourceFrameSelection ? (
                <FirstFrameSelection
                  banded
                  onBusyChange={setLeafBusy}
                  onSelectionChange={handleFirstFrameChange}
                  projectId={projectId}
                  readOnly={readOnly}
                  referenceSelection={referenceSelection}
                  simplified
                  sourceFrameSelectionId={sourceFrameSelection.id}
                />
              ) : (
                <Empty
                  title="等待前置步骤"
                  description="选择场景形象并确认源画面后，即可替换原视频中的人物。"
                />
              )}
              {referenceError ? (
                <>
                  <p className="settings-error" role="alert">
                    {referenceError}
                  </p>
                  <Button
                    disabled={readOnly || referenceMatching}
                    onClick={retryReferenceMatch}
                    variant="outline"
                  >
                    重试匹配人物参考
                  </Button>
                </>
              ) : null}
            </Panel>
          </ReplicaMediaRow>
          <Hint>
            {firstFrameAssetId
              ? "首帧已确认"
              : "选定首帧后，可在下方合成提示词。"}
          </Hint>
        </>
      )}
    </div>
  );
}

export function ReplacementPage() {
  return <ReplicaPage />;
}

const ratios = ["自动", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"];
const assetKindNames: Record<StudioAsset["kind"], string> = {
  image: "图片",
  video: "视频",
  audio: "音频",
};

function ParameterControls() {
  const { state, patchDraft, user } = useStudio();
  const readOnly = user.role === "auditor";
  const draft = state.draft;
  // 供应商 ratio 契约（2026-09-19 真实付费核对）：
  //   t2v 不支持 adaptive，隐藏"自动"选项；
  //   i2v/l2v 恒为 adaptive（比例由首帧图片决定），禁用选择器。
  const mode = resolveVideoMode(
    state.page,
    Boolean(draft.firstFrameId),
    Boolean(draft.tailFrameId),
  );
  const frameBased = mode === "i2v" || mode === "l2v";
  const ratioOptions =
    mode === "t2v" ? ratios.filter((r) => r !== "自动") : ratios;
  return (
    <div className="creation-parameters creation-parameters--inline">
      <Field label="分辨率">
        <select
          aria-label="分辨率"
          disabled={readOnly}
          value={draft.resolution}
          onChange={(event) => patchDraft({ resolution: event.target.value })}
        >
          <option value="768P">768P</option>
          <option value="2K">2K</option>
        </select>
      </Field>
      <Field label="时长">
        <select
          aria-label="时长"
          disabled={readOnly}
          value={draft.duration}
          onChange={(event) =>
            patchDraft({ duration: Number(event.target.value) })
          }
        >
          {Array.from({ length: 12 }, (_, i) => i + 4).map((seconds) => (
            <option key={seconds} value={seconds}>
              {seconds} 秒
            </option>
          ))}
        </select>
      </Field>
      <Field label="画面比例">
        {frameBased ? (
          <>
            <select aria-label="画面比例" disabled value="自动">
              <option value="自动">自动（由首帧图片决定）</option>
            </select>
            <Hint>图生视频的画面比例由首帧图片决定，无需选择。</Hint>
          </>
        ) : (
          <select
            aria-label="画面比例"
            disabled={readOnly}
            value={ratioOptions.includes(draft.ratio) ? draft.ratio : "9:16"}
            onChange={(event) => patchDraft({ ratio: event.target.value })}
          >
            {ratioOptions.map((ratio) => (
              <option key={ratio} value={ratio}>
                {ratio}
              </option>
            ))}
          </select>
        )}
      </Field>
      <Field label="生成数量">
        <select
          aria-label="生成数量"
          disabled={readOnly}
          value={draft.count}
          onChange={(event) =>
            patchDraft({ count: Number(event.target.value) })
          }
        >
          {[1, 2, 4].map((count) => (
            <option key={count} value={count}>
              {count} 个
            </option>
          ))}
        </select>
      </Field>
    </div>
  );
}

// 生成等待期的安抚文案：按阶段轮换，降低等待焦虑（不伪造进度）。
const REASSURANCE_COPY = [
  "AI 正在理解你的提示词与画面结构…",
  "视频生成通常需要 1–3 分钟，可以先去处理其他创作。",
  "任务已进入公平队列，关掉页面也不会丢失进度。",
  "生成完成后可以直接在下方预览成片。",
];

const VIDEO_STAGE_LABELS = ["已提交", "排队中", "生成中", "完成"] as const;

function videoStageIndex(status: StudioTask["status"]): number {
  if (status === "queued") return 1;
  if (status === "running") return 2;
  if (status === "completed") return 3;
  return 0;
}

function formatElapsed(from: string): string {
  // 只给无时区的服务端 UTC 文本补 Z，保留 ISO 时间已有的偏移量。
  const normalized = from.replace(" ", "T").replace(/([+-]\d{2})$/, "$1:00");
  const started = new Date(
    /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized) ? normalized : `${normalized}Z`,
  ).getTime();
  if (Number.isNaN(started)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - started) / 1000));
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

/** 生成等待视图：阶段时间线 + 进度百分比 + 轮换安抚文案。 */
function VideoProgressView({ task }: { task: StudioTask }) {
  const [copyIndex, setCopyIndex] = useState(0);
  const [, setTick] = useState(0);
  const failed = task.status === "failed" || task.status === "uncertain";
  const completed = task.status === "completed";
  const cancelled = task.status === "cancelled";

  useEffect(() => {
    if (failed || completed || cancelled) return;
    const timer = window.setInterval(() => {
      setCopyIndex((value) => (value + 1) % REASSURANCE_COPY.length);
      setTick((value) => value + 1);
    }, 6000);
    return () => window.clearInterval(timer);
  }, [failed, completed, cancelled]);

  if (cancelled) {
    const note = cancelledTaskCreditsNote(task);
    return (
      <div className="creation-progress" role="status">
        <div className="creation-progress-headline">任务已取消</div>
        {note ? (
          <p className={`creation-progress-credits is-${note.tone}`}>
            {note.text}
          </p>
        ) : (
          <p className="creation-progress-copy">
            本次任务已结束，计费结果可在消费记录中查看。
          </p>
        )}
      </div>
    );
  }

  if (failed) {
    const note = failedTaskCreditsNote(task);
    return (
      <div className="creation-progress failed" role="alert">
        <div className="creation-progress-headline">
          生成未完成{task.status === "uncertain" ? "（状态待确认）" : ""}
        </div>
        <p className={`creation-progress-credits is-${note.tone}`}>
          {note.text}
        </p>
        <Empty
          title="这条视频没有生成成功"
          description="可回到上方调整提示词或素材后重新生成。"
        />
      </div>
    );
  }

  const stageIndex = videoStageIndex(task.status);
  const progress = task.progress ?? 0;

  return (
    <div className="creation-progress" aria-live="polite">
      <ol className="creation-progress-stages">
        {VIDEO_STAGE_LABELS.map((label, index) => (
          <li
            key={label}
            className={
              index === stageIndex
                ? "is-active"
                : index < stageIndex
                  ? "is-done"
                  : ""
            }
          >
            {label}
          </li>
        ))}
      </ol>
      <progress
        aria-label="生成进度"
        className="creation-progress-bar"
        max={100}
        value={task.status === "completed" ? 100 : progress}
      />
      <div className="creation-progress-meta">
        <span>{task.status === "completed" ? "生成完成" : `${progress}%`}</span>
        {!completed && formatElapsed(task.submitted) ? (
          <span>已等待 {formatElapsed(task.submitted)}</span>
        ) : null}
      </div>
      <p className="creation-progress-copy">
        {completed
          ? "成片已生成，可以查看、播放或下载。"
          : REASSURANCE_COPY[copyIndex]}
      </p>
    </div>
  );
}

function SavedPromptImporter({
  onImport,
}: {
  onImport: (
    promptText: string,
    context?: SavedPromptItem["generation_context"],
  ) => void;
}) {
  const [open, setOpen] = useState(false);
  const [prompts, setPrompts] = useState<SavedPromptItem[]>();
  const [error, setError] = useState<string>();
  const { review, notify, user } = useStudio();
  const readOnly = user.role === "auditor";

  const toggle = () => {
    if (readOnly) return;
    const next = !open;
    setOpen(next);
    if (next && prompts === undefined && !error && !review) {
      void listUserSavedPrompts()
        .then(setPrompts)
        .catch(() => setError("我的提示词暂时读取失败，请稍后重试。"));
    }
  };

  return (
    <div className="creation-prompt-import">
      <Button variant="quiet" disabled={readOnly} onClick={toggle}>
        <Icon name="arrow" size={16} /> 导入提示词
      </Button>
      {open && (
        <div
          className="creation-prompt-list"
          role="listbox"
          aria-label="我的提示词"
        >
          {review ? (
            <p>审核示例不提供提示词库。</p>
          ) : error ? (
            <p>{error}</p>
          ) : prompts === undefined ? (
            <p>正在读取我的提示词…</p>
          ) : prompts.length === 0 ? (
            <p>
              还没有保存过提示词。在项目拆解中修订反推提示词并保存后会出现在这里。
            </p>
          ) : (
            prompts.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => {
                  onImport(item.prompt_text, item.generation_context);
                  notify(`已导入「${item.name}」，可继续修改。`);
                  setOpen(false);
                }}
              >
                <strong>{item.name}</strong>
                <small>{item.prompt_text.slice(0, 60)}</small>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function ShotTableImporter({
  disabled = false,
  onImport,
}: {
  disabled?: boolean;
  onImport: (promptText: string) => void;
}) {
  const { notify } = useStudio();
  const importFromClipboard = async () => {
    if (disabled) return;
    try {
      if (!navigator.clipboard?.readText)
        throw new Error("当前环境不支持读取剪贴板");
      const result = promptTextFromShotTableClipboard(
        await navigator.clipboard.readText(),
      );
      if (!result.ok) {
        notify(result.error);
        return;
      }
      onImport(result.promptText);
      notify("分镜表已导入，请点击“AI 优化”。");
    } catch {
      notify("无法读取剪贴板，请检查剪贴板权限后重试。");
    }
  };
  return (
    <Button
      variant="quiet"
      disabled={disabled}
      onClick={() => void importFromClipboard()}
    >
      <Icon name="copy" size={16} /> 导入分镜脚本
    </Button>
  );
}

type UploadKind = "image" | "video" | "audio";

/** 与后端 ALLOWED_UPLOADS 及参考音频白名单对齐；accept 与匹配都按扩展名，
 *  因为 WebView 给出的 MIME 会漂移（.m4a→audio/x-m4a、.opus→audio/ogg 等），
 *  按 MIME 精确比对会把合法文件挡在门外。 */
const UPLOAD_EXTENSIONS: Record<UploadKind, string[]> = {
  image: [".png", ".jpg", ".jpeg"],
  video: [".mp4", ".mov"],
  audio: [".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"],
};

function uploadKindForFile(
  file: File,
  acceptKinds: UploadKind[],
): UploadKind | undefined {
  const name = file.name.toLowerCase();
  return acceptKinds.find((item) =>
    UPLOAD_EXTENSIONS[item].some((extension) => name.endsWith(extension)),
  );
}

function VideoMaterialUpload({
  disabled = false,
  dropzone = false,
  group,
  label,
  acceptKinds = ["image"],
  onUploaded,
}: {
  disabled?: boolean;
  dropzone?: boolean;
  group: string;
  label: string;
  acceptKinds?: UploadKind[];
  onUploaded: (asset: StudioAsset) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number>();
  const uploadingRef = useRef(false);
  const [dragOver, setDragOver] = useState(false);
  const { review, notify, user } = useStudio();
  const readOnly = user.role === "auditor";
  const onUploadedRef = useRef(onUploaded);
  const mountedRef = useRef(false);
  onUploadedRef.current = onUploaded;
  const acceptsMedia =
    acceptKinds.includes("video") || acceptKinds.includes("audio");

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const upload = async (file: File) => {
    if (readOnly || disabled || uploadingRef.current) return;
    const kind = uploadKindForFile(file, acceptKinds);
    if (!kind) {
      notify(
        acceptsMedia
          ? "仅支持 PNG、JPEG 图片，MP4、MOV 视频，或 MP3、WAV、M4A、AAC、FLAC、OGG、OPUS 音频。"
          : "仅支持 PNG 或 JPEG 图片。",
      );
      return;
    }
    if (review) {
      notify("审核示例不上传素材。");
      return;
    }
    uploadingRef.current = true;
    setProgress(0);
    try {
      let asset: StudioAsset;
      if (kind === "image") {
        asset = await uploadVideoMaterial(file, group, setProgress);
      } else {
        // 视频/音频参考上传前先探测时长，超出 2–15 秒直接拦截、不发上传请求。
        // 探测失败的报错本身可操作（换格式/裁剪），单独成支透出，不再落到
        // 下面的「上传失败，请稍后重试」通用分支。
        let duration: number;
        try {
          duration =
            kind === "video"
              ? await readVideoDuration(file)
              : await readAudioDuration(file);
        } catch (error) {
          if (mountedRef.current) {
            notify(
              error instanceof Error && error.message
                ? error.message
                : kind === "video"
                  ? "无法读取视频时长，请改用 MP4 或 MOV 文件后重试。"
                  : "无法读取音频时长，请改用 MP3、WAV、M4A 等常见格式后重试。",
            );
          }
          return;
        }
        if (duration < MIN_REFERENCE_MEDIA_SECONDS) {
          if (mountedRef.current)
            notify(
              kind === "video"
                ? `参考视频时长不能短于 ${MIN_REFERENCE_MEDIA_SECONDS} 秒，请更换素材或裁剪后上传。`
                : `参考音频时长不能短于 ${MIN_REFERENCE_MEDIA_SECONDS} 秒，请更换素材或裁剪后上传。`,
            );
          return;
        }
        if (duration > MAX_REFERENCE_MEDIA_SECONDS) {
          if (mountedRef.current)
            notify(
              kind === "video"
                ? `参考视频时长不能超过 ${MAX_REFERENCE_MEDIA_SECONDS} 秒，请裁剪后再上传。`
                : `参考音频时长不能超过 ${MAX_REFERENCE_MEDIA_SECONDS} 秒，请裁剪后再上传。`,
            );
          return;
        }
        asset =
          kind === "video"
            ? await uploadVideoMaterial(file, group, setProgress)
            : await uploadReferenceAudioMaterial(file, duration, setProgress);
      }
      if (kind === "video") {
        // REFERENCE-MATERIAL-PREVIEW：本机上传的视频就地抽一帧，列表立刻有图，
        // 不必等服务端首帧缩略图；失败只损失缩略图，不影响上传结果。
        try {
          const poster = await readVideoFirstFrame(file);
          if (poster) asset = { ...asset, poster };
        } catch {
          // 上传结果仍可直接播放；服务端批量预览会继续尝试补齐封面。
        }
      }
      if (mountedRef.current) {
        onUploadedRef.current(asset);
        notify(`${label}「${file.name}」已上传到素材库。`);
      }
    } catch {
      if (mountedRef.current) notify("素材上传失败，请稍后重试。");
    } finally {
      uploadingRef.current = false;
      if (mountedRef.current) setProgress(undefined);
    }
  };

  return (
    <>
      <button
        className={
          dropzone
            ? `creation-upload-dropzone ${dragOver ? "is-dragging" : ""}`
            : "creation-upload-mini"
        }
        aria-label={dropzone ? "上传文件" : undefined}
        onDragOver={(event) => {
          if (!dropzone) return;
          event.preventDefault();
          if (!readOnly && !disabled) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(event) => {
          if (!dropzone) return;
          event.preventDefault();
          setDragOver(false);
          if (readOnly || disabled || uploadingRef.current) return;
          const files = event.dataTransfer.files;
          if (files.length !== 1) {
            notify("请每次添加一个文件，便于核对参考素材编号。");
            return;
          }
          void upload(files[0]);
        }}
        disabled={readOnly || disabled || progress !== undefined}
        onClick={() => inputRef.current?.click()}
        type="button"
      >
        {dropzone && <Icon name="plus" size={38} />}
        <span>
          {progress !== undefined
            ? `上传中 ${progress}%`
            : dropzone
              ? "上传文件"
              : "本机上传"}
        </span>
        {dropzone && <small>点击或拖拽添加</small>}
      </button>
      <input
        accept={acceptKinds
          .flatMap((item) => UPLOAD_EXTENSIONS[item])
          .join(",")}
        aria-label={`上传${label}`}
        disabled={readOnly || disabled}
        hidden
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void upload(file);
          event.target.value = "";
        }}
        ref={inputRef}
        type="file"
      />
      {acceptsMedia && (
        <small className="creation-upload-hint">
          视频、音频单条 2–
          {MAX_REFERENCE_MEDIA_SECONDS} 秒且各累计 ≤
          {MAX_REFERENCE_MEDIA_SECONDS} 秒；参考合计 ≤{MAX_REFERENCE_FILES} 项
        </small>
      )}
    </>
  );
}

function VideoFrameCard({
  asset,
  disabled,
  label,
  optionalLabel,
  picker,
  onChooseLibrary,
  onRemove,
  onUploaded,
}: {
  asset?: StudioAsset;
  disabled: boolean;
  label: "首帧" | "尾帧";
  optionalLabel: string;
  picker: "first-frame" | "tail-frame";
  onChooseLibrary: (picker: "first-frame" | "tail-frame") => void;
  onRemove: () => void;
  onUploaded: (asset: StudioAsset) => void;
}) {
  const [sourceOpen, setSourceOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeAndRestoreFocus = () => {
    setSourceOpen(false);
    requestAnimationFrame(() => triggerRef.current?.focus());
  };
  const chooseLibrary = () => {
    setSourceOpen(false);
    onChooseLibrary(picker);
  };
  return (
    <div className="creation-frame-slot">
      <button
        aria-haspopup="dialog"
        aria-label={asset ? `更换${label}` : `添加${label}`}
        className="creation-frame-card"
        disabled={disabled}
        onClick={() => setSourceOpen(true)}
        ref={triggerRef}
        type="button"
      >
        {asset ? (
          <Media asset={asset} alt={label} presentation="video" />
        ) : (
          <span className="creation-frame-empty">
            <Icon name="plus" size={38} />
            <strong>{`添加${label}`}</strong>
            <small>从素材库选择或本地上传</small>
          </span>
        )}
        <span className="creation-frame-card-label">
          {asset ? `${label} · 点击更换` : optionalLabel}
        </span>
      </button>
      {asset && (
        <Button variant="quiet" disabled={disabled} onClick={onRemove}>
          {`移除${label}`}
        </Button>
      )}
      {sourceOpen && (
        <StudioDialog title={`选择${label}来源`} onClose={closeAndRestoreFocus}>
          <p className="creation-frame-source-copy">
            从素材库选择已有图片，或从本机上传新图片。
          </p>
          <div className="creation-frame-source-actions">
            <Button variant="outline" onClick={chooseLibrary}>
              从素材库选择
            </Button>
            <VideoMaterialUpload
              disabled={disabled}
              group={`${label}素材`}
              label={label}
              onUploaded={(uploadedAsset) => {
                onUploaded(uploadedAsset);
                closeAndRestoreFocus();
              }}
            />
          </div>
        </StudioDialog>
      )}
    </div>
  );
}

/** 参考素材缩略图/预览解析（REFERENCE-MATERIAL-PREVIEW）：一次批量授权换整表
 * 地址——图片直出本体签名地址，视频额外拿服务端首帧缩略图；结果按 id 增量合并
 * 并缓存，只对尚未就绪的 id 发请求，卸载时释放持有的本机 Blob URL。
 * 依赖只跟「可解析 id 集合 / 用户 / 重试令牌」，不跟每次都新建的素材数组。 */
function useReferencePreviews(assets: StudioAsset[], userId: string) {
  const [entries, setEntries] = useState<ReferencePreviewMap>({});
  const entriesRef = useRef(entries);
  entriesRef.current = entries;
  const assetsRef = useRef(assets);
  assetsRef.current = assets;
  const [retryToken, setRetryToken] = useState(0);
  const operationRef = useRef(0);
  const targetKey = referencePreviewTargets(assets).join("|");

  useEffect(() => {
    // retryToken 只作为「同一批 id 重新解析」的触发器参与依赖。
    void retryToken;
    const ids = targetKey ? targetKey.split("|") : [];
    if (!ids.length) {
      releaseReferencePreviews(entriesRef.current);
      setEntries({});
      return;
    }
    const operation = ++operationRef.current;
    const pending = ids.filter(
      (id) => entriesRef.current[id]?.status !== "ready",
    );
    if (!pending.length) return;
    setEntries((previous) => markReferencePreviewsLoading(previous, pending));
    // 放进 then 里调用：解析器在缺少缓存上下文等场景可能同步抛错，这里统一收敛
    // 成「可见的失败 + 可重试」，绝不让参考列表把整页渲染带崩。
    void Promise.resolve()
      .then(() =>
        getMaterialBatchPreviews(
          userId,
          pending.map((id) => ({ id, populate: false })),
          {},
        ),
      )
      .then((batch) => {
        if (operation !== operationRef.current) return;
        setEntries((previous) => {
          const resolved = referencePreviewEntries(
            assetsRef.current,
            pending,
            batch,
          );
          // 被替换的旧结果可能持有 Blob URL，先释放再合并。
          for (const id of pending) {
            if (resolved[id]) previous[id]?.release?.();
          }
          return mergeReferencePreviews(previous, resolved);
        });
      })
      .catch((cause: unknown) => {
        if (operation !== operationRef.current) return;
        const message = customerVisibleErrorMessage(
          cause,
          "缩略图暂不可用，请重试。",
        );
        setEntries((previous) => {
          const failed: ReferencePreviewMap = {};
          for (const id of pending) failed[id] = { status: "error", message };
          return mergeReferencePreviews(previous, failed);
        });
      });
    return () => {
      operationRef.current += 1;
    };
  }, [targetKey, userId, retryToken]);

  useEffect(() => () => releaseReferencePreviews(entriesRef.current), []);

  const retry = useCallback((assetId: string) => {
    setEntries((previous) => clearReferencePreview(previous, assetId));
    setRetryToken((token) => token + 1);
  }, []);

  return { entries, retry };
}

const PROMPT_MODE_LABELS: Record<H3Mode, string> = {
  T2VA: "文生视频",
  I2VA: "首帧生视频",
  FL2VA: "首尾帧生视频",
  L2VA: "尾帧生视频",
  Ref2VA: "参考生视频",
};

function promptModeLabel(mode: H3Mode | undefined): string {
  return mode ? `${PROMPT_MODE_LABELS[mode]}（${mode}）` : "未记录";
}

/** 用途是自由文本，但快捷项按顿号/逗号拼装，两者共用同一套切分口径。 */
function purposeTerms(value?: string): string[] {
  return (value ?? "")
    .split(/[、,，]/)
    .map((term) => term.trim())
    .filter(Boolean);
}

function selectPurposeTerm(
  kind: AssetKind,
  selected: string[],
  term: string,
): string {
  // 音频用途是单选：「音色参考」与「不用它的声音」互斥，多选会让提示词自相矛盾。
  if (kind === "audio")
    return selected.includes(term) && selected.length === 1 ? "" : term;
  return (
    selected.includes(term)
      ? selected.filter((item) => item !== term)
      : [...selected, term]
  ).join("、");
}

export function VideoPage() {
  const {
    state,
    data,
    patchDraft,
    navigate,
    openPicker,
    saveDraft,
    requestGeneration,
    updateData,
    notify,
    review,
    videoCapabilities,
    videoCapabilitiesStatus,
    retryVideoCapabilities,
    referenceAssetsPending,
    referenceAssetsError,
    retryReferenceAssets,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [previewReferenceId, setPreviewReferenceId] = useState<string>();
  const [referencePreviewDialogId, setReferencePreviewDialogId] =
    useState<string>();
  const [resolvedReferencePreview, setResolvedReferencePreview] =
    useState<StudioAsset>();
  const [referencePreviewLoading, setReferencePreviewLoading] = useState(false);
  const [referencePreviewError, setReferencePreviewError] = useState("");
  const [shotTableImported, setShotTableImported] = useState(false);
  const referencePreviewRequestRef = useRef(0);
  const referencePreviewTriggerRef = useRef<HTMLButtonElement | null>(null);
  const closeReferencePreview = () => {
    setReferencePreviewDialogId(undefined);
    requestAnimationFrame(() =>
      referencePreviewTriggerRef.current?.focus({ preventScroll: true }),
    );
  };
  const referenceMode = state.page === "reference";
  const currentPromptMode: H3Mode = referenceMode
    ? "Ref2VA"
    : state.draft.firstFrameId && state.draft.tailFrameId
      ? "FL2VA"
      : state.draft.firstFrameId
        ? "I2VA"
        : state.draft.tailFrameId
          ? "L2VA"
          : "T2VA";
  const importedPromptMode = state.draft.importedPromptContext?.mode;
  const importedModeMismatch = Boolean(
    importedPromptMode && importedPromptMode !== currentPromptMode,
  );
  const storedFirstFrame =
    findAsset(data.assets, state.draft.firstFrameId) ??
    findAsset(data.materials, state.draft.firstFrameId);
  const [resolvedFirstFrame, setResolvedFirstFrame] = useState<StudioAsset>();
  const [firstFrameLoading, setFirstFrameLoading] = useState(false);
  const [firstFrameError, setFirstFrameError] = useState("");
  const [firstFrameLoadAttempt, setFirstFrameLoadAttempt] = useState(0);
  const firstFrameRequestRef = useRef(0);
  const firstFramePromiseRef = useRef<
    | {
        key: string;
        promise: ReturnType<typeof getAssetDownloadUrl>;
      }
    | undefined
  >(undefined);
  const firstFrameId = state.draft.firstFrameId;
  const usableStoredFirstFrame =
    storedFirstFrame?.kind === "image" && storedFirstFrame.url
      ? storedFirstFrame
      : undefined;
  const firstFrame =
    usableStoredFirstFrame ??
    (resolvedFirstFrame?.id === firstFrameId ? resolvedFirstFrame : undefined);

  useEffect(() => {
    const requestId = ++firstFrameRequestRef.current;
    if (referenceMode || review || !firstFrameId || usableStoredFirstFrame) {
      setResolvedFirstFrame(undefined);
      setFirstFrameLoading(false);
      setFirstFrameError("");
      return;
    }

    setResolvedFirstFrame(undefined);
    setFirstFrameLoading(true);
    setFirstFrameError("");
    const key = `${firstFrameId}:${firstFrameLoadAttempt}`;
    const existing = firstFramePromiseRef.current;
    const promise =
      existing?.key === key
        ? existing.promise
        : getAssetDownloadUrl(firstFrameId);
    firstFramePromiseRef.current = { key, promise };

    void promise
      .then(({ url }) => {
        if (firstFrameRequestRef.current !== requestId) return;
        const asset: StudioAsset = {
          ...storedFirstFrame,
          id: firstFrameId,
          assetId: storedFirstFrame?.assetId ?? firstFrameId,
          name: storedFirstFrame?.name ?? "已确认置换首帧",
          kind: "image",
          url,
          group: storedFirstFrame?.group ?? "置换首帧",
          source: storedFirstFrame?.source ?? "人物置换",
          saved: true,
          delivery: storedFirstFrame?.delivery ?? "stored",
        };
        setResolvedFirstFrame(asset);
        setFirstFrameLoading(false);
        updateData((previous) => ({
          ...previous,
          assets: [
            asset,
            ...previous.assets.filter((item) => item.id !== asset.id),
          ],
        }));
      })
      .catch((cause: unknown) => {
        if (firstFrameRequestRef.current !== requestId) return;
        setFirstFrameLoading(false);
        setFirstFrameError(
          customerVisibleErrorMessage(cause, "首帧预览读取失败，请重试。"),
        );
      });

    return () => {
      if (firstFrameRequestRef.current === requestId)
        firstFrameRequestRef.current += 1;
    };
  }, [
    firstFrameId,
    firstFrameLoadAttempt,
    referenceMode,
    review,
    storedFirstFrame,
    updateData,
    usableStoredFirstFrame,
  ]);
  const tailFrame =
    findAsset(data.assets, state.draft.tailFrameId) ??
    findAsset(data.materials, state.draft.tailFrameId);
  const referenceValidation = validateReferences(
    state.draft.referenceIds,
    [...data.assets, ...data.materials],
    {
      maxReferenceImages:
        videoCapabilities?.max_reference_images ?? DEFAULT_MAX_REFERENCE_IMAGES,
      maxReferenceVideos:
        videoCapabilities?.max_reference_videos ?? DEFAULT_MAX_REFERENCE_VIDEOS,
      maxReferenceAudios:
        videoCapabilities?.max_reference_audios ?? DEFAULT_MAX_REFERENCE_AUDIOS,
    },
  );
  const references = referenceValidation.assets;
  const referencePreviews = useReferencePreviews(references, user.id);
  const selectedReferenceAsset =
    references.find((asset) => asset.id === previewReferenceId) ??
    references[0];
  const selectedReferenceEntry = selectedReferenceAsset
    ? referencePreviews.entries[referencePreviewAssetId(selectedReferenceAsset)]
    : undefined;
  const selectedReferencePreviewAsset = selectedReferenceAsset
    ? {
        ...selectedReferenceAsset,
        url: selectedReferenceEntry?.mediaUrl ?? selectedReferenceAsset.url,
        poster: selectedReferenceEntry?.poster ?? selectedReferenceAsset.poster,
      }
    : undefined;
  const referencePreviewDialogAsset = references.find(
    (asset) => asset.id === referencePreviewDialogId,
  );
  const referencePreviewEntry = referencePreviewDialogAsset
    ? referencePreviews.entries[
        referencePreviewAssetId(referencePreviewDialogAsset)
      ]
    : undefined;
  const referencePreviewMedia = !referencePreviewDialogAsset
    ? undefined
    : resolvedReferencePreview &&
        resolvedReferencePreview.id === referencePreviewDialogId
      ? {
          ...resolvedReferencePreview,
          poster:
            referencePreviewEntry?.poster ?? resolvedReferencePreview.poster,
        }
      : referencePreviewDialogAsset.url
        ? {
            ...referencePreviewDialogAsset,
            poster:
              referencePreviewEntry?.poster ??
              referencePreviewDialogAsset.poster,
          }
        : referencePreviewEntry?.mediaUrl
          ? {
              ...referencePreviewDialogAsset,
              url: referencePreviewEntry.mediaUrl,
              poster:
                referencePreviewEntry.poster ??
                referencePreviewDialogAsset.poster,
            }
          : undefined;
  useEffect(() => {
    const requestId = ++referencePreviewRequestRef.current;
    if (!referencePreviewDialogAsset) {
      setResolvedReferencePreview(undefined);
      setReferencePreviewLoading(false);
      setReferencePreviewError("");
      return;
    }
    if (referencePreviewDialogAsset.url) {
      setResolvedReferencePreview(referencePreviewDialogAsset);
      setReferencePreviewLoading(false);
      setReferencePreviewError("");
      return;
    }
    // 列表已经批量解析过的地址直接复用，弹窗不再重复授权一次。
    const resolvedFromList =
      referencePreviews.entries[
        referencePreviewAssetId(referencePreviewDialogAsset)
      ];
    if (resolvedFromList?.status === "ready" && resolvedFromList.mediaUrl) {
      setResolvedReferencePreview({
        ...referencePreviewDialogAsset,
        url: resolvedFromList.mediaUrl,
      });
      setReferencePreviewLoading(false);
      setReferencePreviewError("");
      return;
    }

    setResolvedReferencePreview(undefined);
    setReferencePreviewLoading(true);
    setReferencePreviewError("");
    void getAssetDownloadUrl(
      referencePreviewDialogAsset.assetId ?? referencePreviewDialogAsset.id,
    )
      .then(({ url }) => {
        if (referencePreviewRequestRef.current !== requestId) return;
        setResolvedReferencePreview({ ...referencePreviewDialogAsset, url });
        setReferencePreviewLoading(false);
      })
      .catch((cause: unknown) => {
        if (referencePreviewRequestRef.current !== requestId) return;
        setReferencePreviewLoading(false);
        setReferencePreviewError(
          customerVisibleErrorMessage(cause, "素材预览读取失败，请重试。"),
        );
      });

    return () => {
      if (referencePreviewRequestRef.current === requestId)
        referencePreviewRequestRef.current += 1;
    };
  }, [referencePreviewDialogAsset, referencePreviews.entries]);
  const effectiveCapabilitiesStatus = review
    ? "ready"
    : (videoCapabilitiesStatus ?? (videoCapabilities ? "ready" : "loading"));
  const videoCapabilityPending =
    !referenceMode && effectiveCapabilitiesStatus === "loading";
  const videoCapabilityError =
    !referenceMode && effectiveCapabilitiesStatus === "error";
  const videoModeDisabled =
    !referenceMode &&
    !review &&
    (firstFrameId
      ? videoCapabilities?.i2v_enabled === false
      : videoCapabilities?.t2v_enabled === false);
  const referenceCapabilityPending =
    referenceMode && effectiveCapabilitiesStatus === "loading";
  const referenceCapabilityError =
    referenceMode && effectiveCapabilitiesStatus === "error";
  const referenceModeDisabled =
    referenceMode && !review && videoCapabilities?.r2v_enabled === false;
  const referenceHasIssues =
    !referenceAssetsPending &&
    !referenceAssetsError &&
    referenceValidation.issues.length > 0;
  const referenceAtLimit =
    !referenceHasIssues &&
    referenceValidation.imageCount >= referenceValidation.imageLimit &&
    referenceValidation.videoCount >= referenceValidation.videoLimit &&
    referenceValidation.audioCount >= referenceValidation.audioLimit;
  // 合并上传区（上传框 + 从素材库选择）共用同一套禁用条件。
  const referenceAddingDisabled =
    readOnly ||
    referenceCapabilityPending ||
    referenceCapabilityError ||
    referenceModeDisabled ||
    referenceHasIssues ||
    referenceAssetsPending ||
    referenceAssetsError ||
    referenceAtLimit;
  // 参考生视频（六段式）与文/图生视频（集成描述）是两套提示词实现：正文分仓，
  // 切页签不会互相覆盖，提交时按当前模式取对应那份。
  const videoPrompt = referenceMode
    ? (state.draft.referencePrompt ?? "")
    : state.draft.prompt;
  const ready =
    Boolean(videoPrompt.trim()) &&
    !state.draft.replicaPreparationPending &&
    (referenceMode
      ? references.length > 0 &&
        !referenceCapabilityPending &&
        !referenceCapabilityError &&
        !referenceModeDisabled &&
        !referenceHasIssues &&
        !referenceAssetsPending &&
        !referenceAssetsError
      : !videoCapabilityPending &&
        !videoCapabilityError &&
        !videoModeDisabled &&
        (!firstFrameId || Boolean(firstFrame)));
  // T2V 提交前的「建议优化提示词」引导（非强制）：仅当当前为文生视频、
  // 已填写提示词、且本会话尚未对当前文本应用过 AI 优化时提示。用户可
  // 忽略直接提交；readAppliedOptimization 命中已优化收据时返回 source="ai"。
  const promptOptimizationScope = `${user.id}:${state.page}`;
  const promptAlreadyOptimized =
    readAppliedOptimization(promptOptimizationScope, videoPrompt).source ===
    "ai";
  const suggestOptimizePrompt =
    !referenceMode &&
    currentPromptMode === "T2VA" &&
    !readOnly &&
    Boolean(videoPrompt.trim()) &&
    !promptAlreadyOptimized;
  // 参考生视频的两个状态位：已是六段式 → 按钮只做优化；否则按钮负责从需求生成。
  // 生成中禁用「生成视频」——半成品提示词提交会被服务端结构门禁拒绝，不如在入口拦住。
  const referenceStructured = referenceMode && hasRef2vaStructure(videoPrompt);
  const [promptBusy, setPromptBusy] = useState(false);
  const [regenerateSignal, setRegenerateSignal] = useState(0);
  const videoTask = state.draft.videoBatchId
    ? data.tasks.find((task) => task.id === state.draft.videoBatchId)
    : undefined;
  const patchVideoPrompt = (text: string, extra: Partial<StudioDraft> = {}) =>
    patchDraft(
      referenceMode
        ? { referencePrompt: text, ...extra }
        : { prompt: text, ...extra },
    );

  const appendMaterial = (asset: StudioAsset) => {
    updateData((previous) => ({
      ...previous,
      materials: [
        asset,
        ...previous.materials.filter((item) => item.id !== asset.id),
      ],
    }));
  };

  const addReference = (asset: StudioAsset) => {
    appendMaterial(asset);
    if (referenceHasIssues) {
      notify("请先整理旧草稿中的无效参考素材。");
      return;
    }
    if (state.draft.referenceIds.includes(asset.id)) {
      notify("该参考素材已选择，请勿重复添加。");
      return;
    }
    const atKindLimit =
      asset.kind === "image"
        ? referenceValidation.imageCount >= referenceValidation.imageLimit
        : asset.kind === "video"
          ? referenceValidation.videoCount >= referenceValidation.videoLimit
          : referenceValidation.audioCount >= referenceValidation.audioLimit;
    if (atKindLimit) {
      notify(
        asset.kind === "image"
          ? `当前最多选择 ${referenceValidation.imageLimit} 张参考图。`
          : asset.kind === "video"
            ? `当前最多选择 ${referenceValidation.videoLimit} 个参考视频。`
            : `当前最多选择 ${referenceValidation.audioLimit} 个参考音频。`,
      );
      return;
    }
    patchDraft({
      referenceIds: [...state.draft.referenceIds, asset.id],
    });
  };

  const generationActions = (
    <div className="creation-form-actions">
      {suggestOptimizePrompt && (
        <p className="creation-optimize-hint" role="status">
          <Icon name="sparkles" size={14} />
          <span>
            建议先点击“AI 优化提示词”，把描述整理成符合 H3 标准格式（镜头 / 声景
            / 配乐）再生成，成片更稳定；也可直接提交。
          </span>
        </p>
      )}
      <Button
        variant="outline"
        disabled={readOnly}
        onClick={() => !readOnly && saveDraft()}
      >
        保存草稿
      </Button>
      <Button
        variant="primary"
        disabled={readOnly || !ready || promptBusy}
        onClick={() => requestGeneration("视频生成")}
      >
        <Icon name="play" />
        生成视频
      </Button>
    </div>
  );

  return (
    <section
      className="creation-page creation-video-workspace creation-video-workspace--split"
      aria-label="AI 视频"
    >
      <CreationNavigation />
      <Tabs
        items={[
          { id: "video", label: "文/图生视频" },
          { id: "reference", label: "参考生视频" },
        ]}
        value={referenceMode ? "reference" : "video"}
        onChange={(value) =>
          navigate(value === "reference" ? "reference" : "video")
        }
      />
      <div
        className={`creation-video-grid ${referenceMode ? "reference" : ""}`}
      >
        <Panel className="creation-video-form">
          {referenceMode ? (
            <ControlGroup
              label={
                <>
                  <b className="creation-step-number">01</b> 参考素材
                </>
              }
            >
              {referenceCapabilityPending && (
                <p className="settings-error" role="status">
                  正在读取参考生视频能力，请稍候。
                </p>
              )}
              {referenceCapabilityError && (
                <div>
                  <p className="settings-error" role="alert">
                    视频生成能力读取失败，请重试。
                  </p>
                  <Button onClick={retryVideoCapabilities} variant="outline">
                    重试读取视频能力
                  </Button>
                </div>
              )}
              {referenceAssetsPending && (
                <p className="settings-error" role="status">
                  正在恢复草稿参考图，请稍候。
                </p>
              )}
              {referenceAssetsError && (
                <div>
                  <p className="settings-error" role="alert">
                    草稿参考图读取失败，请重试。
                  </p>
                  <Button onClick={retryReferenceAssets} variant="outline">
                    重试读取草稿参考图
                  </Button>
                </div>
              )}
              {referenceModeDisabled && (
                <div>
                  <p className="settings-error" role="alert">
                    参考生视频当前未开放，请等待能力开启后再提交。
                  </p>
                  <Button onClick={retryVideoCapabilities} variant="outline">
                    刷新开放状态
                  </Button>
                </div>
              )}
              {!referenceAssetsPending &&
                !referenceAssetsError &&
                referenceValidation.issues.map((issue) => (
                  <p className="settings-error" key={issue} role="alert">
                    {issue}
                  </p>
                ))}
              {referenceHasIssues && (
                <Button
                  disabled={readOnly}
                  onClick={() =>
                    !readOnly &&
                    patchDraft({ referenceIds: referenceValidation.repairIds })
                  }
                  variant="outline"
                >
                  整理参考素材
                </Button>
              )}
              <div className="creation-reference-upload">
                <VideoMaterialUpload
                  key={`reference-upload-${state.draft.id}`}
                  disabled={referenceAddingDisabled}
                  acceptKinds={["image", "video", "audio"]}
                  dropzone
                  group="参考素材"
                  label="参考素材"
                  onUploaded={addReference}
                />
                <Button
                  disabled={referenceAddingDisabled}
                  onClick={() => openPicker("reference")}
                  variant="outline"
                >
                  <Icon name="upload" />
                  <span>从素材库选择</span>
                </Button>
              </div>
              <div className="creation-reference-materials">
                {references.length ? (
                  <div className="creation-reference-list">
                    {references.map((asset, index) => {
                      const entry =
                        referencePreviews.entries[
                          referencePreviewAssetId(asset)
                        ];
                      const view = referencePreviewView(asset, entry);
                      return (
                        <div className="creation-reference-row" key={asset.id}>
                          <button
                            type="button"
                            className="creation-reference-preview-button"
                            aria-label={`预览 ${asset.name}`}
                            aria-pressed={previewReferenceId === asset.id}
                            onClick={(event) => {
                              referencePreviewTriggerRef.current =
                                event.currentTarget;
                              setPreviewReferenceId(asset.id);
                              setReferencePreviewDialogId(asset.id);
                            }}
                          >
                            <Media
                              asset={view.asset}
                              alt={
                                asset.kind === "audio" ? "音频预览" : asset.name
                              }
                              fallback={
                                view.placeholder ? (
                                  <span className="creation-reference-placeholder">
                                    <span className="creation-reference-name">
                                      {asset.kind === "audio"
                                        ? "音频预览"
                                        : asset.name}
                                    </span>
                                    <small>{view.placeholder}</small>
                                  </span>
                                ) : undefined
                              }
                            />
                          </button>
                          <span className="creation-reference-copy">
                            <strong>
                              @{index + 1} → &lt;
                              {asset.kind === "image"
                                ? "Picture"
                                : asset.kind === "video"
                                  ? "Video"
                                  : "Audio"}{" "}
                              {
                                references
                                  .slice(0, index + 1)
                                  .filter((item) => item.kind === asset.kind)
                                  .length
                              }
                              &gt; {asset.name}
                            </strong>
                            <small>
                              {assetKindNames[asset.kind]} · {asset.source}
                            </small>
                            {entry?.status === "error" ? (
                              <button
                                className="creation-reference-retry"
                                type="button"
                                aria-label={`重试 ${asset.name} 的缩略图`}
                                onClick={() =>
                                  referencePreviews.retry(
                                    referencePreviewAssetId(asset),
                                  )
                                }
                              >
                                重试缩略图
                              </button>
                            ) : null}
                          </span>
                          <span className="creation-purpose-field">
                            <input
                              aria-label={`${asset.name}的参考用途`}
                              placeholder={
                                asset.kind === "audio"
                                  ? "参考用途，如音色参考、节奏参考（可留空）"
                                  : "参考用途，如人物、服装、场景"
                              }
                              disabled={readOnly}
                              value={
                                state.draft.referencePurposes?.[asset.id] ?? ""
                              }
                              maxLength={200}
                              onChange={(event) =>
                                patchDraft({
                                  referencePurposes: {
                                    ...state.draft.referencePurposes,
                                    [asset.id]: event.target.value,
                                  },
                                })
                              }
                            />
                            {/* 留空不是错误：服务端会按默认用途生成并在核对区提示待确认。
                                角标只说明「这一项还没定」，避免用户以为必须填完才能生成。 */}
                            {asset.kind === "audio" &&
                            !(
                              state.draft.referencePurposes?.[asset.id] ?? ""
                            ).trim() ? (
                              <span className="creation-purpose-badge">
                                用途待确认
                              </span>
                            ) : null}
                          </span>
                          <div className="creation-purpose-chips">
                            {(
                              {
                                image: ["人物", "服装", "场景", "风格", "构图"],
                                video: ["运镜", "动作", "节奏", "构图"],
                                audio: [
                                  "音色参考",
                                  "节奏参考",
                                  "环境音参考",
                                  "不用它的声音",
                                ],
                              } as const
                            )[asset.kind].map((term) => {
                              const selected = purposeTerms(
                                state.draft.referencePurposes?.[asset.id],
                              );
                              return (
                                <button
                                  type="button"
                                  key={term}
                                  className={
                                    selected.includes(term)
                                      ? "is-on"
                                      : undefined
                                  }
                                  disabled={readOnly}
                                  onClick={() =>
                                    patchDraft({
                                      referencePurposes: {
                                        ...state.draft.referencePurposes,
                                        [asset.id]: selectPurposeTerm(
                                          asset.kind,
                                          selected,
                                          term,
                                        ),
                                      },
                                    })
                                  }
                                >
                                  {term}
                                </button>
                              );
                            })}
                            {asset.kind === "audio" ? (
                              <small className="creation-purpose-note">
                                系统不会分析音轨内容，用途只按你的说明生效；留空时按「音色参考」生成并在核对区提示待确认。
                              </small>
                            ) : null}
                          </div>
                          <Button
                            aria-label={`移除 ${asset.name}`}
                            className="creation-reference-remove"
                            disabled={readOnly}
                            onClick={() =>
                              patchDraft({
                                referenceIds: state.draft.referenceIds.filter(
                                  (id) => id !== asset.id,
                                ),
                              })
                            }
                            variant="quiet"
                          >
                            <Icon name="close" size={18} />
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="creation-reference-empty">
                    还没有参考素材。上传本机文件或从素材库选择，第一项即 @1。
                  </p>
                )}
              </div>
              <p className="creation-reference-counter">
                <span>{`参考图 ${referenceValidation.imageCount}/${referenceValidation.imageLimit} · 视频 ${referenceValidation.videoCount}/${referenceValidation.videoLimit} · 音频 ${referenceValidation.audioCount}/${referenceValidation.audioLimit}`}</span>
                <small data-state={referenceAtLimit ? "limited" : undefined}>
                  {referenceAtLimit
                    ? "已达参考素材上限，需移除后才能继续添加。"
                    : `视频、音频单条 ${MIN_REFERENCE_MEDIA_SECONDS}–${MAX_REFERENCE_MEDIA_SECONDS} 秒，各累计 ≤${MAX_REFERENCE_MEDIA_SECONDS} 秒`}
                </small>
              </p>
              {referencePreviewDialogAsset ? (
                <StudioDialog
                  title={`预览 ${referencePreviewDialogAsset.name}`}
                  onClose={closeReferencePreview}
                >
                  {referencePreviewLoading ? (
                    <p role="status">正在读取素材预览…</p>
                  ) : referencePreviewError ? (
                    <p className="settings-error" role="alert">
                      {referencePreviewError}
                    </p>
                  ) : (
                    <Media
                      asset={referencePreviewMedia}
                      alt={referencePreviewDialogAsset.name}
                      className="creation-reference-dialog-media"
                      presentation="video"
                    />
                  )}
                </StudioDialog>
              ) : null}
            </ControlGroup>
          ) : (
            <ControlGroup
              label={
                <span className="creation-frame-heading">
                  <b className="creation-step-number">01</b> 首尾帧
                  <small>无首帧时文生视频；添加首帧后图生视频。</small>
                </span>
              }
            >
              {videoCapabilityPending && (
                <p role="status">正在读取视频生成能力，请稍候。</p>
              )}
              {videoCapabilityError && (
                <div>
                  <p className="settings-error" role="alert">
                    视频生成能力读取失败，请重试。
                  </p>
                  <Button onClick={retryVideoCapabilities} variant="outline">
                    重试读取视频能力
                  </Button>
                </div>
              )}
              {videoModeDisabled && !videoCapabilityError && (
                <div>
                  <p className="settings-error" role="alert">
                    {firstFrameId
                      ? "图生视频当前未开放，请等待能力开启后再提交。"
                      : "文生视频当前未开放，可添加首帧使用图生视频。"}
                  </p>
                  <Button onClick={retryVideoCapabilities} variant="outline">
                    刷新开放状态
                  </Button>
                </div>
              )}
              <div className="creation-frame-row">
                <VideoFrameCard
                  asset={firstFrame}
                  disabled={readOnly}
                  label="首帧"
                  optionalLabel="首帧（选填）"
                  picker="first-frame"
                  onChooseLibrary={openPicker}
                  onRemove={() => patchDraft({ firstFrameId: undefined })}
                  onUploaded={(asset) => {
                    appendMaterial(asset);
                    patchDraft({ firstFrameId: asset.id });
                  }}
                />
                <Icon name="arrow" />
                <VideoFrameCard
                  asset={tailFrame}
                  disabled={readOnly}
                  label="尾帧"
                  optionalLabel="尾帧（可选）"
                  picker="tail-frame"
                  onChooseLibrary={openPicker}
                  onRemove={() => patchDraft({ tailFrameId: undefined })}
                  onUploaded={(asset) => {
                    appendMaterial(asset);
                    patchDraft({ tailFrameId: asset.id });
                  }}
                />
              </div>
            </ControlGroup>
          )}
        </Panel>
        <Panel className="creation-video-composer">
          {state.draft.replicaPreparationPending && (
            <div role="status" className="creation-inline-error">
              <span>复刻准备尚未完成，请先交接新提示词与采用首帧。</span>
              <Button variant="outline" onClick={() => navigate("replica")}>
                返回复刻准备
              </Button>
            </div>
          )}
          <PromptEditor
            label="提示词"
            rows={24}
            showToolbarLabel
            toolbarLabel={
              <>
                <b className="creation-step-number">02</b> 画面描述
              </>
            }
            toolbarStart={
              <>
                <SavedPromptImporter
                  onImport={(promptText, context) => {
                    setShotTableImported(false);
                    patchVideoPrompt(promptText, {
                      importedPromptContext: context,
                      promptBindingsStale:
                        /<(Picture|Video|Audio)\s+\d+>|@\d+/.test(promptText),
                    });
                  }}
                />
                {!referenceMode ? (
                  <ShotTableImporter
                    disabled={readOnly}
                    onImport={(promptText) => {
                      setShotTableImported(true);
                      patchVideoPrompt(promptText, {
                        promptEdited: true,
                        importedPromptContext: undefined,
                        promptBindingsStale: false,
                      });
                    }}
                  />
                ) : null}
              </>
            }
            value={videoPrompt}
            readOnly={readOnly}
            optimizationDisabled={review}
            optimizationActionLabel={
              importedModeMismatch
                ? "按当前素材 AI 转换"
                : referenceMode
                  ? referenceStructured
                    ? "AI 优化"
                    : "生成标准提示词"
                  : shotTableImported
                    ? "AI 优化"
                    : undefined
            }
            formatStatus={
              referenceMode
                ? {
                    label: "六段式（H3 Ref2VA）",
                    title: "参考生视频（H3 Ref2VA）官方六段式结构",
                    ready: referenceStructured,
                    hint: promptBusy
                      ? "AI 正在按参考素材生成六段式提示词…通常 10–30 秒，生成完成前不建议提交。"
                      : promptAlreadyOptimized
                        ? "格式已就绪，请核对下方核对区再提交。"
                        : referenceStructured
                          ? "已识别为六段式（你手写或从提示词库导入）；AI 优化只改文风与措辞，不改结构、编号与引用规则。"
                          : "直接用一句话写需求即可：六段结构、素材编号（<Picture 1>/<Video 1>/<Audio 1>）与排除规则由系统生成。",
                  }
                : undefined
            }
            assetNames={Object.fromEntries(
              references.map((asset) => [
                asset.assetId ?? asset.id,
                asset.name,
              ]),
            )}
            onPurposeChange={(assetId, purpose) => {
              const asset = references.find(
                (item) => (item.assetId ?? item.id) === assetId,
              );
              if (asset)
                patchDraft({
                  referencePurposes: {
                    ...state.draft.referencePurposes,
                    [asset.id]: purpose,
                  },
                });
            }}
            onOptimizationBusyChange={setPromptBusy}
            runSignal={regenerateSignal}
            scope={`${user.id}:${state.page}`}
            onChange={(text) => {
              if (
                /^(?:integrated_multimodal_description|subject_definitions)\s*:/m.test(
                  text,
                )
              )
                setShotTableImported(false);
              patchVideoPrompt(text, {
                promptEdited: true,
                importedPromptContext: undefined,
              });
            }}
            placeholder="描述镜头、场景、运动与光线"
            context={{
              route: referenceMode ? "reference" : "text_image",
              duration_seconds: state.draft.duration,
              ratio: state.draft.ratio as GenerationRatio,
              resolution: state.draft.resolution as "768P" | "2K",
              first_frame_asset_id: referenceMode
                ? undefined
                : state.draft.firstFrameId,
              last_frame_asset_id: referenceMode
                ? undefined
                : state.draft.tailFrameId,
              references: referenceMode
                ? references.map((asset) => ({
                    asset_id: asset.assetId ?? asset.id,
                    purpose:
                      state.draft.referencePurposes?.[asset.id] ||
                      "unspecified",
                  }))
                : [],
            }}
          />
          {state.draft.promptBindingsStale && (
            <div role="alert" className="h3-prompt-stale">
              {importedModeMismatch
                ? "导入模板与当前生成模式不同，请点击上方“按当前素材 AI 转换”；转换完成后会自动更新素材引用。"
                : "参考素材已变化，请核对提示词的素材编号。"}
              {/* 素材一变，提示词里的编号就可能是旧的。与其让用户自己找按钮，
                  直接把生成入口放到告警条上；生成结果仍可撤销。 */}
              {referenceMode && !importedModeMismatch ? (
                <Button
                  variant="outline"
                  disabled={readOnly || promptBusy || !videoPrompt.trim()}
                  onClick={() => setRegenerateSignal((value) => value + 1)}
                >
                  <Icon name="sparkles" size={16} />
                  重新生成标准提示词
                </Button>
              ) : null}
              <Button
                variant="quiet"
                onClick={() => patchDraft({ promptBindingsStale: false })}
                disabled={readOnly}
              >
                我已手动核对
              </Button>
            </div>
          )}
          {state.draft.importedPromptContext && (
            <div className="creation-prompt-import-context" role="status">
              <strong>
                导入模板：{promptModeLabel(importedPromptMode)}；当前模式：
                {promptModeLabel(currentPromptMode)}。
              </strong>
              <span>
                {importedModeMismatch
                  ? "模板素材类型与当前选择不同，需要重新绑定。可使用上方 AI 转换，或手动修改后核对。"
                  : "请核对模板中的素材编号是否与当前列表顺序一致。"}
              </span>
            </div>
          )}
        </Panel>

        <Panel className="creation-video-controls">
          <div className="creation-panel-title">
            <span className="creation-step-number">03</span> 生成参数
          </div>
          <ParameterControls />
        </Panel>
        <Panel className="creation-video-preview">
          <div className="creation-panel-title">
            {referenceMode ? "参考预览" : "首帧预览"}
            <small className="creation-preview-ratio">
              {(referenceMode ? references.length > 0 : Boolean(firstFrame))
                ? "原图比例"
                : "9:16"}
            </small>
          </div>
          {!referenceMode && firstFrameLoading ? (
            <Empty
              title="正在加载首帧预览"
              description="正在读取已确认置换首帧的签名地址。"
            />
          ) : !referenceMode && firstFrameError ? (
            <Empty
              title="首帧预览加载失败"
              description={firstFrameError}
              action={
                <Button
                  variant="outline"
                  onClick={() =>
                    setFirstFrameLoadAttempt((attempt) => attempt + 1)
                  }
                >
                  重试加载首帧
                </Button>
              }
            />
          ) : videoTask ? (
            <>
              <VideoProgressView task={videoTask} />
              {videoTask.status === "completed" ? (
                <Button
                  variant="primary"
                  onClick={() =>
                    navigate("task-detail", {
                      selectedTaskId: videoTask.id,
                      selectedTaskKind: videoTask.backendKind,
                      selectedTaskBackendId:
                        videoTask.backendId ??
                        videoTask.batchId ??
                        videoTask.id,
                      returnTo: state.page,
                    })
                  }
                >
                  查看成片
                </Button>
              ) : null}
            </>
          ) : referenceMode ? (
            references.length ? (
              <Media
                asset={selectedReferencePreviewAsset}
                alt="参考画布"
                className="creation-preview-media"
                presentation="video"
              />
            ) : (
              <Media
                alt="还没有参考素材"
                className="creation-preview-media"
                fallback={
                  <Empty
                    title="还没有参考素材"
                    description="设置参考素材与参数后再生成视频。"
                  />
                }
              />
            )
          ) : firstFrame ? (
            <Media
              asset={firstFrame}
              alt="首帧预览"
              className="creation-preview-media"
              presentation="video"
            />
          ) : (
            <Media
              alt="当前为文生视频"
              className="creation-preview-media"
              fallback={
                <Empty
                  title="当前为文生视频"
                  description="添加首帧后会在这里显示图生预览。"
                />
              }
            />
          )}
          {videoTask && (
            <Hint>成片与历史进度可在任务中心查看，任务记录不会丢失。</Hint>
          )}
          <div className="creation-preview-footer">
            <Hint>生成后可在此查看视频</Hint>
            <Button variant="quiet" onClick={() => navigate("tasks")}>
              前往任务中心 <Icon name="arrow" />
            </Button>
          </div>
        </Panel>
      </div>
      <div className="creation-video-bottom-bar">
        <div>
          <strong>
            {referenceMode
              ? "参考生视频"
              : firstFrameId
                ? "图生视频"
                : "文生视频"}{" "}
            · {state.draft.resolution} · {state.draft.duration} 秒 · {(() => {
              // 底栏展示的比例必须与实际提交值一致（图生恒为自动，
              // 文生回落具体比例），不能在扣费确认处误导用户。
              const submitted = resolveSubmittedRatio(
                resolveVideoMode(
                  state.page,
                  Boolean(firstFrameId),
                  Boolean(state.draft.tailFrameId),
                ),
                state.draft.ratio,
              );
              return submitted === "adaptive" ? "自动" : submitted;
            })()}
          </strong>
          <Hint>提交前确认费用；生成结果进入任务中心。</Hint>
        </div>
        {generationActions}
      </div>
    </section>
  );
}

function PersonIdentity({ person }: { person?: StudioPerson }) {
  const { openPicker, user } = useStudio();
  const readOnly = user.role === "auditor";
  return (
    <div className="creation-identity-row">
      <span>人物 IP</span>
      <strong>{person ? `${person.name} · ${person.role}` : "未选择"}</strong>
      <Button
        variant="outline"
        disabled={readOnly}
        onClick={() => openPicker("person")}
      >
        更换 IP
      </Button>
    </div>
  );
}

function avatarDisplayName(person: StudioPerson | undefined, name: string) {
  if (!person || name.startsWith(person.name)) return name;
  return `${person.name} · ${name}`;
}

export function OralPage() {
  const {
    state,
    data,
    patchDraft,
    navigate,
    openPicker,
    saveDraft,
    requestGeneration,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const person = activePerson(data.people, state.draft.ipId);
  const avatar = person?.avatars.find(
    (item) =>
      item.id === state.draft.avatarId &&
      item.ready &&
      item.origin === "视频制作",
  );
  const voice = person?.voices.find(
    (item) => item.id === state.draft.voiceId && item.confirmed,
  );
  const scriptReady = Boolean(
    state.draft.script.confirmed && state.draft.script.text.trim(),
  );
  const ready = Boolean(person && avatar && voice && scriptReady);
  const manage = (page: "person-avatars" | "person-voices") =>
    navigate(page, { returnTo: "oral", selectedPersonId: state.draft.ipId });
  return (
    <section className="creation-page creation-oral oral-composer">
      <header className="creation-heading creation-heading-back">
        <Button
          variant="quiet"
          onClick={() => navigate(state.returnTo ?? "workbench")}
        >
          ← 返回
        </Button>
        <div>
          <h1>数字人口播</h1>
          <p>选好视频分身和自己的声音，让文案成为新口播。</p>
        </div>
      </header>
      <CreationNavigation />
      <OralJourney step={3} />
      <div className="oral-composer-grid">
        <Panel className="oral-source-panel">
          <PersonIdentity person={person} />
          <div className="creation-panel-title-row">
            <span>视频分身</span>
            <Button
              variant="outline"
              disabled={readOnly}
              onClick={() => openPicker("avatar")}
            >
              更换分身
            </Button>
          </div>
          {avatar ? (
            <>
              <Media
                asset={findAsset(data.assets, avatar.imageId)}
                alt={avatar.name}
                className="creation-avatar-preview"
                presentation="video"
              />
              <div className="creation-avatar-meta">
                <strong>{avatarDisplayName(person, avatar.name)}</strong>
                <small>已就绪 · 真人视频创建</small>
              </div>
              <span className="oral-source-label">
                分身来源预览，成片会按文案和声音重新生成。
              </span>
              <Button variant="quiet" onClick={() => manage("person-avatars")}>
                管理口播分身
              </Button>
            </>
          ) : (
            <Empty
              title="还没有可用口播分身"
              description="请先上传当前人物的真人视频创建分身。"
              action={
                <Button
                  variant="outline"
                  onClick={() => manage("person-avatars")}
                >
                  去人物库制作口播分身
                </Button>
              }
            />
          )}
          <div className="oral-voice-choice">
            <div className="creation-panel-title-row">
              <span>克隆声音</span>
              <Button
                variant="outline"
                disabled={readOnly}
                onClick={() => openPicker("voice")}
              >
                更换声音
              </Button>
            </div>
            <div className="creation-voice-row">
              <strong>{voice?.name ?? "未选择已确认声音"}</strong>
              <small>{voice ? "已试听确认" : "需在声音档案中试听确认"}</small>
            </div>
            {voice?.url ? (
              <audio
                controls
                preload="none"
                src={voice.url}
                aria-label={`${voice.name}试听`}
              >
                <track kind="captions" label="声音样本" />
              </audio>
            ) : null}
            <Button variant="quiet" onClick={() => manage("person-voices")}>
              管理声音
            </Button>
          </div>
        </Panel>
        <Panel className="oral-script-panel">
          <div className="oral-panel-heading">
            <h2>口播文案</h2>
            <span className="oral-source-label">
              {scriptReady ? "已确认终稿" : "待确认终稿"}
            </span>
          </div>
          <div className="creation-readonly-script">
            <div>
              <span>来源：文案工坊</span>
              <strong>V{state.draft.script.version}</strong>
            </div>
            <h3>{state.draft.script.title || "未命名作品"}</h3>
            <p>{state.draft.script.text || "去文案工坊填写并确认口播文案。"}</p>
            <button
              type="button"
              onClick={() => navigate("copy", { returnTo: "oral" })}
            >
              去文案工坊修改
            </button>
          </div>
          <div className="oral-script-meta">
            <span>{state.draft.script.text.trim().length} 字</span>
            <span>成片时长随口播内容确定</span>
          </div>
          <div className="creation-style-controls">
            <span>标准口播</span>
            <span>字幕</span>
            <Button
              disabled={readOnly}
              variant={!state.draft.subtitles ? "outline" : "quiet"}
              onClick={() => patchDraft({ subtitles: false })}
            >
              不添加
            </Button>
            <Button
              disabled={readOnly}
              variant={state.draft.subtitles ? "outline" : "quiet"}
              onClick={() => patchDraft({ subtitles: true })}
            >
              添加
            </Button>
          </div>
          <div className="oral-readiness">
            {[
              ["视频分身", Boolean(avatar)],
              ["克隆声音", Boolean(voice)],
              ["文案终稿", scriptReady],
            ].map(([label, complete]) => (
              <span key={String(label)} className={complete ? "is-ready" : ""}>
                <Icon name={complete ? "check" : "clock"} size={14} />
                {label}
                {complete ? "已就绪" : "待准备"}
              </span>
            ))}
          </div>
          <footer className="creation-action-bar">
            <Button variant="outline" disabled={readOnly} onClick={saveDraft}>
              保存草稿
            </Button>
            <Button
              variant="primary"
              disabled={readOnly || !ready}
              onClick={() => requestGeneration("数字人口播")}
            >
              生成口播视频
            </Button>
          </footer>
          <Hint>确认预计费用后提交，生成进度可在任务中心查看。</Hint>
        </Panel>
      </div>
    </section>
  );
}
