import { useCallback, useEffect, useRef, useState } from "react";
import {
  type CustomerProfile,
  createViralImportTask,
  customerVisibleErrorMessage,
  getPublishSummary,
  getStudioNotificationPreferences,
  getViralImportTask,
  resolveViralLink,
  updateStudioNotificationPreferences,
  type ViralImportTask,
} from "../api";
import { VideoPreview } from "../VideoPreview";
import { useStudio } from "./context";
import { LocalPublishAccountsPanel as PublishAccountsPanel } from "./LocalPublishAccountsPanel";
import {
  cancelStudioTask,
  downloadStudioTaskResult,
  loadMoreGenerationTasks,
  loadMoreOralTasks,
  loadStudioTaskDetail,
  loadTaskPreview,
  removeStudioGenerationTask,
  renameStudioGenerationTask,
  retryStudioTask,
  saveTaskPreview,
  studioVideoFromViral,
  uploadWorkbenchSourceVideo,
} from "./live";
import { PlatformLogo } from "./PlatformLogo";
import { readPageCache, writePageCache } from "./pageCache";
import { draftFromTask } from "./state";
import type { StudioAsset, StudioData, StudioTask, StudioVideo } from "./types";
import {
  Button,
  Empty,
  Field,
  FilterSelect,
  formatTaskTime,
  Hint,
  Icon,
  Media,
  Panel,
  StudioDialog,
  Tabs,
} from "./ui";
import {
  clearViralImportIdempotencyKey,
  shouldClearViralImportIdempotencyKey,
  ViralImportPollingTimeoutError,
  viralImportIdempotencyKey,
} from "./viralImport";

const statusNames: Record<StudioTask["status"], string> = {
  running: "生成中",
  queued: "排队中",
  failed: "待处理",
  completed: "已完成",
  uncertain: "状态待确认",
  cancelled: "已取消",
};

function taskDetailPatch(task: StudioTask, returnTo: "workbench" | "tasks") {
  return {
    selectedTaskId: task.id,
    selectedTaskKind: task.backendKind,
    selectedTaskBackendId: task.backendId ?? task.batchId ?? task.id,
    returnTo,
  };
}

/** 任务中心状态列的图标与子文案（与效果图一致：排队中"等待开始"、
 * 待处理"生成失败"），状态待确认保持独立提示避免误导重试。 */
const statusHints: Partial<Record<StudioTask["status"], string>> = {
  queued: "等待开始",
  failed: "生成失败",
  uncertain: "状态待确认",
};

const statusIcons: Record<StudioTask["status"], string> = {
  running: "clock",
  queued: "clock",
  failed: "warning",
  uncertain: "warning",
  completed: "check",
  cancelled: "close",
};

function formatWorkbenchLikes(video: StudioVideo) {
  if (video.likeDisplay) return video.likeDisplay;
  if (video.likes >= 10000) {
    return `${(video.likes / 10000).toFixed(1).replace(".0", "")}万`;
  }
  return video.likes.toLocaleString("zh-CN");
}

function persistWorkbenchViralDetailUrl(video: StudioVideo) {
  if (!video.platformKey || !video.nativeId) return;
  const url = new URL(window.location.href);
  url.searchParams.set("viralPlatform", video.platformKey);
  url.searchParams.set("viralVideoId", video.nativeId);
  window.history.replaceState(null, "", url);
}

function Status({ task }: { task: StudioTask }) {
  return (
    <span className={`studio-status studio-status--${task.status}`}>
      {statusNames[task.status]}
      {task.progress !== undefined && task.status === "running"
        ? ` ${task.progress}%`
        : ""}
    </span>
  );
}

function StatusCell({ task }: { task: StudioTask }) {
  const hint = statusHints[task.status];
  return (
    <div className="studio-status-cell">
      <span className={`studio-status studio-status--${task.status}`}>
        <Icon name={statusIcons[task.status]} size={16} />
        {statusNames[task.status]}
        {task.progress !== undefined && task.status === "running"
          ? ` ${task.progress}%`
          : ""}
      </span>
      {hint && <small>{hint}</small>}
    </div>
  );
}

/** Per-row overflow menu on the workbench "正在进行" list. Every action is
 * real: jump to the task center, or copy the server batch id for support. */
function RunningRowMenu({ task }: { task: StudioTask }) {
  const { navigate, notify } = useStudio();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const copyTaskId = async () => {
    try {
      await navigator.clipboard.writeText(
        task.backendId || task.batchId || task.id,
      );
      notify("任务编号已复制");
    } catch {
      notify("复制失败，请手动复制任务编号。");
    }
  };

  return (
    <div className="studio-row-menu" ref={rootRef}>
      <button
        type="button"
        className="studio-row-menu-trigger"
        aria-label={`更多操作：${task.title}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <Icon name="more" size={20} />
      </button>
      {open && (
        <div className="studio-row-menu-list" role="menu">
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              navigate("tasks");
            }}
          >
            打开任务中心
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              void copyTaskId();
            }}
          >
            复制任务编号
          </button>
        </div>
      )}
    </div>
  );
}

export function WorkbenchPage() {
  const {
    requireLogin,
    data,
    user,
    review,
    navigate,
    openLive,
    notify,
    patchDraft,
    updateData,
    state,
    extractScriptFromUpload,
    refresh,
  } = useStudio();
  const accountId = user.id || "anonymous";
  const accountGenerationRef = useRef({ accountId, generation: 0 });
  if (accountGenerationRef.current.accountId !== accountId) {
    accountGenerationRef.current = {
      accountId,
      generation: accountGenerationRef.current.generation + 1,
    };
  }
  const accountContextKey = `${accountId}:${accountGenerationRef.current.generation}`;
  // PUBLISH-DELIVERY-20260917: the home metric reads real publish records;
  // null keeps the honest "—" until the summary arrives (or fails).
  const [publishedTotal, setPublishedTotal] = useState<number | null>(null);
  useEffect(() => {
    let active = true;
    setPublishedTotal(null);
    if (review || !user.id) return;
    void (async () => {
      // MATERIAL-PERF-C（P1-1）：先回放上次摘要（切页立即出数），再后台刷新。
      const cached = readPageCache<number>(`publish-summary:${user.id}`);
      if (cached !== undefined && active) setPublishedTotal(cached);
      try {
        const summary = await getPublishSummary();
        writePageCache(`publish-summary:${user.id}`, summary.published_total);
        if (active) setPublishedTotal(summary.published_total);
      } catch {
        if (active && cached === undefined) setPublishedTotal(null);
      }
    })();
    return () => {
      active = false;
    };
  }, [review, user.id]);
  const [sourceLinkState, setSourceLinkState] = useState({
    accountContextKey,
    value: "",
  });
  const sourceLink =
    sourceLinkState.accountContextKey === accountContextKey
      ? sourceLinkState.value
      : "";
  const [uploadState, setUpload] = useState<{
    accountContextKey: string;
    name: string;
    progress: number;
    error: string;
    projectId: string | null;
    completed: boolean;
  } | null>(null);
  const upload =
    uploadState?.accountContextKey === accountContextKey ? uploadState : null;
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadOperationRef = useRef(0);
  const linkOperationRef = useRef(0);
  const viralOperationRef = useRef(0);
  const viralIdempotencyKeysRef = useRef(new Map<string, string>());
  const uploadAbortRef = useRef<AbortController | null>(null);
  const [viralImportingId, setViralImportingId] = useState<string>();
  const [linkStateValue, setLinkState] = useState<{
    accountContextKey: string;
    status: "idle" | "loading" | "error";
    stage?: "resolve" | "import";
    message?: string;
  }>({ accountContextKey, status: "idle" });
  const linkState =
    linkStateValue.accountContextKey === accountContextKey
      ? linkStateValue
      : ({ accountContextKey, status: "idle" } as const);
  const activeAccountRef = useRef(accountId);
  if (activeAccountRef.current !== accountId) {
    activeAccountRef.current = accountId;
    uploadOperationRef.current += 1;
    linkOperationRef.current += 1;
    viralOperationRef.current += 1;
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = null;
  }
  useEffect(
    () => () => {
      uploadOperationRef.current += 1;
      linkOperationRef.current += 1;
      viralOperationRef.current += 1;
      uploadAbortRef.current?.abort();
    },
    [],
  );
  useEffect(() => {
    if (activeAccountRef.current !== accountId) return;
    setViralImportingId(undefined);
  }, [accountId]);
  const active = data.tasks.filter((task) =>
    ["running", "queued", "uncertain"].includes(task.status),
  );
  const featuredVideos = [...(data.homepageVideos ?? [])]
    .filter((video) => video.homepageFeatured === true)
    .sort((left, right) => {
      if (left.homepageRank !== null && left.homepageRank !== undefined) {
        if (right.homepageRank === null || right.homepageRank === undefined)
          return -1;
        if (left.homepageRank !== right.homepageRank)
          return left.homepageRank - right.homepageRank;
      } else if (
        right.homepageRank !== null &&
        right.homepageRank !== undefined
      ) {
        return 1;
      }
      return (
        right.likes - left.likes ||
        left.id.localeCompare(right.id, undefined, { numeric: true })
      );
    })
    .slice(0, 5);
  const beginViralCreation = async (
    video: StudioVideo,
    purpose: "copy" | "replica" = "replica",
    providedIdempotencyKey?: string,
    requestAccount = accountId,
  ) => {
    if (review) {
      patchDraft({ sourceId: video.id });
      navigate(purpose, {
        selectedVideoId: video.id,
        returnTo: "workbench",
      });
      return;
    }
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少可导入的平台标识");
      return;
    }
    const operation = ++viralOperationRef.current;
    const actionKey = `${video.platformKey}:${video.nativeId}:${purpose}`;
    const memoryKey = `${requestAccount}:${actionKey}`;
    const idempotencyKey =
      providedIdempotencyKey ??
      viralIdempotencyKeysRef.current.get(memoryKey) ??
      viralImportIdempotencyKey(requestAccount, actionKey);
    viralIdempotencyKeysRef.current.set(memoryKey, idempotencyKey);
    const clearKey = () => {
      viralIdempotencyKeysRef.current.delete(memoryKey);
      clearViralImportIdempotencyKey(requestAccount, actionKey, idempotencyKey);
    };
    setViralImportingId(video.id);
    try {
      let task: ViralImportTask = await createViralImportTask(
        video.platformKey,
        video.nativeId,
        purpose,
        idempotencyKey,
      );
      if (
        operation !== viralOperationRef.current ||
        requestAccount !== activeAccountRef.current
      )
        return;
      for (let attempt = 0; attempt < 120; attempt += 1) {
        if (
          operation !== viralOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        if (task.status === "SUCCEEDED" || task.status === "FAILED") break;
        const taskId = task.taskId ?? task.id;
        if (!taskId) throw new Error("导入任务缺少任务 ID");
        await new Promise<void>((resolve) => window.setTimeout(resolve, 1_000));
        if (
          operation !== viralOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        task = await getViralImportTask(taskId);
      }
      if (
        operation !== viralOperationRef.current ||
        requestAccount !== activeAccountRef.current
      )
        return;
      if (task.status !== "SUCCEEDED" && task.status !== "FAILED") {
        throw new ViralImportPollingTimeoutError(
          "导入任务等待超时，请稍后重试",
        );
      }
      if (task.status !== "SUCCEEDED") {
        if (task.retryable === false) clearKey();
        throw new Error(
          task.errorMessage || task.error || task.message || "参考素材导入失败",
        );
      }
      if (!task.projectId || !task.sourceAssetId) {
        clearKey();
        throw new Error("导入任务缺少项目或素材结果");
      }
      if (purpose === "replica" && !task.canAnalyze) {
        clearKey();
        throw new Error("该来源暂不支持视频复刻");
      }
      if (purpose === "copy" && !task.canTranscribe) {
        clearKey();
        throw new Error("该来源暂不支持提取文案");
      }
      patchDraft({
        projectId: task.projectId,
        sourceId: task.sourceAssetId,
        sourceAssetId: task.sourceAssetId,
      });
      refresh();
      if (purpose === "copy") {
        extractScriptFromUpload(task.projectId, task.sourceAssetId);
      } else {
        navigate("replica", {
          selectedVideoId: video.id,
          returnTo: "workbench",
        });
      }
    } catch (error) {
      if (shouldClearViralImportIdempotencyKey(error)) clearKey();
      if (
        operation === viralOperationRef.current &&
        requestAccount === activeAccountRef.current
      ) {
        notify(error instanceof Error ? error.message : "参考素材导入失败");
      }
    } finally {
      if (
        operation === viralOperationRef.current &&
        requestAccount === activeAccountRef.current
      )
        setViralImportingId(undefined);
    }
  };
  const pendingViralSource = !state.draft.projectId
    ? data.videos.find((video) => video.id === state.draft.sourceId)
    : undefined;
  const begin = async (mode: "copy" | "replica") => {
    if (requireLogin) {
      requireLogin();
      return;
    }
    if (review) {
      navigate(mode);
      return;
    }
    if (sourceLink.trim()) {
      if (linkState.status === "loading") return;
      const operation = ++linkOperationRef.current;
      const requestAccount = accountId;
      const linkActionKey = `link:${sourceLink.trim()}:${mode}`;
      const resolutionKey = viralImportIdempotencyKey(
        requestAccount,
        linkActionKey,
      );
      setLinkState({
        accountContextKey,
        status: "loading",
        stage: "resolve",
      });
      try {
        const resolution = await resolveViralLink(
          sourceLink.trim(),
          mode,
          resolutionKey,
        );
        if (
          operation !== linkOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        const video = studioVideoFromViral(resolution.item);
        updateData((current) => ({
          ...current,
          videos: [
            video,
            ...current.videos.filter((candidate) => candidate.id !== video.id),
          ],
        }));
        setLinkState({ accountContextKey, status: "loading", stage: "import" });
        await beginViralCreation(
          video,
          mode,
          resolution.importIdempotencyKey,
          requestAccount,
        );
        if (
          operation === linkOperationRef.current &&
          requestAccount === activeAccountRef.current
        ) {
          setLinkState({ accountContextKey, status: "idle" });
        }
      } catch (error) {
        if (
          operation !== linkOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        const requestError = error as { code?: string; status?: number };
        if (
          requestError.code !== "VIRAL_LINK_IN_PROGRESS" &&
          requestError.code !== "VIRAL_LINK_SUBMISSION_UNCERTAIN" &&
          (requestError.code === "VIRAL_LINK_MEDIA_DNS_UNAVAILABLE" ||
            (requestError.status !== undefined &&
              requestError.status >= 400 &&
              requestError.status < 500))
        ) {
          clearViralImportIdempotencyKey(
            requestAccount,
            linkActionKey,
            resolutionKey,
          );
        }
        setLinkState({
          accountContextKey,
          status: "error",
          message:
            error instanceof Error && error.message.trim()
              ? error.message.trim()
              : "视频链接解析失败，请上传 MP4/MOV 文件。",
        });
      }
      return;
    }
    if (upload?.projectId || state.draft.sourceId || state.draft.projectId) {
      // 上传完成的来源已经写进当前草稿：复刻直接进分镜工作区；
      // 文案提取走 script-from-audio 异步管线（抽音轨→转写→回填草稿）。
      if (mode === "replica") {
        navigate("replica");
        return;
      }
      extractScriptFromUpload();
      return;
    }
    openLive("projects");
  };
  const handleUploadFile = (file: File) => {
    if (requireLogin) {
      requireLogin();
      return;
    }
    if (!/\.(mp4|mov)$/i.test(file.name)) {
      notify("目前仅支持 MP4 / MOV 视频文件。");
      return;
    }
    const operation = ++uploadOperationRef.current;
    const requestAccount = accountId;
    uploadAbortRef.current?.abort();
    const abortController = new AbortController();
    uploadAbortRef.current = abortController;
    setUpload({
      accountContextKey,
      name: file.name,
      progress: 0,
      error: "",
      projectId: null,
      completed: false,
    });
    void uploadWorkbenchSourceVideo(
      file,
      (progress) => {
        if (
          operation !== uploadOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        setUpload((current) =>
          current?.accountContextKey === accountContextKey
            ? { ...current, progress }
            : current,
        );
      },
      abortController.signal,
      // 首页上传尚未选择复刻或文案提取；只存素材，复刻入口再恢复/启动视觉分析。
      "script",
    )
      .then((uploaded) => {
        if (
          operation !== uploadOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        const { projectId, assetId } = uploaded;
        setUpload((current) =>
          current?.accountContextKey === accountContextKey
            ? { ...current, progress: 100, projectId, completed: true }
            : current,
        );
        patchDraft({
          projectId,
          sourceId: assetId,
          sourceAssetId: assetId,
          analysisTaskId: uploaded.analysisTaskId,
          analysisTaskStatus: uploaded.analysisTaskStatus,
          firstFrameId: undefined,
          firstFrameSelectionVersionId: undefined,
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
        notify("视频已上传，来源已加入当前创作。");
      })
      .catch((error) => {
        if (
          operation !== uploadOperationRef.current ||
          requestAccount !== activeAccountRef.current
        )
          return;
        setUpload((current) =>
          current?.accountContextKey === accountContextKey
            ? {
                ...current,
                error:
                  error instanceof Error && error.message.trim()
                    ? error.message.trim()
                    : "上传失败，请重试。",
              }
            : current,
        );
      });
  };
  return (
    <section className="studio-home">
      <h2 className="studio-home-page-title">工作台</h2>
      <header className="studio-hero">
        <h1>
          粘贴一条爆款乡墅视频链接，
          <strong>快速生成它的原创视频</strong>
        </h1>
      </header>
      <div className="studio-start">
        <div className="studio-source-input">
          <button
            type="button"
            className="studio-upload-icon"
            aria-label="上传视频"
            onClick={() => {
              if (requireLogin) {
                requireLogin();
                return;
              }
              if (review) {
                notify("审核示例不执行真实上传。");
                return;
              }
              fileInputRef.current?.click();
            }}
          >
            <Icon name="upload" />
          </button>
          <input
            aria-label="视频链接"
            placeholder="粘贴抖音或小红书视频链接"
            value={sourceLink}
            onChange={(event) => {
              linkOperationRef.current += 1;
              viralOperationRef.current += 1;
              setSourceLinkState({
                accountContextKey,
                value: event.target.value,
              });
              setLinkState({ accountContextKey, status: "idle" });
              setViralImportingId(undefined);
            }}
          />
          <input
            ref={fileInputRef}
            type="file"
            aria-label="选择视频文件"
            accept=".mp4,.mov,video/mp4,video/quicktime"
            hidden
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) handleUploadFile(file);
              event.target.value = "";
            }}
          />
          <Button
            disabled={linkState.status === "loading"}
            variant="primary"
            onClick={() => void begin("copy")}
          >
            <Icon name="pen" />
            提取文案
          </Button>
          <Button
            disabled={linkState.status === "loading"}
            onClick={() => void begin("replica")}
          >
            <Icon name="play" />
            {linkState.status === "loading" ? "正在解析链接…" : "开始复刻"}
          </Button>
        </div>
        {upload && (
          <p className="studio-upload-status" role="status">
            {upload.error
              ? `上传失败：${upload.error}`
              : upload.completed
                ? `已上传：${upload.name}`
                : `正在上传 ${upload.name}… ${upload.progress}%`}
          </p>
        )}
        <p className="studio-start-helper">
          支持抖音、小红书的视频链接；其他平台请上传 MP4/MOV 文件；视频复刻支持
          4-15 秒、50 MB 以内的视频。
        </p>
        {linkState.status === "loading" && (
          <p className="studio-upload-status" role="status">
            {linkState.stage === "import"
              ? "链接解析成功，正在导入视频素材并建立项目，请稍候…"
              : "正在解析链接并转存视频素材，通常十几秒内完成…"}
          </p>
        )}
        {linkState.status === "error" && (
          <p className="viral-media-status is-error" role="alert">
            {linkState.message}
          </p>
        )}
        {pendingViralSource && (
          <p className="studio-upload-status" role="status">
            已选参考：{pendingViralSource.title}。请上传该视频的 MP4 或 MOV
            文件， 上传完成后点击“提取文案”。
          </p>
        )}
      </div>
      <div className="studio-home-metrics">
        {[
          {
            label: "今日成片",
            value: data.stats ? String(data.stats.today_completed) : "—",
            hint: review ? "较昨日 +3" : "今日已完成",
            tone: "success",
            icon: "video",
            page: "tasks" as const,
          },
          {
            label: "成片队列",
            value: data.stats
              ? String(data.stats.running + data.stats.queued)
              : data.loading
                ? "—"
                : String(active.length),
            hint: "等待中",
            tone: "muted",
            icon: "tasks",
            page: "tasks" as const,
          },
          {
            label: "累计已发布",
            value: review
              ? "156"
              : publishedTotal === null
                ? "—"
                : String(publishedTotal),
            hint: review
              ? "本周 +21"
              : publishedTotal === null
                ? "等待发布统计"
                : "已发布作品",
            tone: "success",
            icon: "upload",
            page: "publishing" as const,
          },
          {
            label: "待处理",
            value: data.stats
              ? String(data.stats.needs_attention)
              : data.loading
                ? "—"
                : String(
                    data.tasks.filter((task) =>
                      ["failed", "uncertain"].includes(task.status),
                    ).length,
                  ),
            hint: "需要您处理",
            tone: "danger",
            icon: "warning",
            page: "tasks" as const,
          },
        ].map((metric) => (
          <button
            key={metric.label}
            type="button"
            className="studio-panel"
            onClick={() => navigate(metric.page)}
          >
            <span>
              <Icon name={metric.icon} />
              {metric.label}
            </span>
            <strong>{metric.value}</strong>
            <small className={`studio-metric-hint is-${metric.tone}`}>
              {metric.hint}
            </small>
          </button>
        ))}
      </div>
      <div className="studio-home-grid">
        <div className="studio-home-cell" data-area="running">
          <Panel className="studio-running-list">
            <h2>正在进行</h2>
            {active.length ? (
              active.slice(0, 2).map((task) => (
                <div className="studio-running-row" key={task.id}>
                  {task.poster ? (
                    <VideoPreview poster={task.poster} alt={task.title} />
                  ) : (
                    <Icon name="video" size={46} />
                  )}
                  <div>
                    <h3>{task.title}</h3>
                    <p>{task.type}</p>
                  </div>
                  <div className="studio-running-progress">
                    <span
                      className={`studio-status studio-status--${task.status}`}
                    >
                      {statusNames[task.status]}
                    </span>
                    {task.progress !== undefined &&
                      task.status === "running" && (
                        <>
                          <progress value={task.progress} max={100} />
                          <span className="studio-running-percent">
                            {task.progress}%
                          </span>
                        </>
                      )}
                  </div>
                  <Button
                    onClick={() =>
                      navigate(
                        "task-detail",
                        taskDetailPatch(task, "workbench"),
                      )
                    }
                  >
                    查看详情
                  </Button>
                  <RunningRowMenu task={task} />
                </div>
              ))
            ) : (
              <Empty
                title={data.loading ? "正在读取任务" : "还没有进行中的任务"}
                description="从上传视频或选择素材开始创作"
                action={
                  data.loading ? undefined : (
                    <Button onClick={() => fileInputRef.current?.click()}>
                      上传视频，开始第一支创作
                    </Button>
                  )
                }
              />
            )}
          </Panel>
        </div>
        <div className="studio-home-cell" data-area="activity">
          <Panel className="studio-activity">
            <h2>任务动态</h2>
            {data.tasks.slice(0, 4).map((task) => (
              <button
                type="button"
                key={task.id}
                onClick={() =>
                  navigate("task-detail", taskDetailPatch(task, "workbench"))
                }
              >
                <i className={`studio-dot studio-dot--${task.status}`} />
                <span>
                  “{task.title}” {statusNames[task.status]}
                  <small>{formatTaskTime(task.submitted)}</small>
                </span>
              </button>
            ))}
            {!data.tasks.length && (
              <Empty
                title={data.loading ? "正在读取任务" : "暂无任务动态"}
                description="任务提交后这里会实时更新"
              />
            )}
            {data.tasks.length > 4 && (
              <Button variant="quiet" onClick={() => navigate("tasks")}>
                进入任务中心
              </Button>
            )}
          </Panel>
        </div>
        <div className="studio-home-cell" data-area="viral">
          <Panel className="studio-viral-panel">
            <div className="studio-home-section-heading">
              <h2>爆款视频精选</h2>
              <Button
                variant="quiet"
                aria-label="查看全部爆款"
                onClick={() => navigate("viral")}
              >
                查看全部爆款
                <Icon name="arrow" size={16} />
              </Button>
            </div>
            {featuredVideos.length ? (
              <div className="studio-home-viral-grid">
                {featuredVideos.map((video) => (
                  <article className="studio-home-viral-card" key={video.id}>
                    <button
                      type="button"
                      className="studio-home-viral-cover"
                      aria-label={`打开视频详情：${video.title}`}
                      onClick={() => {
                        navigate("viral-detail", {
                          selectedVideoId: video.id,
                          returnTo: "workbench",
                        });
                        persistWorkbenchViralDetailUrl(video);
                      }}
                    >
                      <VideoPreview poster={video.poster} />
                      <span className="studio-home-viral-platform">
                        <PlatformLogo platform={video.platform} size={18} />{" "}
                        {video.platform}
                      </span>
                      <span className="studio-home-viral-duration">
                        {video.duration}
                      </span>
                    </button>
                    <h3 title={video.title}>{video.title}</h3>
                    <div className="studio-home-viral-meta">
                      <span>
                        <Icon name="fire" size={14} />
                        热度 {formatWorkbenchLikes(video)}
                      </span>
                    </div>
                    <div className="studio-home-viral-actions">
                      <Button
                        variant="quiet"
                        aria-label={`查看详情：${video.title}`}
                        onClick={() => {
                          navigate("viral-detail", {
                            selectedVideoId: video.id,
                            returnTo: "workbench",
                          });
                          persistWorkbenchViralDetailUrl(video);
                        }}
                      >
                        查看详情
                      </Button>
                      <Button
                        variant="quiet"
                        aria-label={`提取文案：${video.title}`}
                        disabled={viralImportingId === video.id}
                        onClick={() => void beginViralCreation(video, "copy")}
                      >
                        {viralImportingId === video.id ? "导入中…" : "提取文案"}
                      </Button>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <Empty
                title={data.loading ? "正在读取爆款灵感" : "暂无爆款灵感"}
                description="前往爆款视频页查看更多乡墅参考作品"
              />
            )}
          </Panel>
        </div>
        <div className="studio-home-cell" data-area="shortcuts">
          <Panel className="studio-shortcuts">
            <h2>快捷入口</h2>
            <div className="studio-shortcuts-grid">
              <button
                type="button"
                aria-label="快捷入口：文案工坊"
                onClick={() => navigate("copy")}
              >
                <Icon name="pen" size={28} />
                <span>
                  文案工坊<small>创作爆款文案</small>
                </span>
                <Icon name="chevron" />
              </button>
              <button
                type="button"
                aria-label="快捷入口：人物库"
                onClick={() => navigate("people")}
              >
                <Icon name="person" size={28} />
                <span>
                  人物库<small>管理数字人</small>
                </span>
                <Icon name="chevron" />
              </button>
              <button
                type="button"
                aria-label="快捷入口：素材库"
                onClick={() => navigate("materials")}
              >
                <Icon name="folder" size={28} />
                <span>
                  素材库<small>管理我的素材</small>
                </span>
                <Icon name="chevron" />
              </button>
              <button
                type="button"
                aria-label="快捷入口：数据看板"
                onClick={() => navigate("analytics")}
              >
                <Icon name="chart" size={28} />
                <span>
                  数据看板<small>查看创作数据</small>
                </span>
                <Icon name="chevron" />
              </button>
            </div>
          </Panel>
        </div>
      </div>
    </section>
  );
}

export function TasksPage() {
  const { data, navigate, openLive, review, notify, refresh, updateData } =
    useStudio();
  const [status, setStatus] = useState("all");
  const [kind, setKind] = useState("全部");
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [renameSavingId, setRenameSavingId] = useState<string | null>(null);
  const [pendingRemove, setPendingRemove] = useState<StudioTask | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [loadingHistory, setLoadingHistory] = useState<
    "generation" | "oral" | null
  >(null);
  const matchesStatus = (task: StudioTask) =>
    status === "all" ||
    (status === "active"
      ? ["running", "queued"].includes(task.status)
      : status === "attention"
        ? ["failed", "uncertain"].includes(task.status)
        : task.status === "completed");
  const tasks = data.tasks.filter(
    (task) => matchesStatus(task) && (kind === "全部" || kind === task.type),
  );
  // 类型菜单计数与状态筛选联动：展示"当前状态下各类型还有几个"。
  const kindItems = [
    "全部",
    "视频复刻",
    "视频生成",
    "数字人口播",
    "人物置换",
  ].map((label) => ({
    id: label,
    label: label === "全部" ? "全部类型" : label,
    count: data.tasks.filter(
      (task) =>
        matchesStatus(task) && (label === "全部" || label === task.type),
    ).length,
  }));
  const activeCount = data.tasks.filter((task) =>
    ["running", "queued"].includes(task.status),
  ).length;
  const attentionCount = data.tasks.filter((task) =>
    ["failed", "uncertain"].includes(task.status),
  ).length;
  const generationPage = data.pagination?.generationTasks;
  const oralPage = data.pagination?.oralTasks;
  const appendTasks = (
    incoming: StudioTask[],
    pagination: NonNullable<StudioData["pagination"]>,
  ) => {
    updateData((current) => {
      const known = new Set(current.tasks.map((task) => task.id));
      return {
        ...current,
        tasks: [
          ...current.tasks,
          ...incoming.filter((task) => !known.has(task.id)),
        ],
        pagination: { ...current.pagination, ...pagination },
      };
    });
  };
  const loadGenerationHistory = async () => {
    if (review || loadingHistory || !generationPage?.nextCursor) return;
    setLoadingHistory("generation");
    try {
      const result = await loadMoreGenerationTasks(generationPage.nextCursor);
      appendTasks(result.items, {
        generationTasks: {
          nextCursor: result.nextCursor,
          total: result.total,
        },
      });
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "加载更多普通批次失败，请重试。",
      );
    } finally {
      setLoadingHistory(null);
    }
  };
  const loadOralHistory = async () => {
    if (
      review ||
      loadingHistory ||
      !oralPage ||
      oralPage.loaded >= oralPage.total
    )
      return;
    setLoadingHistory("oral");
    try {
      const result = await loadMoreOralTasks(oralPage.loaded);
      appendTasks(result.items, {
        oralTasks: { loaded: result.loaded, total: result.total },
      });
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "加载更多口播任务失败，请重试。",
      );
    } finally {
      setLoadingHistory(null);
    }
  };
  const cancelTask = async (task: StudioTask) => {
    if (review) {
      notify("审核示例不执行真实取消。");
      return;
    }
    setCancellingId(task.id);
    try {
      const result = await cancelStudioTask(task);
      notify(
        result.billingStatus === "RELEASED" ||
          result.billingStatus === "RELEASE"
          ? "任务已取消，预扣积分已退回。"
          : result.billingStatus
            ? "任务已取消，计费状态处理中，请稍后刷新核对。"
            : "任务已取消，请刷新核对计费状态。",
      );
      refresh();
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "取消失败，请重试。",
      );
    } finally {
      setCancellingId(null);
    }
  };
  const removeTask = async (task: StudioTask) => {
    if (removingId) return;
    setRemovingId(task.id);
    try {
      // 审核示例没有真实批次可隐藏，只做本地移除，保持与人物库删除一致。
      if (!review) await removeStudioGenerationTask(task);
      updateData((current) => ({
        ...current,
        tasks: current.tasks.filter((item) => item.id !== task.id),
      }));
      setPendingRemove(null);
      notify("任务已从列表移除。");
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message.trim()
          : "移除失败，请重试。",
      );
    } finally {
      setRemovingId(null);
    }
  };
  const saveTaskName = async (task: StudioTask) => {
    if (review || renameSavingId) return;
    const name = renameDraft.trim();
    if (!name) {
      notify("视频名称不能为空。");
      return;
    }
    setRenameSavingId(task.id);
    try {
      const updated = await renameStudioGenerationTask(task, name);
      updateData((current) => ({
        ...current,
        tasks: current.tasks.map((item) =>
          item.id === task.id ? { ...item, title: updated.title } : item,
        ),
      }));
      setRenamingId(null);
      setRenameDraft("");
      notify("视频名称已更新。");
    } catch (cause) {
      notify(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "重命名失败，请重试。",
      );
    } finally {
      setRenameSavingId(null);
    }
  };
  return (
    <section className="studio-tasks-page">
      <h1>任务中心</h1>
      <div className="studio-page-actions">
        {!review && (
          <Button onClick={() => openLive("tasks")}>历史任务与下载</Button>
        )}
      </div>
      <div className="studio-filter-bar">
        <Tabs
          value={status}
          onChange={setStatus}
          items={[
            { id: "all", label: "全部" },
            { id: "active", label: `进行中 ${activeCount}` },
            { id: "attention", label: `待处理 ${attentionCount}` },
            { id: "completed", label: "已完成" },
          ]}
        />
        <FilterSelect
          label="类型"
          items={kindItems}
          value={kind}
          onChange={setKind}
        />
      </div>
      <p className="studio-result-count" role="status">
        共 {tasks.length} 条任务
      </p>
      <div className="studio-table-wrap">
        <table className="studio-table">
          <thead>
            <tr>
              <th>作品</th>
              <th>类型</th>
              <th>状态</th>
              <th>提交时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tasks.map((task) => (
              <tr key={task.id}>
                <td>
                  <div className="studio-task-name">
                    {task.poster ? (
                      <VideoPreview poster={task.poster} />
                    ) : (
                      <Icon name="video" size={30} />
                    )}
                    {renamingId === task.id ? (
                      <input
                        aria-label={`重命名 ${task.title}`}
                        maxLength={120}
                        value={renameDraft}
                        onChange={(event) => setRenameDraft(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") void saveTaskName(task);
                          if (event.key === "Escape") setRenamingId(null);
                        }}
                      />
                    ) : (
                      <span>{task.title}</span>
                    )}
                  </div>
                </td>
                <td>{task.type}</td>
                <td>
                  <StatusCell task={task} />
                </td>
                <td>{formatTaskTime(task.submitted)}</td>
                <td>
                  <div className="studio-task-actions">
                    {task.backendKind === "generation_batch" &&
                    renamingId === task.id ? (
                      <>
                        <Button
                          variant="quiet"
                          disabled={
                            renameSavingId === task.id || !renameDraft.trim()
                          }
                          onClick={() => void saveTaskName(task)}
                        >
                          {renameSavingId === task.id ? "保存中…" : "保存名称"}
                        </Button>
                        <Button
                          variant="quiet"
                          disabled={renameSavingId === task.id}
                          onClick={() => setRenamingId(null)}
                        >
                          取消
                        </Button>
                      </>
                    ) : (
                      task.backendKind === "generation_batch" && (
                        <Button
                          variant="quiet"
                          onClick={() => {
                            setRenamingId(task.id);
                            setRenameDraft(task.title);
                          }}
                        >
                          重命名
                        </Button>
                      )
                    )}
                    {task.status === "queued" &&
                    (task.backendKind !== "oral_task" ||
                      task.backendStatus === "QUEUED") ? (
                      <Button
                        variant="quiet"
                        disabled={cancellingId === task.id}
                        onClick={() => void cancelTask(task)}
                      >
                        {cancellingId === task.id ? "取消中…" : "取消任务"}
                      </Button>
                    ) : (
                      <Button
                        variant="quiet"
                        onClick={() =>
                          navigate(
                            "task-detail",
                            taskDetailPatch(task, "tasks"),
                          )
                        }
                      >
                        {task.status === "completed" ? "查看结果" : "查看详情"}
                      </Button>
                    )}
                    {/* 两类任务都有账号级隐藏接口：仅从列表移除，不取消任务。 */}
                    {(task.backendKind === "generation_batch" ||
                      task.backendKind === "oral_task") &&
                      renamingId !== task.id && (
                        <Button
                          variant="quiet"
                          disabled={removingId === task.id}
                          onClick={() => setPendingRemove(task)}
                        >
                          删除
                        </Button>
                      )}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!tasks.length && (
          <Empty
            title={data.loading ? "正在加载任务" : "当前筛选下暂无任务"}
            description="真实生成任务会在这里显示，不会因接口失败填入示例结果。"
            action={
              status !== "all" || kind !== "全部" ? (
                <Button
                  onClick={() => {
                    setStatus("all");
                    setKind("全部");
                  }}
                >
                  清除筛选
                </Button>
              ) : undefined
            }
          />
        )}
      </div>
      {!review && (generationPage || oralPage) ? (
        <fieldset className="studio-page-actions" aria-label="任务历史分页">
          <span>
            普通批次已加载{" "}
            {
              data.tasks.filter((task) => task.backendKind !== "oral_task")
                .length
            }
            {generationPage ? ` / ${generationPage.total}` : ""}
          </span>
          <Button
            disabled={loadingHistory !== null || !generationPage?.nextCursor}
            onClick={() => void loadGenerationHistory()}
          >
            {loadingHistory === "generation"
              ? "加载普通批次中…"
              : "加载更多普通批次"}
          </Button>
          <span>
            口播任务已加载 {oralPage?.loaded ?? 0}
            {oralPage ? ` / ${oralPage.total}` : ""}
          </span>
          <Button
            disabled={
              loadingHistory !== null ||
              !oralPage ||
              oralPage.loaded >= oralPage.total
            }
            onClick={() => void loadOralHistory()}
          >
            {loadingHistory === "oral" ? "加载口播任务中…" : "加载更多口播任务"}
          </Button>
        </fieldset>
      ) : null}
      {!review && (
        <Hint>
          普通批次与口播任务分别分页；状态待确认的任务请先核对，不要直接重复提交。
        </Hint>
      )}
      {pendingRemove ? (
        <StudioDialog
          title="从任务列表移除"
          onClose={() => setPendingRemove(null)}
        >
          <div className="studio-confirm-panel">
            <p>
              确定从任务列表移除「{pendingRemove.title}」吗？
              仅从本账号列表移除，不会取消正在进行的任务，也不影响已产生的费用与生成结果。
            </p>
            <div className="studio-confirm-actions">
              <Button variant="quiet" onClick={() => setPendingRemove(null)}>
                取消
              </Button>
              <Button
                variant="primary"
                disabled={removingId === pendingRemove.id}
                onClick={() => void removeTask(pendingRemove)}
              >
                {removingId === pendingRemove.id ? "移除中…" : "确认移除"}
              </Button>
            </div>
          </div>
        </StudioDialog>
      ) : null}
    </section>
  );
}

export function TaskDetailPage() {
  const {
    state,
    data,
    navigate,
    openLive,
    review,
    patchState,
    updateData,
    notify,
    refresh,
    user,
  } = useStudio();
  const [actionBusy, setActionBusy] = useState<
    "download" | "retry" | "archive"
  >();
  const [previewLoad, setPreviewLoad] = useState<{
    key?: string;
    asset?: StudioAsset;
    status: "idle" | "loading" | "empty" | "error" | "ready";
  }>({ status: "idle" });
  const [detailLoad, setDetailLoad] = useState<{
    key?: string;
    status: "idle" | "loading" | "error" | "ready";
    error?: string;
  }>({ status: "idle" });
  const [detailRetry, setDetailRetry] = useState(0);
  const previewRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const detailUserIdRef = useRef(user.id);
  const verifiedDetailRef = useRef<{ key: string; userId: string } | undefined>(
    undefined,
  );
  detailUserIdRef.current = user.id;
  const task = data.tasks.find((item) => item.id === state.selectedTaskId);
  const currentResultIdRef = useRef(task?.resultId);
  currentResultIdRef.current = task?.resultId;
  const currentPreviewUrlRef = useRef<string | undefined>(undefined);
  const detailKind = state.selectedTaskKind;
  const detailId = state.selectedTaskBackendId;
  const detailKey =
    detailKind && detailId ? `${detailKind}:${detailId}` : undefined;
  const detailContextKey = detailKey ? `${user.id}:${detailKey}` : undefined;
  const taskMatchesRoute =
    task !== undefined &&
    task.backendKind === detailKind &&
    (task.backendId ?? task.batchId ?? task.id) === detailId;
  const taskIsVerified =
    review ||
    !detailContextKey ||
    (taskMatchesRoute &&
      verifiedDetailRef.current?.key === detailContextKey &&
      verifiedDetailRef.current?.userId === user.id);
  const previewContextKey = `${user.id}:${detailKey ?? state.selectedTaskId ?? "none"}`;
  const previewContextRef = useRef(previewContextKey);
  previewContextRef.current = previewContextKey;
  useEffect(() => {
    previewContextRef.current = previewContextKey;
    setActionBusy(undefined);
    return () => {
      previewRequestRef.current += 1;
    };
  }, [previewContextKey]);
  useEffect(() => {
    void detailRetry;
    if (review || !detailKind || !detailId) return;
    if (
      taskMatchesRoute &&
      verifiedDetailRef.current?.key === detailContextKey &&
      verifiedDetailRef.current?.userId === user.id
    )
      return;
    const requestId = ++detailRequestRef.current;
    const requestedUserId = user.id;
    let active = true;
    setDetailLoad({ key: detailContextKey, status: "loading" });
    void loadStudioTaskDetail(detailKind, detailId)
      .then((loaded) => {
        if (
          !active ||
          requestId !== detailRequestRef.current ||
          detailUserIdRef.current !== requestedUserId
        )
          return;
        verifiedDetailRef.current = {
          key: detailContextKey as string,
          userId: requestedUserId,
        };
        updateData((current) => ({
          ...current,
          tasks: current.tasks.some((item) => item.id === loaded.id)
            ? current.tasks.map((item) =>
                item.id === loaded.id ? loaded : item,
              )
            : [...current.tasks, loaded],
        }));
        setDetailLoad({ key: detailContextKey, status: "ready" });
      })
      .catch((cause: unknown) => {
        if (
          !active ||
          requestId !== detailRequestRef.current ||
          detailUserIdRef.current !== requestedUserId
        )
          return;
        setDetailLoad({
          key: detailContextKey,
          status: "error",
          error: customerVisibleErrorMessage(
            cause,
            "任务详情暂不可用，请重试。",
          ),
        });
      });
    return () => {
      active = false;
      detailRequestRef.current += 1;
    };
  }, [
    detailId,
    detailContextKey,
    detailKind,
    detailRetry,
    review,
    taskMatchesRoute,
    updateData,
    user.id,
  ]);
  const detailIsLoading =
    Boolean(detailContextKey && !taskIsVerified) &&
    !(detailLoad.key === detailContextKey && detailLoad.status === "error");
  const detailHasError =
    detailLoad.key === detailContextKey && detailLoad.status === "error";
  // Task-list polling contains persisted IDs only. Keep a loaded direct preview
  // in its user/task context so refreshing the list cannot unmount playback.
  const cachedPreview =
    previewLoad.key === previewContextKey ? previewLoad.asset : undefined;
  const result =
    (cachedPreview && (!task?.resultId || cachedPreview.id === task?.resultId)
      ? cachedPreview
      : undefined) ?? data.assets.find((asset) => asset.id === task?.resultId);
  currentPreviewUrlRef.current = result?.url;
  const previewStatus =
    previewLoad.key === previewContextKey ? previewLoad.status : "idle";
  const previewResult = useCallback(async () => {
    if (!task || !taskIsVerified || review || task.status !== "completed")
      return;
    const requestedTask = task;
    const requestedContext = previewContextKey;
    const requestId = ++previewRequestRef.current;
    setPreviewLoad({ key: requestedContext, status: "loading" });
    try {
      const asset = await loadTaskPreview(requestedTask);
      if (
        requestId !== previewRequestRef.current ||
        previewContextRef.current !== requestedContext
      )
        return;
      if (!asset) {
        setPreviewLoad({ key: requestedContext, status: "empty" });
        return;
      }
      // A poll may have announced an archived/replaced asset while the old
      // signing request was in flight. Do not overwrite that physical ID.
      if (
        currentResultIdRef.current &&
        currentResultIdRef.current !== requestedTask.resultId &&
        currentResultIdRef.current !== asset.id
      ) {
        setPreviewLoad({ key: requestedContext, status: "idle" });
        return;
      }
      updateData((current) => ({
        ...current,
        assets: current.assets.some((item) => item.id === asset.id)
          ? current.assets.map((item) => (item.id === asset.id ? asset : item))
          : [...current.assets, asset],
        tasks: current.tasks.map((item) =>
          item.id === requestedTask.id ? { ...item, resultId: asset.id } : item,
        ),
      }));
      setPreviewLoad({ key: requestedContext, status: "ready", asset });
    } catch {
      if (
        requestId === previewRequestRef.current &&
        previewContextRef.current === requestedContext
      )
        setPreviewLoad({ key: requestedContext, status: "error" });
    }
  }, [task, taskIsVerified, review, previewContextKey, updateData]);
  useEffect(() => {
    if (
      !review &&
      taskIsVerified &&
      task?.status === "completed" &&
      !result?.url &&
      previewStatus === "idle"
    )
      void previewResult();
  }, [
    review,
    taskIsVerified,
    task?.status,
    result?.url,
    previewStatus,
    previewResult,
  ]);
  if (!task || !taskIsVerified || detailIsLoading || detailHasError)
    return (
      <Empty
        title={
          detailIsLoading
            ? "正在读取任务详情"
            : detailHasError
              ? "任务详情暂不可用"
              : "未选择任务"
        }
        description={
          detailLoad.key === detailContextKey ? detailLoad.error : undefined
        }
        action={
          detailHasError ? (
            <Button onClick={() => setDetailRetry((value) => value + 1)}>
              重试读取任务详情
            </Button>
          ) : (
            <Button onClick={() => navigate(state.returnTo ?? "tasks")}>
              返回任务中心
            </Button>
          )
        }
      />
    );
  const person = data.people.find((item) => item.id === task.ipId);
  const info = [
    ["任务类型", task.type],
    [
      "信息来源",
      task.driverMode === "audio"
        ? "完整口播音频"
        : task.scriptVersion
          ? `终稿 V${task.scriptVersion}`
          : task.backendKind === "oral_task"
            ? "文案口播"
            : task.projectId
              ? "项目分镜"
              : "独立视频创作",
    ],
    ["IP", person?.name || "—"],
    ...(task.driverMode !== "audio"
      ? [
          [
            "声音",
            person?.voices.find((voice) => voice.id === task.voiceId)?.name ||
              "—",
          ],
        ]
      : []),
    [
      "口播分身",
      person?.avatars.find((avatar) => avatar.id === task.avatarId)?.name ||
        "—",
    ],
    ["提交时间", formatTaskTime(task.submitted)],
    ["资产状态", result?.saved ? "已保存到素材库" : "以任务返回结果为准"],
  ];
  const recreate = (page: "copy" | "oral" | "replica") => {
    const hasOralInputs =
      task.backendKind === "oral_task" &&
      (task.scriptText?.trim() ||
        (page === "oral" && task.driverMode === "audio" && task.audioId));
    if (!review && !task.draftSnapshot && !hasOralInputs) {
      openLive("tasks");
      notify(
        "请在原任务记录中使用重新生成，以保留服务端确认的镜头和素材参数。",
      );
      return;
    }
    patchState({ draft: draftFromTask(task) });
    navigate(
      page === "oral" && task.driverMode === "audio" ? "oral-audio" : page,
      {
        returnTo: "task-detail",
      },
    );
    if (!review) notify("已创建新的创作草稿，请核对正文与素材后重新确认。");
  };
  const saveResult = async () => {
    if (!result || result.saved || actionBusy) return;
    const requestedContext = previewContextKey;
    setActionBusy("archive");
    try {
      const asset = await saveTaskPreview(result);
      if (previewContextRef.current !== requestedContext) return;
      updateData((current) => ({
        ...current,
        assets: [
          ...current.assets.filter(
            (item) => item.id !== result.id && item.id !== asset.id,
          ),
          asset,
        ],
        tasks: current.tasks.map((item) =>
          item.id === task.id ? { ...item, resultId: asset.id } : item,
        ),
      }));
      setPreviewLoad({ key: requestedContext, status: "ready", asset });
      notify("成片已保存到素材库，可复用或创建发布草稿。未重复扣费。");
    } catch (error) {
      if (previewContextRef.current === requestedContext) {
        notify(
          error instanceof Error ? error.message : "保存成片失败，请重试。",
        );
        // 超时不代表服务端停止归档；只重新读取结果，不重发保存或生成请求。
        await previewResult();
      }
    } finally {
      if (previewContextRef.current === requestedContext)
        setActionBusy(undefined);
    }
  };
  const downloadResult = async () => {
    if (review) {
      notify(
        "这是效果审核示例，未提供可下载成片；真实任务通过原有下载接口获取。",
      );
      return;
    }
    if (task.backendKind !== "oral_task") {
      openLive("tasks");
      return;
    }
    setActionBusy("download");
    try {
      await downloadStudioTaskResult(task);
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "口播成片下载失败，请重试。",
      );
    } finally {
      setActionBusy(undefined);
    }
  };
  const retryTask = async () => {
    setActionBusy("retry");
    try {
      await retryStudioTask(task);
      notify("已提交成片归档重试。");
      refresh();
    } catch (error) {
      notify(
        error instanceof Error && error.message.trim()
          ? error.message.trim()
          : "重试失败，请刷新后再试。",
      );
    } finally {
      setActionBusy(undefined);
    }
  };
  return (
    <section className="studio-task-detail">
      <h1>任务详情与结果</h1>
      <Button onClick={() => navigate(state.returnTo ?? "tasks")}>
        <Icon name="back" />
        返回任务中心
      </Button>
      <header>
        <h2>{task.title}</h2>
        <span className="studio-tag">{task.type}</span>
        <Status task={task} />
      </header>
      <p>
        {task.status === "completed"
          ? "作品已生成完成，可查看与管理生成结果"
          : "任务状态实时以服务端记录为准"}
      </p>
      <div className="studio-result-grid">
        <Media
          asset={
            (task.status === "completed" && result) ||
            (task.status === "completed" && task.poster
              ? {
                  id: task.id,
                  name: task.title,
                  kind: "video",
                  poster: task.poster,
                  group: "任务",
                  source: "任务中心",
                  saved: false,
                }
              : undefined)
          }
          alt={
            task.status === "completed"
              ? task.title
              : "任务处理中或待核对，尚无可预览成片"
          }
          className="studio-result-preview"
          onError={(failedUrl) => {
            if (
              !failedUrl ||
              previewContextRef.current !== previewContextKey ||
              currentPreviewUrlRef.current !== failedUrl
            )
              return;
            setPreviewLoad({
              key: previewContextKey,
              status: "error",
              asset: result,
            });
          }}
        />
        <Panel>
          <h2>任务信息</h2>
          <dl className="studio-details">
            {info.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </Panel>
        <Panel>
          <h2>动作</h2>
          <div className="studio-result-actions">
            {!review && result?.generationTaskId && !result.saved && (
              <Button
                onClick={() => void saveResult()}
                disabled={actionBusy === "archive"}
              >
                {actionBusy === "archive" ? "正在保存成片…" : "保存到素材库"}
              </Button>
            )}
            {!review &&
              task.status === "completed" &&
              (!result?.url || previewStatus === "error") && (
                <Button
                  variant="primary"
                  onClick={previewResult}
                  disabled={previewStatus === "loading"}
                >
                  <Icon name="play" />
                  {previewStatus === "loading"
                    ? "正在加载预览…"
                    : previewStatus === "error"
                      ? "重试预览"
                      : previewStatus === "empty"
                        ? "重新尝试"
                        : "预览成片"}
                </Button>
              )}
            <Button
              onClick={() => void downloadResult()}
              disabled={
                task.status !== "completed" || actionBusy === "download"
              }
            >
              <Icon name="download" />
              {actionBusy === "download" ? "正在下载…" : "下载成片"}
            </Button>
            <Button
              disabled={!result?.saved || task.status !== "completed"}
              onClick={() =>
                navigate("materials", {
                  selectedAssetId: result?.id,
                  returnTo: "task-detail",
                })
              }
            >
              <Icon name="folder" />
              查看素材
            </Button>
            <Button
              variant="primary"
              disabled={!result?.saved || task.status !== "completed"}
              onClick={() =>
                navigate("publishing", { selectedAssetId: result?.id })
              }
            >
              <Icon name="upload" />
              去发布管理
            </Button>
          </div>
          {previewStatus === "empty" && (
            <Hint>该批次暂时没有可预览的成功结果。</Hint>
          )}
          {previewStatus === "error" && <Hint>预览加载失败，请重试。</Hint>}
          {result && !result.saved && (
            <Hint>
              保存到素材库后可复用和创建发布草稿，保存不会重新生成或扣积分。
            </Hint>
          )}
          {!review && task.status === "completed" && (
            <Hint>
              预览仅展示首个可用结果；完整结果与下载请进入“历史任务与下载”。
            </Hint>
          )}
          <Hint>进入发布管理仅创建发布草稿，不会自动发布。</Hint>
          {!review && task.retryAction && (
            <Button
              disabled={actionBusy === "retry"}
              onClick={() => void retryTask()}
            >
              重试归档
            </Button>
          )}
          {task.status === "uncertain" && !task.retryAction && (
            <Button onClick={() => openLive("tasks")}>核对任务状态</Button>
          )}
        </Panel>
        <Panel>
          <h2>基于此任务再创作</h2>
          <div className="studio-recreate">
            <Button
              onClick={() => recreate("copy")}
              disabled={
                !review &&
                task.backendKind === "oral_task" &&
                !task.scriptText?.trim() &&
                !task.draftSnapshot?.script.text.trim()
              }
            >
              <Icon name="pen" />
              调整脚本
            </Button>
            <Button
              onClick={() =>
                navigate("materials", {
                  returnTo: "task-detail",
                  selectedTaskId: task.id,
                })
              }
            >
              <Icon name="image" />
              更换素材
            </Button>
            <Button
              onClick={() =>
                recreate(task.type === "数字人口播" ? "oral" : "replica")
              }
            >
              <Icon name="refresh" />
              新建草稿
            </Button>
          </div>
        </Panel>
      </div>
    </section>
  );
}

export type StudioAccountSummary = {
  walletStatus: "loading" | "ready" | "error" | "unknown";
  availableCredits: number | null;
  retryWallet: () => void;
  retryProfile?: () => void;
  profile: CustomerProfile | null;
  profileLoadError: string;
};

export function ProfilePage({
  accountSummary,
}: {
  accountSummary?: StudioAccountSummary;
}) {
  const { user, review, openLive, notify } = useStudio();
  const [name, setName] = useState(user.display_name || user.username);
  const [profileTab, setProfileTab] = useState("overview");
  // C10b 通知偏好：进页拉取，乐观保存、失败回退；审核模式只演示不落库。
  const [notificationsEnabled, setNotificationsEnabled] = useState<
    boolean | null
  >(null);
  const [savingNotifications, setSavingNotifications] = useState(false);

  useEffect(() => {
    setName(user.display_name || user.username);
  }, [user.display_name, user.username]);

  useEffect(() => {
    if (review) {
      setNotificationsEnabled(true);
      return;
    }
    let cancelled = false;
    getStudioNotificationPreferences()
      .then((prefs) => {
        if (!cancelled) setNotificationsEnabled(prefs.enabled);
      })
      .catch(() => {
        if (!cancelled) setNotificationsEnabled(null);
      });
    return () => {
      cancelled = true;
    };
  }, [review]);

  async function toggleNotifications() {
    if (review) {
      notify("审核模式下为演示开关，不保存设置。");
      return;
    }
    if (notificationsEnabled === null || savingNotifications) return;
    const previous = notificationsEnabled;
    const next = !previous;
    setNotificationsEnabled(next);
    setSavingNotifications(true);
    try {
      await updateStudioNotificationPreferences(next);
      notify(next ? "已开启通知。" : "已关闭通知。");
    } catch (cause) {
      setNotificationsEnabled(previous);
      notify(cause instanceof Error ? cause.message : "保存通知偏好失败");
    } finally {
      setSavingNotifications(false);
    }
  }
  return (
    <section className="studio-profile">
      <h1>用户档案</h1>
      <div className="studio-profile-identity">
        {review ? (
          <img src="/studio/li.png" alt="登录账户" />
        ) : (
          <span className="studio-user-initial">
            {user.display_name?.slice(0, 1) || "我"}
          </span>
        )}
        <div>
          <h2>
            {user.display_name || user.username}
            <span className="studio-tag">登录账户</span>
          </h2>
          <p>{review ? "众墅之家" : "账户资料"}</p>
          <small>与创作用的人物 IP 分开管理</small>
        </div>
      </div>
      <Tabs
        value={profileTab}
        onChange={(id) =>
          id === "billing"
            ? openLive("wallet")
            : id === "devices"
              ? openLive("profile")
              : setProfileTab(id)
        }
        items={[
          { id: "overview", label: "账户资料" },
          { id: "publishing", label: "发布账号" },
          { id: "billing", label: "使用记录" },
          { id: "devices", label: "设备管理" },
        ]}
      />
      {profileTab === "publishing" ? (
        <PublishAccountsPanel notify={notify} />
      ) : (
        <>
          <div className="studio-profile-grid">
            <Panel>
              <h2>账户资料</h2>
              <Field label="用户名">
                <input
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  readOnly={!review}
                />
              </Field>
              <dl className="studio-details">
                <div>
                  <dt>账号</dt>
                  <dd>{accountSummary?.profile?.username ?? user.username}</dd>
                </div>
                <div>
                  <dt>加入时间</dt>
                  <dd>
                    {accountSummary?.profile?.joined_at
                      ? new Date(
                          accountSummary.profile.joined_at,
                        ).toLocaleDateString("zh-CN")
                      : review
                        ? "审核示例"
                        : "未查询"}
                  </dd>
                </div>
                <div>
                  <dt>
                    通知偏好<small>接收任务完成及异常状态通知</small>
                  </dt>
                  <dd>
                    <button
                      type="button"
                      className="studio-switch"
                      aria-label="通知偏好"
                      aria-pressed={notificationsEnabled === true}
                      disabled={
                        notificationsEnabled === null || savingNotifications
                      }
                      onClick={() => void toggleNotifications()}
                    >
                      {notificationsEnabled === false
                        ? "关闭"
                        : notificationsEnabled === null
                          ? "—"
                          : "开启"}
                    </button>
                  </dd>
                </div>
              </dl>
            </Panel>
            <Panel>
              <h2>发布账号概览</h2>
              <p>扫码账号与登录状态保存在本机。</p>
              <Button onClick={() => setProfileTab("publishing")}>
                <Icon name="person" />
                管理发布账号
              </Button>
            </Panel>
            <Panel>
              <h2>账户使用概览</h2>
              <dl className="studio-details">
                <div>
                  <dt>账户余额</dt>
                  <dd>
                    {accountSummary?.walletStatus === "ready" &&
                    accountSummary.availableCredits !== null
                      ? `${accountSummary.availableCredits} 积分`
                      : accountSummary?.walletStatus === "loading"
                        ? "查询中"
                        : accountSummary?.walletStatus === "error"
                          ? "读取失败"
                          : "未查询"}
                  </dd>
                </div>
                {accountSummary?.walletStatus === "error" && (
                  <div>
                    <dt>余额查询</dt>
                    <dd>
                      <Button onClick={accountSummary.retryWallet}>
                        重试余额查询
                      </Button>
                    </dd>
                  </div>
                )}
                {accountSummary?.profileLoadError && (
                  <div>
                    <dt>资料状态</dt>
                    <dd>
                      {accountSummary.profileLoadError}
                      {accountSummary.retryProfile && (
                        <Button onClick={accountSummary.retryProfile}>
                          重试资料查询
                        </Button>
                      )}
                    </dd>
                  </div>
                )}
                <div>
                  <dt>
                    使用记录<small>查看详细的使用记录</small>
                  </dt>
                  <dd>
                    <Button onClick={() => openLive("wallet")}>
                      查看使用记录
                    </Button>
                  </dd>
                </div>
              </dl>
            </Panel>
            <Panel>
              <h2>设备摘要</h2>
              <div className="studio-device-summary">
                <span>
                  {review
                    ? "多台设备可同时在线"
                    : "进入设备管理查看当前绑定记录"}
                </span>
                <Button onClick={() => openLive("profile")}>管理设备</Button>
              </div>
            </Panel>
          </div>
          <div className="studio-profile-save">
            <Button
              variant="primary"
              onClick={() =>
                review
                  ? notify("示例账户，不保存真实资料。")
                  : openLive("profile")
              }
            >
              {review ? "保存资料" : "编辑账户资料"}
            </Button>
          </div>
        </>
      )}
    </section>
  );
}

export { PublishAccountsPanel };
