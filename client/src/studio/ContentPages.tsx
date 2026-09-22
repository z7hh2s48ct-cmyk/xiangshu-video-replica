import type { RefObject } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  MaterialBulkResult,
  MaterialBulkUpdate,
  MaterialGroupItem,
  MaterialItem,
  MaterialPage,
  ViralImportPurpose,
  ViralImportTask,
  ViralPlatform,
  ViralVideoItem,
} from "../api";
import {
  bulkUpdateMaterials,
  clearMaterialCache,
  createGenerationTaskPreviewUrl,
  createMaterialUploadIntent,
  createPublishRecord,
  createViralImportTask,
  downloadMaterialAsset,
  evictMaterialCachedPreview,
  fetchViralVideo,
  fetchViralVideoMedia,
  fetchViralVideoStatistics,
  getAssetDownloadUrl,
  getMaterialBatchPreviews,
  getMaterialCachedPreview,
  getMaterialCacheUsage,
  getStudioDraft,
  getViralImportTask,
  hideMaterial,
  listMaterialGroups,
  listMaterials,
  listViralFavorites,
  listViralVideos,
  putMaterial,
  refreshViralVideoStatistics,
  removeViralFavorite,
  resolveMaterials,
  saveStudioDraft,
  saveViralFavorite,
  updateMaterial,
} from "../api";
import { VideoPreview } from "../VideoPreview";
import { CharacterMaterialViews } from "./CharacterMaterialViews";
import { useStudio } from "./context";
import { studioAssetFromMaterial, studioVideoFromViral } from "./live";
import {
  type CloudPublishAccount,
  canUseLocalPublishAccounts,
  type LocalPublishAccount,
  listCloudPublishAccounts,
  listLocalPublishAccounts,
  openLocalPublishAccount,
} from "./localPublishAccounts";
import { AccountAvatar, PlatformLogo } from "./PlatformLogo";
import { PublishRecordsPanel } from "./PublishRecordsPanel";
import type {
  StudioAsset,
  StudioContextValue,
  StudioPublishDraft,
  StudioVideo,
} from "./types";
import { Button, Empty, Field, Hint, Icon, Media, Panel, Tabs } from "./ui";
import {
  clearViralImportIdempotencyKey,
  shouldClearViralImportIdempotencyKey,
  ViralImportPollingTimeoutError,
  viralImportIdempotencyKey,
} from "./viralImport";
import "./content.css";

const pageSize = 24;
const categoryTabs = ["全部", "建房预算", "户型设计", "施工避坑", "庭院案例"];

/** MATERIAL-PERF-A（P0-1）：分页按钮窗口化——始终含首末页与当前页 ±1，
 * 其余以「…」折叠，页数多时不再渲染整排页码按钮。 */
export function visiblePageButtons(
  page: number,
  pages: number,
): (number | "…")[] {
  if (pages <= 7) {
    return Array.from({ length: pages }, (_, index) => index + 1);
  }
  const wanted = new Set<number>(
    [1, pages, page - 1, page, page + 1].filter(
      (value) => value >= 1 && value <= pages,
    ),
  );
  const ordered = [...wanted].sort((a, b) => a - b);
  const entries: (number | "…")[] = [];
  let previous = 0;
  for (const value of ordered) {
    if (previous && value - previous > 1) entries.push("…");
    entries.push(value);
    previous = value;
  }
  return entries;
}

function formatCount(value: number | null) {
  if (value === null) return "—";
  return value >= 10000
    ? `${(value / 10000).toFixed(1).replace(".0", "")}万`
    : value.toLocaleString("zh-CN");
}

function assetKindLabel(kind: StudioAsset["kind"]) {
  return kind === "image" ? "图片" : kind === "video" ? "视频" : "音频";
}

const viralInitialCount = 12;
const viralPageSize = 12;

function viralIdentity(video: StudioVideo) {
  return video.platformKey && video.nativeId
    ? `${video.platformKey}:${video.nativeId}`
    : video.id;
}

function apiErrorCode(error: unknown) {
  if (!error || typeof error !== "object" || !("code" in error)) return;
  return typeof error.code === "string" ? error.code : undefined;
}

function viralDetailParams():
  | { platform: ViralPlatform; videoId: string }
  | undefined {
  const params = new URLSearchParams(window.location.search);
  const platform = params.get("viralPlatform");
  const videoId = params.get("viralVideoId")?.trim();
  if (
    (platform === "douyin" ||
      platform === "wechat_channels" ||
      platform === "xiaohongshu") &&
    videoId
  ) {
    return { platform, videoId };
  }
  return undefined;
}

function persistViralDetailUrl(video: StudioVideo) {
  if (!video.platformKey || !video.nativeId) return;
  const url = new URL(window.location.href);
  url.searchParams.set("viralPlatform", video.platformKey);
  url.searchParams.set("viralVideoId", video.nativeId);
  window.history.replaceState(null, "", url);
}

type MaterialPreviewState = {
  status: "loading" | "ready" | "error";
  url?: string;
  cached?: boolean;
};
type MaterialPreviewStates = Record<string, MaterialPreviewState>;

export function failMaterialPreview(
  current: MaterialPreviewStates,
  assetId: string,
  failedUrl?: string,
): MaterialPreviewStates {
  const preview = current[assetId];
  if (preview?.status !== "ready" || preview.url !== failedUrl) return current;
  return { ...current, [assetId]: { status: "error" } };
}

function viralLikesLabel(video: StudioVideo) {
  return video.likeDisplay ?? formatCount(video.likes);
}

function viralPublishLabel(video: StudioVideo) {
  if (video.publishedDisplay) return video.publishedDisplay;
  if (video.publishedAt) {
    return new Date(video.publishedAt * 1000).toLocaleDateString("zh-CN", {
      month: "numeric",
      day: "numeric",
    });
  }
  return "";
}

function ViralPoster({
  video,
  className,
}: {
  video: StudioVideo;
  className?: string;
}) {
  return (
    <VideoPreview
      poster={video.poster}
      className={className}
      fallback={<Icon name="video" />}
    />
  );
}

type ViralPlayback =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "playing"; src: string }
  | { status: "error"; message: string };

function updateViralStats(
  updateData: StudioContextValue["updateData"],
  item?: ViralVideoItem | null,
) {
  updateViralStatistics(updateData, item ? [item] : []);
}

function updateViralStatistics(
  updateData: StudioContextValue["updateData"],
  items: ViralVideoItem[],
) {
  if (!items.length) return;
  const byId = new Map(items.map((item) => [item.videoId, item]));
  updateData((data) => ({
    ...data,
    videos: data.videos.map((video) => {
      const item = video.nativeId ? byId.get(video.nativeId) : undefined;
      return item && video.platformKey === item.platform
        ? {
            ...video,
            likes: item.likes,
            comments: item.comments,
            shares: item.shares,
            collections: item.collects,
            likeDisplay: item.likeDisplay,
          }
        : video;
    }),
  }));
}

function useViralStatistics(videos: StudioVideo[], enabled: boolean) {
  const { updateData } = useStudio();
  const attemptedIds = useRef(new Set<string>());
  const [error, setError] = useState<string>();
  const pendingIds = videos
    .filter(
      (video) =>
        video.platformKey === "wechat_channels" &&
        video.nativeId &&
        !attemptedIds.current.has(video.nativeId),
    )
    .map((video) => video.nativeId as string)
    .slice(0, 12);
  const pendingKey = pendingIds.join(",");

  useEffect(() => {
    if (!enabled || !pendingKey) return;
    const ids = pendingKey.split(",");
    ids.forEach((id) => {
      attemptedIds.current.add(id);
    });
    setError(undefined);
    void fetchViralVideoStatistics(ids)
      .then((result) => updateViralStatistics(updateData, result.items))
      .catch(() => setError("部分视频统计暂时无法更新"));
  }, [enabled, pendingKey, updateData]);

  return error;
}

function RefreshStatisticsButton({ videos }: { videos: StudioVideo[] }) {
  const { updateData } = useStudio();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const key = useRef<{ fingerprint: string; value: string } | null>(null);
  const saving = useRef(false);
  const ids = videos
    .filter(
      (video) => video.platformKey === "wechat_channels" && video.nativeId,
    )
    .map((video) => video.nativeId as string)
    .slice(0, 12);
  if (!ids.length) return null;
  async function refreshStatistics() {
    if (saving.current) return;
    const fingerprint = JSON.stringify([...ids].sort());
    if (key.current?.fingerprint !== fingerprint)
      key.current = { fingerprint, value: crypto.randomUUID() };
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      const result = await refreshViralVideoStatistics(ids, key.current.value);
      updateViralStatistics(updateData, result.items);
      key.current = null;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "数据刷新失败");
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  return (
    <div>
      <button
        type="button"
        disabled={busy}
        onClick={() => void refreshStatistics()}
      >
        {busy ? "正在刷新…" : "读取最新已采集数据"}
      </button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}

/** 点击播放：真实平台视频先走媒体管线，测试夹具可直接使用 playUrl。 */
function useViralPlayback(
  video?: StudioVideo,
  active = true,
  onActivate?: () => void,
) {
  const { updateData } = useStudio();
  const [playback, setPlayback] = useState<ViralPlayback>({ status: "idle" });
  const activeRef = useRef(active);
  const requestIdRef = useRef(0);
  const loadingRef = useRef(false);
  const activate = () => {
    activeRef.current = true;
    onActivate?.();
  };

  useEffect(() => {
    activeRef.current = active;
    if (!active) {
      requestIdRef.current += 1;
      loadingRef.current = false;
      setPlayback((current) =>
        current.status === "loading" ? { status: "idle" } : current,
      );
    }
  }, [active]);

  const playFromStorage = async () => {
    if (!video?.platformKey || !video.nativeId || loadingRef.current) return;
    activate();
    const requestId = ++requestIdRef.current;
    loadingRef.current = true;
    setPlayback({ status: "loading" });
    try {
      const media = await fetchViralVideoMedia(
        video.platformKey,
        video.nativeId,
        "video",
      );
      updateViralStats(updateData, media.video);
      if (requestId === requestIdRef.current && activeRef.current) {
        setPlayback({ status: "playing", src: media.url });
      }
    } catch {
      if (requestId === requestIdRef.current && activeRef.current) {
        setPlayback({
          status: "error",
          message: "视频暂时无法播放，请稍后重试",
        });
      }
    } finally {
      if (requestId === requestIdRef.current) loadingRef.current = false;
    }
  };
  const play = async () => {
    if (!video || playback.status !== "idle") return;
    if (video.platformKey && video.nativeId) {
      await playFromStorage();
      return;
    }
    if (video.playUrl) {
      activate();
      setPlayback({ status: "playing", src: video.playUrl });
      return;
    }
    setPlayback({ status: "error", message: "视频暂时无法播放，请稍后重试" });
  };
  const markFailed = () => {
    setPlayback({ status: "error", message: "视频播放失败，请重试" });
  };
  return {
    playback,
    play,
    retry: playFromStorage,
    markFailed,
    activate,
  };
}

/** 封面区（参考 CardCover）：点击原位播放，缓冲态整窗进度条，失败可重试。 */
function ViralCover({
  video,
  playing,
  loading,
  src,
  onPlay,
  onRetry,
  onPlaybackError,
  onNativePlay,
  playerRef,
  error,
}: {
  video: StudioVideo;
  playing: boolean;
  loading: boolean;
  src?: string;
  onPlay: () => void;
  onRetry: () => void;
  onPlaybackError: () => void;
  onNativePlay: () => void;
  playerRef: RefObject<HTMLVideoElement | null>;
  error?: string;
}) {
  const publish = viralPublishLabel(video);
  return (
    <div
      className={`viral-card-cover ${playing ? "is-playing" : ""}`}
      title={playing ? undefined : "播放"}
    >
      {playing ? (
        <VideoPreview
          ref={playerRef}
          autoPlay
          controls
          playsInline
          src={src}
          poster={video.poster}
          title={video.title}
          onError={onPlaybackError}
          onPlay={onNativePlay}
        />
      ) : (
        <button
          type="button"
          className="viral-card-cover-trigger"
          onClick={error ? onRetry : onPlay}
          aria-label={`${error ? "重试播放" : "播放"} ${video.title}`}
          disabled={loading}
        >
          <ViralPoster video={video} />
          <span className="viral-card-scrim" aria-hidden="true" />
          <span className="viral-card-playbtn" aria-hidden="true">
            ▶
          </span>
          {loading && <span className="viral-card-loading">准备中…</span>}
          {error && (
            <span className="viral-card-loading" role="status">
              {error}
            </span>
          )}
        </button>
      )}
      <span className="viral-card-platform">
        <PlatformLogo platform={video.platform} size={18} /> {video.platform}
      </span>
      <span className="viral-card-duration">{video.duration}</span>
      {!playing && (
        <div className="viral-card-overlay">
          <ViralStatsRow video={video} />
          <div className="viral-card-overlay-author">
            {video.authorAvatar ? (
              <img alt="" loading="lazy" src={video.authorAvatar} />
            ) : (
              <i>{(video.author || "无").slice(0, 1)}</i>
            )}
            <span>{video.author}</span>
            {video.verified && <em title="认证作者">✓</em>}
            {publish && <time>{publish}</time>}
            {video.category && (
              <em className="viral-card-cat">{video.category}</em>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** 统计行（参考统计行四字段常显）：点赞 / 评论 / 转发 / 收藏。 */
function ViralStatsRow({ video }: { video: StudioVideo }) {
  const stats: Array<[string, string, string]> = [
    ["heart", "点赞", viralLikesLabel(video)],
    ["comment", "评论", formatCount(video.comments ?? null)],
    ["share", "转发", formatCount(video.shares)],
    ["star", "收藏", formatCount(video.collections)],
  ];
  return (
    <div className="viral-card-stats">
      {stats.map(([icon, label, value]) => (
        <span key={icon} title={label}>
          <Icon name={icon} size={13} />
          {value}
        </span>
      ))}
    </div>
  );
}

export function ViralFavoriteButton({
  video,
  savedFromServer,
  onSavedChange,
  compact = false,
}: {
  video: StudioVideo;
  savedFromServer?: boolean;
  onSavedChange?: (saved: boolean) => void;
  compact?: boolean;
}) {
  const { state, review, notify, patchState, user } = useStudio();
  const [saved, setSaved] = useState(
    savedFromServer ?? state.favorites.includes(video.id),
  );
  const [savingOperation, setSavingOperation] = useState<{
    accountId: string;
    operationId: number;
  }>();
  const accountRef = useRef(user.id);
  const operationRef = useRef(0);
  if (accountRef.current !== user.id) {
    accountRef.current = user.id;
    operationRef.current += 1;
  }
  const saving = Boolean(
    savingOperation &&
      savingOperation.accountId === user.id &&
      savingOperation.operationId === operationRef.current,
  );
  useEffect(() => {
    if (savedFromServer !== undefined) setSaved(savedFromServer);
  }, [savedFromServer]);
  useEffect(() => {
    return () => {
      operationRef.current += 1;
    };
  }, []);

  const toggle = async () => {
    if (saving) return;
    const operation = {
      accountId: user.id,
      operationId: ++operationRef.current,
    };
    const isCurrent = () =>
      accountRef.current === operation.accountId &&
      operationRef.current === operation.operationId;
    const nextSaved = !saved;
    if (!isCurrent()) return;
    setSaved(nextSaved);
    onSavedChange?.(nextSaved);
    patchState({
      favorites: nextSaved
        ? [...new Set([...state.favorites, video.id])]
        : state.favorites.filter((id) => id !== video.id),
    });
    if (review || !video.platformKey || !video.nativeId) return;
    setSavingOperation(operation);
    try {
      if (nextSaved) {
        await saveViralFavorite(video.platformKey, video.nativeId);
      } else {
        await removeViralFavorite(video.platformKey, video.nativeId);
      }
    } catch {
      if (!isCurrent()) return;
      setSaved(saved);
      onSavedChange?.(saved);
      patchState({
        favorites: saved
          ? [...new Set([...state.favorites, video.id])]
          : state.favorites.filter((id) => id !== video.id),
      });
      notify("收藏失败，已恢复原状态");
    } finally {
      if (isCurrent()) setSavingOperation(undefined);
    }
  };

  return (
    <Button
      variant="outline"
      className={compact ? "viral-card-favorite" : undefined}
      aria-label={`收藏 ${video.title}`}
      aria-pressed={saved}
      title={saved ? "取消收藏" : "收藏视频"}
      disabled={saving}
      onClick={() => void toggle()}
    >
      {compact ? <Icon name="star" size={18} /> : saved ? "已收藏" : "收藏"}
    </Button>
  );
}

function ViralCard({
  video,
  active,
  onActivate,
  savedFromServer,
  onFavoriteChange,
  availability = "available",
}: {
  video: StudioVideo;
  active: boolean;
  onActivate: () => void;
  savedFromServer?: boolean;
  onFavoriteChange: (saved: boolean) => void;
  availability?: ViralVideoItem["availability"];
}) {
  const {
    review,
    navigate,
    notify,
    patchDraft,
    extractScriptFromUpload,
    user,
  } = useStudio();
  const playerRef = useRef<HTMLVideoElement | null>(null);
  const { importState, start } = useViralImport(video.id, user.id);
  const { playback, play, retry, markFailed, activate } = useViralPlayback(
    video,
    active,
    onActivate,
  );
  useEffect(() => {
    if (!active) playerRef.current?.pause();
  }, [active]);
  const openDetail = () => {
    navigate("viral-detail", {
      selectedVideoId: video.id,
      returnTo: "viral",
    });
    persistViralDetailUrl(video);
  };
  const beginExtract = () => {
    if (availability !== "available") {
      notify("该视频已不可用，无法提取文案");
      return;
    }
    if (review) {
      patchDraft({ sourceId: video.id });
      navigate("copy", {
        selectedVideoId: video.id,
        returnTo: "viral",
      });
      return;
    }
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少可导入的平台标识");
      return;
    }
    void start(video, "copy", (task) => {
      if (!task.canTranscribe || !task.projectId || !task.sourceAssetId) {
        notify("该来源暂不支持提取文案");
        return;
      }
      patchDraft({
        projectId: task.projectId,
        sourceId: task.sourceAssetId,
        sourceAssetId: task.sourceAssetId,
      });
      extractScriptFromUpload(task.projectId, task.sourceAssetId);
      navigate("copy", {
        selectedVideoId: video.id,
        returnTo: "viral",
      });
    });
  };
  return (
    <article className="viral-card">
      <ViralCover
        video={video}
        playing={playback.status === "playing"}
        loading={playback.status === "loading"}
        src={playback.status === "playing" ? playback.src : undefined}
        onPlay={play}
        onRetry={retry}
        onPlaybackError={markFailed}
        onNativePlay={activate}
        playerRef={playerRef}
        error={playback.status === "error" ? playback.message : undefined}
      />
      <ViralFavoriteButton
        compact
        video={video}
        savedFromServer={savedFromServer}
        onSavedChange={onFavoriteChange}
      />
      <div className="viral-card-body">
        <h3>{video.title}</h3>
        <div className="viral-card-tags">
          {video.tags?.slice(0, 6).map((tag) => (
            <span key={tag} title={`#${tag}`}>
              #{tag}
            </span>
          ))}
        </div>
        <div className="content-card-actions">
          <Button variant="quiet" onClick={openDetail}>
            查看详情
          </Button>
          <Button
            variant="outline"
            aria-label={`提取文案 ${video.title}`}
            disabled={importState.status === "loading"}
            onClick={beginExtract}
          >
            提取文案
          </Button>
        </div>
        {importState.status !== "idle" && (
          <p
            className={`viral-media-status is-${importState.status}`}
            role="status"
          >
            {importState.message}
          </p>
        )}
        {availability !== "available" && (
          <p className="viral-media-status is-error" role="status">
            该视频已不可用
          </p>
        )}
      </div>
    </article>
  );
}

export function ViralPage() {
  const { data, review, updateData, user } = useStudio();
  const accountId = user.id;
  const [platform, setPlatform] = useState<"抖音" | "视频号">("抖音");
  const [category, setCategory] = useState("全部");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"热门优先" | "最新">("热门优先");
  const [scope, setScope] = useState<"all" | "favorites">("all");
  const [visibleCount, setVisibleCount] = useState(viralInitialCount);
  const [activeVideoId, setActiveVideoId] = useState<string>();
  const [listError, setListError] = useState<string>();
  const [refreshStatus, setRefreshStatus] = useState<string>();
  const [listLoading, setListLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string>();
  const [hasMore, setHasMore] = useState(false);
  const [listReloadRevision, setListReloadRevision] = useState(0);
  const [serverCategories, setServerCategories] = useState<string[]>([]);
  const [favoriteKeys, setFavoriteKeys] = useState(new Set<string>());
  const [availabilityByKey, setAvailabilityByKey] = useState(
    new Map<string, ViralVideoItem["availability"]>(),
  );
  const sentinelRef = useRef<HTMLButtonElement | null>(null);
  const listRequestRef = useRef(0);
  const loadingMoreRef = useRef(false);
  const listReloadRevisionRef = useRef(listReloadRevision);
  const queryRef = useRef(query);
  const queryLifecycleRef = useRef(query);
  listReloadRevisionRef.current = listReloadRevision;
  queryRef.current = query;
  const platformKey: ViralPlatform =
    platform === "抖音" ? "douyin" : "wechat_channels";
  const sortKey = sort === "最新" ? "latest" : "hot";
  const paginationContextKey = `${accountId}:${scope}:${platformKey}:${sortKey}:${category}`;
  const paginationContextKeyRef = useRef(paginationContextKey);
  const paginationOperationRef = useRef(0);
  const activePaginationContextRef = useRef({
    key: `${paginationContextKey}:${listReloadRevision}`,
    operationId: 0,
  });
  const renderedPaginationContextKey = `${paginationContextKey}:${listReloadRevision}`;
  if (activePaginationContextRef.current.key !== renderedPaginationContextKey) {
    activePaginationContextRef.current = {
      key: renderedPaginationContextKey,
      operationId: ++paginationOperationRef.current,
    };
  }
  const renderedPaginationContext = activePaginationContextRef.current;
  const confirmedPaginationContextRef = useRef<
    typeof renderedPaginationContext | undefined
  >(undefined);
  const paginationSnapshotsRef = useRef(
    new Map<string, { nextCursor?: string; hasMore: boolean }>(),
  );
  paginationContextKeyRef.current = paginationContextKey;

  const shown = useMemo(
    () =>
      data.videos
        .filter(
          (item) =>
            item.platform === platform &&
            (scope === "all" || favoriteKeys.has(viralIdentity(item))) &&
            (category === "全部" || item.category === category) &&
            `${item.title}${item.author}`.includes(query),
        )
        .sort((left, right) => {
          if (sort === "热门优先") {
            return (
              right.likes - left.likes ||
              left.id.localeCompare(right.id, undefined, { numeric: true })
            );
          }
          return (right.publishedAt ?? 0) - (left.publishedAt ?? 0);
        }),
    [category, data.videos, favoriteKeys, platform, query, scope, sort],
  );

  // biome-ignore lint/correctness/useExhaustiveDependencies: 平台/分类/搜索词/排序变化时重置滚动加载计数。
  useEffect(() => {
    setVisibleCount(viralInitialCount);
  }, [platform, category, query, scope, sort]);

  const current = review ? shown.slice(0, visibleCount) : shown;
  const statisticsError = useViralStatistics(current, !review);

  useEffect(() => {
    void accountId;
    setFavoriteKeys(new Set());
  }, [accountId]);

  useEffect(() => {
    if (queryLifecycleRef.current === query) return;
    queryLifecycleRef.current = query;
    listRequestRef.current += 1;
    loadingMoreRef.current = false;
    setListLoading(false);
    setLoadingMore(false);
    setListError(undefined);
    const snapshot = paginationSnapshotsRef.current.get(paginationContextKey);
    if (!query && snapshot) {
      setNextCursor(snapshot.nextCursor);
      setHasMore(snapshot.hasMore);
    } else {
      setNextCursor(undefined);
      setHasMore(false);
    }
  }, [paginationContextKey, query]);

  useEffect(() => {
    if (review || scope !== "all") return;
    const requestContext = renderedPaginationContext;
    const requestId = ++listRequestRef.current;
    const requestReloadRevision = listReloadRevision;
    loadingMoreRef.current = false;
    setLoadingMore(false);
    let cancelled = false;
    setListError(undefined);
    setRefreshStatus(undefined);
    setListLoading(true);
    setNextCursor(undefined);
    setHasMore(false);
    void listViralVideos(platformKey, sortKey, { limit: viralPageSize })
      .then((result) => {
        if (
          cancelled ||
          requestId !== listRequestRef.current ||
          requestReloadRevision !== listReloadRevisionRef.current ||
          requestContext !== activePaginationContextRef.current ||
          paginationContextKey !== paginationContextKeyRef.current
        )
          return;
        const incoming = result.items.map(studioVideoFromViral);
        setServerCategories(result.categories);
        const confirmedNextCursor = result.nextCursor ?? undefined;
        const confirmedHasMore = Boolean(result.hasMore && result.nextCursor);
        confirmedPaginationContextRef.current = requestContext;
        paginationSnapshotsRef.current.set(paginationContextKey, {
          nextCursor: confirmedNextCursor,
          hasMore: confirmedHasMore,
        });
        setNextCursor(confirmedNextCursor);
        setHasMore(confirmedHasMore);
        setRefreshStatus(
          result.refreshing
            ? "正在采集爆款视频，当前先展示已缓存内容…"
            : result.refreshError
              ? result.refreshError
              : result.stale
                ? "当前展示缓存内容，等待下次刷新。"
                : undefined,
        );
        setFavoriteKeys(
          new Set(
            result.items
              .filter((item) => item.isFavorite)
              .map((item) => `${item.platform}:${item.videoId}`),
          ),
        );
        setAvailabilityByKey(
          new Map(
            result.items.map((item) => [
              `${item.platform}:${item.videoId}`,
              item.availability,
            ]),
          ),
        );
        updateData((currentData) => ({
          ...currentData,
          videos: [
            ...currentData.videos.filter(
              (video) => video.platformKey !== platformKey,
            ),
            ...incoming,
          ],
        }));
      })
      .catch(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListError("视频列表暂时无法更新，已保留当前内容");
        }
      })
      .finally(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    listReloadRevision,
    paginationContextKey,
    platformKey,
    review,
    renderedPaginationContext,
    scope,
    sortKey,
    updateData,
  ]);

  useEffect(() => {
    if (review || scope !== "favorites") return;
    const requestContext = renderedPaginationContext;
    const requestId = ++listRequestRef.current;
    const requestReloadRevision = listReloadRevision;
    loadingMoreRef.current = false;
    setLoadingMore(false);
    let cancelled = false;
    setListError(undefined);
    setListLoading(true);
    setNextCursor(undefined);
    setHasMore(false);
    void listViralFavorites({ platform: platformKey, limit: viralPageSize })
      .then((result) => {
        if (
          cancelled ||
          requestId !== listRequestRef.current ||
          requestReloadRevision !== listReloadRevisionRef.current ||
          requestContext !== activePaginationContextRef.current ||
          paginationContextKey !== paginationContextKeyRef.current
        )
          return;
        const incoming = result.items.map(studioVideoFromViral);
        setFavoriteKeys(
          new Set(
            result.items.map((item) => `${item.platform}:${item.videoId}`),
          ),
        );
        setAvailabilityByKey(
          new Map(
            result.items.map((item) => [
              `${item.platform}:${item.videoId}`,
              item.availability,
            ]),
          ),
        );
        setServerCategories([
          ...new Set(result.items.map((item) => item.category).filter(Boolean)),
        ]);
        const confirmedNextCursor = result.nextCursor ?? undefined;
        const confirmedHasMore = Boolean(result.hasMore && result.nextCursor);
        confirmedPaginationContextRef.current = requestContext;
        paginationSnapshotsRef.current.set(paginationContextKey, {
          nextCursor: confirmedNextCursor,
          hasMore: confirmedHasMore,
        });
        setNextCursor(confirmedNextCursor);
        setHasMore(confirmedHasMore);
        updateData((currentData) => ({
          ...currentData,
          videos: [
            ...currentData.videos.filter(
              (video) => video.platformKey !== platformKey,
            ),
            ...incoming,
          ],
        }));
      })
      .catch(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListError("收藏列表暂时无法更新");
        }
      })
      .finally(() => {
        if (
          !cancelled &&
          requestId === listRequestRef.current &&
          requestReloadRevision === listReloadRevisionRef.current &&
          requestContext === activePaginationContextRef.current &&
          paginationContextKey === paginationContextKeyRef.current
        ) {
          setListLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [
    listReloadRevision,
    paginationContextKey,
    platformKey,
    review,
    renderedPaginationContext,
    scope,
    updateData,
  ]);

  const loadMore = useCallback(async () => {
    if (review) {
      setVisibleCount((count) => Math.min(count + viralPageSize, shown.length));
      return;
    }
    if (
      !hasMore ||
      !nextCursor ||
      loadingMoreRef.current ||
      confirmedPaginationContextRef.current !== renderedPaginationContext
    )
      return;
    const requestId = listRequestRef.current;
    const requestQuery = query;
    const requestContextKey = paginationContextKey;
    const requestContext = renderedPaginationContext;
    const isCurrent = () =>
      requestId === listRequestRef.current &&
      requestQuery === queryRef.current &&
      requestContext === activePaginationContextRef.current &&
      requestContext === confirmedPaginationContextRef.current &&
      requestContextKey === paginationContextKeyRef.current;
    loadingMoreRef.current = true;
    setLoadingMore(true);
    setListError(undefined);
    try {
      const result =
        scope === "favorites"
          ? await listViralFavorites({
              platform: platformKey,
              limit: viralPageSize,
              cursor: nextCursor,
            })
          : await listViralVideos(platformKey, sortKey, {
              limit: viralPageSize,
              cursor: nextCursor,
            });
      if (!isCurrent()) return;
      const incoming = result.items.map(studioVideoFromViral);
      const confirmedNextCursor = result.nextCursor ?? undefined;
      const confirmedHasMore = Boolean(result.hasMore && result.nextCursor);
      paginationSnapshotsRef.current.set(paginationContextKey, {
        nextCursor: confirmedNextCursor,
        hasMore: confirmedHasMore,
      });
      setNextCursor(confirmedNextCursor);
      setHasMore(confirmedHasMore);
      setFavoriteKeys((currentKeys) => {
        const nextKeys = new Set(currentKeys);
        result.items.forEach((item) => {
          if (scope === "favorites" || item.isFavorite) {
            nextKeys.add(`${item.platform}:${item.videoId}`);
          }
        });
        return nextKeys;
      });
      setAvailabilityByKey((currentAvailability) => {
        const nextAvailability = new Map(currentAvailability);
        result.items.forEach((item) => {
          nextAvailability.set(
            `${item.platform}:${item.videoId}`,
            item.availability,
          );
        });
        return nextAvailability;
      });
      updateData((currentData) => {
        const incomingIds = new Set(incoming.map((video) => video.id));
        return {
          ...currentData,
          videos: [
            ...currentData.videos.filter(
              (video) =>
                video.platformKey !== platformKey || !incomingIds.has(video.id),
            ),
            ...incoming,
          ],
        };
      });
    } catch (error) {
      if (isCurrent()) {
        if (apiErrorCode(error) === "VIRAL_CURSOR_INVALID") {
          paginationSnapshotsRef.current.delete(paginationContextKey);
          setNextCursor(undefined);
          setHasMore(false);
          setListError(undefined);
          setListReloadRevision((revision) => revision + 1);
          return;
        }
        setListError("加载更多失败，请重试");
      }
    } finally {
      if (isCurrent()) {
        loadingMoreRef.current = false;
        setLoadingMore(false);
      }
    }
  }, [
    hasMore,
    nextCursor,
    paginationContextKey,
    platformKey,
    query,
    renderedPaginationContext,
    review,
    scope,
    shown.length,
    sortKey,
    updateData,
  ]);

  const canLoadMore = review ? visibleCount < shown.length : !query && hasMore;
  useEffect(() => {
    const node = sentinelRef.current;
    if (!node || !canLoadMore) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadMore();
      },
      { rootMargin: "240px 0px" },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [canLoadMore, loadMore]);

  const platformTabs = (["抖音", "视频号"] as const).map((item) => ({
    id: item,
    label: `${item} ${
      review
        ? item === "抖音"
          ? 20
          : 30
        : data.videos.filter((video) => video.platform === item).length
    }`,
  }));
  const categories = review
    ? categoryTabs
    : ["全部", ...serverCategories.filter((item) => item !== "全部")];

  return (
    <section className="content-page content-viral">
      <header className="content-title">
        <div>
          <h1>爆款视频</h1>
          <p>乡墅灵感，持续发现 · 最近 7 天爆款</p>
        </div>
        <div className="content-search">
          <Icon name="search" />
          <input
            aria-label="搜索视频标题"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
            }}
            placeholder="在已加载的爆款中搜索"
          />
        </div>
      </header>
      <div className="content-viral-toolbar">
        <div
          className="content-platform-tabs"
          role="tablist"
          aria-label="内容范围"
        >
          <button
            aria-selected={scope === "all"}
            className={scope === "all" ? "is-active" : ""}
            onClick={() => {
              setCategory("全部");
              setScope("all");
            }}
            role="tab"
            type="button"
          >
            全部爆款
          </button>
          <button
            aria-selected={scope === "favorites"}
            className={scope === "favorites" ? "is-active" : ""}
            onClick={() => {
              setCategory("全部");
              setScope("favorites");
            }}
            role="tab"
            type="button"
          >
            我的收藏
          </button>
        </div>
        <div
          className="content-platform-tabs"
          role="tablist"
          aria-label="视频平台"
        >
          {platformTabs.map((item) => (
            <button
              aria-selected={item.id === platform}
              className={item.id === platform ? "is-active" : ""}
              key={item.id}
              onClick={() => {
                setCategory("全部");
                setPlatform(item.id);
              }}
              role="tab"
              type="button"
            >
              <PlatformLogo platform={item.id} /> {item.label}
            </button>
          ))}
        </div>
        <label className="content-sort">
          <span>排序</span>
          <select
            aria-label="排序方式"
            value={sort}
            onChange={(event) =>
              setSort(event.target.value as "热门优先" | "最新")
            }
          >
            <option value="热门优先">热门优先</option>
            <option value="最新">最新</option>
          </select>
        </label>
      </div>
      <nav className="content-filters" aria-label="视频分类">
        {categories.map((item) => (
          <Button
            key={item}
            variant={item === category ? "primary" : "outline"}
            onClick={() => setCategory(item)}
          >
            {item}
          </Button>
        ))}
      </nav>
      {!review && <RefreshStatisticsButton videos={current} />}
      {(listError || statisticsError) && (
        <p className="viral-media-status is-error" role="status">
          {listError ?? statisticsError}
        </p>
      )}
      {refreshStatus && (
        <p className="viral-media-status" role="status">
          {refreshStatus}
        </p>
      )}
      {listLoading && (
        <p className="viral-media-status" role="status">
          正在读取爆款视频…
        </p>
      )}
      {current.length ? (
        <section className="content-video-grid content-video-grid-viral">
          {current.map((video) => (
            <ViralCard
              key={video.id}
              video={video}
              active={activeVideoId === video.id}
              onActivate={() => setActiveVideoId(video.id)}
              savedFromServer={
                review ? undefined : favoriteKeys.has(viralIdentity(video))
              }
              onFavoriteChange={(saved) =>
                setFavoriteKeys((currentKeys) => {
                  const nextKeys = new Set(currentKeys);
                  const key = viralIdentity(video);
                  if (saved) nextKeys.add(key);
                  else nextKeys.delete(key);
                  return nextKeys;
                })
              }
              availability={availabilityByKey.get(viralIdentity(video))}
            />
          ))}
        </section>
      ) : (
        <Empty
          title={scope === "favorites" ? "暂无收藏视频" : "暂无爆款视频"}
          description={
            scope === "favorites"
              ? "收藏爆款视频后，可以在这里统一查看。"
              : refreshStatus
                ? refreshStatus
                : "数据源尚未配置或最近 7 天暂无内容，配置后自动展示。"
          }
        />
      )}
      {canLoadMore && (
        <button
          type="button"
          className="viral-reveal-sentinel"
          ref={sentinelRef}
          disabled={loadingMore}
          onClick={() => void loadMore()}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              void loadMore();
            }
          }}
        >
          {loadingMore
            ? "正在加载…"
            : review
              ? "上拉加载更多…"
              : "加载更多视频"}
        </button>
      )}
    </section>
  );
}

type ViralImportState = {
  status: "idle" | "loading" | "ready" | "error";
  message?: string;
};

function useViralImport(sourceId?: string, accountId = "anonymous") {
  const [state, setState] = useState<ViralImportState>({ status: "idle" });
  const requestRef = useRef(0);
  const sourceRef = useRef(sourceId);
  const accountRef = useRef(accountId);
  const idempotencyKeysRef = useRef(new Map<string, string>());
  useEffect(() => {
    sourceRef.current = sourceId;
    accountRef.current = accountId;
    setState({ status: "idle" });
    return () => {
      requestRef.current += 1;
    };
  }, [sourceId, accountId]);

  const waitForCompletion = async (
    initial: ViralImportTask,
    request: number,
  ) => {
    let task = initial;
    for (let attempt = 0; attempt < 120; attempt += 1) {
      if (request !== requestRef.current) return undefined;
      if (task.status === "SUCCEEDED" || task.status === "FAILED") return task;
      const taskId = task.taskId ?? task.id;
      if (!taskId) throw new Error("导入任务缺少任务 ID");
      await new Promise<void>((resolve) => window.setTimeout(resolve, 1_000));
      if (request !== requestRef.current) return undefined;
      task = await getViralImportTask(taskId);
    }
    if (request !== requestRef.current) return undefined;
    if (task.status === "SUCCEEDED" || task.status === "FAILED") return task;
    throw new ViralImportPollingTimeoutError("导入任务等待超时，请稍后重试");
  };

  return {
    importState: state,
    start: async (
      video: StudioVideo,
      purpose: ViralImportPurpose,
      onReady: (task: ViralImportTask) => void,
    ) => {
      if (video.id !== sourceRef.current || accountId !== accountRef.current)
        return;
      if (!video.platformKey || !video.nativeId) {
        setState({ status: "error", message: "该视频缺少可导入的平台标识" });
        return;
      }
      const request = ++requestRef.current;
      const actionKey = `${video.platformKey}:${video.nativeId}:${purpose}`;
      const memoryKey = `${accountId}:${actionKey}`;
      const idempotencyKey =
        idempotencyKeysRef.current.get(memoryKey) ??
        viralImportIdempotencyKey(accountId, actionKey);
      idempotencyKeysRef.current.set(memoryKey, idempotencyKey);
      const clearKey = () => {
        idempotencyKeysRef.current.delete(memoryKey);
        clearViralImportIdempotencyKey(accountId, actionKey, idempotencyKey);
      };
      setState({ status: "loading", message: "正在导入参考素材…" });
      try {
        const created = await createViralImportTask(
          video.platformKey,
          video.nativeId,
          purpose,
          idempotencyKey,
        );
        const completed = await waitForCompletion(created, request);
        if (!completed || request !== requestRef.current) return;
        if (completed.status === "FAILED") {
          if (completed.retryable === false) clearKey();
          throw new Error(
            completed.errorMessage ||
              completed.error ||
              completed.message ||
              "导入任务执行失败",
          );
        }
        if (!completed.projectId || !completed.sourceAssetId) {
          clearKey();
          throw new Error("导入任务缺少项目或素材结果");
        }
        if (purpose === "copy" && !completed.canTranscribe) {
          clearKey();
          throw new Error("该来源暂不支持提取文案");
        }
        if (purpose === "replica" && !completed.canAnalyze) {
          clearKey();
          throw new Error("该来源暂不支持视频复刻");
        }
        setState({ status: "ready", message: "参考素材已导入" });
        onReady(completed);
      } catch (error) {
        if (request !== requestRef.current) return;
        if (shouldClearViralImportIdempotencyKey(error)) clearKey();
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "导入任务失败",
        });
      }
    },
  };
}

export function ViralDetailPage() {
  const {
    data,
    state,
    user,
    review,
    navigate,
    notify,
    patchDraft,
    updateData,
    extractScriptFromUpload,
  } = useStudio();
  const accountId = user.id;
  const selectedVideo = state.selectedVideoId
    ? data.videos.find((item) => item.id === state.selectedVideoId)
    : undefined;
  const [remoteVideo, setRemoteVideo] = useState<StudioVideo>();
  const [detailStatus, setDetailStatus] = useState<
    "idle" | "loading" | "error"
  >("idle");
  const [detailAvailability, setDetailAvailability] =
    useState<ViralVideoItem["availability"]>("available");
  const [detailFavorite, setDetailFavorite] = useState<boolean>();
  const detailAccountRef = useRef(accountId);
  const detailRequestRef = useRef(0);
  detailAccountRef.current = accountId;
  const detailParams = useMemo(viralDetailParams, []);
  const video = remoteVideo ?? selectedVideo;
  const { importState, start } = useViralImport(video?.id, accountId);

  useEffect(() => {
    void accountId;
    setRemoteVideo(undefined);
    setDetailFavorite(undefined);
    setDetailAvailability("available");
    setDetailStatus("idle");
  }, [accountId]);

  useEffect(() => {
    void accountId;
    if (remoteVideo || review || !detailParams) return;
    if (
      selectedVideo &&
      (selectedVideo.platformKey !== detailParams.platform ||
        selectedVideo.nativeId !== detailParams.videoId)
    ) {
      return;
    }
    const requestAccountId = accountId;
    const requestId = ++detailRequestRef.current;
    const isCurrent = () =>
      detailAccountRef.current === requestAccountId &&
      detailRequestRef.current === requestId;
    setDetailStatus("loading");
    void fetchViralVideo(detailParams.platform, detailParams.videoId)
      .then((response) => {
        if (!isCurrent()) return;
        const item = "item" in response ? response.item : response;
        const restored = studioVideoFromViral(item);
        if (!isCurrent()) return;
        setDetailAvailability(item.availability ?? "available");
        setDetailFavorite(Boolean(item.isFavorite));
        setRemoteVideo(restored);
        setDetailStatus("idle");
        if (!isCurrent()) return;
        updateData((current) => ({
          ...current,
          videos: [
            ...current.videos.filter(
              (candidate) => candidate.id !== restored.id,
            ),
            restored,
          ],
        }));
      })
      .catch(() => {
        if (isCurrent()) setDetailStatus("error");
      });
    return () => {
      if (detailRequestRef.current === requestId) {
        detailRequestRef.current += 1;
      }
    };
  }, [detailParams, remoteVideo, review, selectedVideo, updateData, accountId]);

  useEffect(() => {
    if (video) persistViralDetailUrl(video);
  }, [video]);

  const statisticsError = useViralStatistics(video ? [video] : [], !review);
  const { playback, play, retry, markFailed } = useViralPlayback(video);
  if (!video && detailStatus === "loading") {
    return (
      <section className="content-page">
        <p className="viral-media-status" role="status">
          正在读取视频详情…
        </p>
      </section>
    );
  }
  if (!video)
    return (
      <section className="content-page">
        <Empty
          title={detailStatus === "error" ? "视频暂不可用" : "暂未选择参考视频"}
          description={
            detailStatus === "error"
              ? "该视频可能已下架或暂时无法读取。"
              : "请返回爆款视频列表选择一条内容。"
          }
          action={
            <Button variant="primary" onClick={() => navigate("viral")}>
              返回爆款视频
            </Button>
          }
        />
      </section>
    );
  const published =
    video.publishedDisplay ??
    (video.publishedAt
      ? new Date(video.publishedAt * 1000).toLocaleDateString("zh-CN")
      : "—");
  const metrics: Array<[string, string, string]> = [
    ["heart", "点赞", viralLikesLabel(video)],
    ["comment", "评论", formatCount(video.comments ?? null)],
    ["share", "转发", formatCount(video.shares)],
    ["star", "收藏", formatCount(video.collections)],
  ];
  const goExtract = (task: ViralImportTask) => {
    if (!task.canTranscribe || !task.projectId || !task.sourceAssetId) {
      notify("该来源暂不支持提取文案");
      return;
    }
    patchDraft({
      projectId: task.projectId,
      sourceId: task.sourceAssetId,
      sourceAssetId: task.sourceAssetId,
    });
    extractScriptFromUpload(task.projectId, task.sourceAssetId);
    navigate("copy", {
      selectedVideoId: video.id,
      returnTo: "viral-detail",
    });
  };
  return (
    <section className="content-page content-detail">
      <header className="content-detail-heading">
        <h1>爆款视频 / 视频详情</h1>
        <Button
          variant="quiet"
          onClick={() => navigate(state.returnTo ?? "viral")}
        >
          ‹ 返回列表
        </Button>
      </header>
      <section className="content-detail-grid content-detail-grid-viral">
        <div className="content-player content-player-viral">
          {playback.status === "playing" ? (
            <VideoPreview
              autoPlay
              controls
              playsInline
              src={playback.src}
              poster={video.poster}
              title={video.title}
              onError={markFailed}
            />
          ) : (
            <button
              type="button"
              className="content-player-viral-trigger"
              onClick={playback.status === "error" ? retry : play}
              aria-label={`${playback.status === "error" ? "重试播放" : "播放"} ${video.title}`}
              disabled={playback.status === "loading"}
            >
              <ViralPoster
                video={video}
                className="content-player-viral-poster"
              />
              {playback.status === "loading" ? (
                <span className="viral-card-loading">素材准备中…</span>
              ) : playback.status === "error" ? (
                <span className="viral-card-loading" role="status">
                  {playback.message}
                </span>
              ) : (
                <span className="content-player-viral-play">▶ 播放</span>
              )}
            </button>
          )}
          <span className="viral-card-duration">{video.duration}</span>
        </div>
        <Panel className="content-detail-info">
          <div className="content-detail-author">
            {video.authorAvatar ? (
              <img alt="" src={video.authorAvatar} />
            ) : (
              <i>{(video.author || "无").slice(0, 1)}</i>
            )}
            <div>
              <strong>
                {video.author}
                {video.verified && <em title="认证作者">✓</em>}
              </strong>
              <span>
                <PlatformLogo platform={video.platform} /> {video.platform} ·{" "}
                {video.category || "推荐"}
                {video.hasPlayableAudio ? " · 有原声" : ""}
              </span>
            </div>
          </div>
          <h2>{video.title}</h2>
          {video.tags && video.tags.length > 0 && (
            <div className="viral-card-tags viral-detail-tags">
              {video.tags.slice(0, 6).map((tag) => (
                <span key={tag}>#{tag}</span>
              ))}
            </div>
          )}
          <div className="viral-detail-metrics">
            {metrics.map(([icon, label, value]) => (
              <span key={label} title={label}>
                <Icon name={icon} size={14} />
                {value}
              </span>
            ))}
            <span className="viral-detail-published">发布 {published}</span>
            <span>时长 {video.duration}</span>
          </div>
          {!review && video && <RefreshStatisticsButton videos={[video]} />}
          {statisticsError && (
            <p className="viral-media-status is-error" role="status">
              {statisticsError}
            </p>
          )}
          {detailAvailability !== "available" && (
            <p className="viral-media-status is-error" role="status">
              该视频已不可用，暂不能导入创作。
            </p>
          )}
          <Field label="视频摘要（来源描述，非提取文案）">
            <p className="content-source-copy">{video.description}</p>
          </Field>
          {importState.status !== "idle" && (
            <p
              className={`viral-media-status is-${importState.status}`}
              role="status"
            >
              {importState.message}
            </p>
          )}
          <div className="content-detail-actions">
            <div className="content-detail-action">
              <Button
                disabled={
                  importState.status === "loading" ||
                  detailAvailability !== "available"
                }
                variant="outline"
                onClick={() => void start(video, "copy", goExtract)}
              >
                提取文案
              </Button>
            </div>
            <div className="content-detail-action">
              <ViralFavoriteButton
                video={video}
                savedFromServer={review ? undefined : detailFavorite}
                onSavedChange={setDetailFavorite}
              />
            </div>
          </div>
        </Panel>
      </section>
    </section>
  );
}

function AssetCard({
  asset,
  selected,
  organize = false,
  checked = false,
  previewStatus,
  onSelect,
  onPreviewError,
  onPlay,
}: {
  asset: StudioAsset;
  selected: boolean;
  // MATERIAL-UX-01：整理模式下卡片进入可勾选态，点击切换选中而非打开详情。
  organize?: boolean;
  checked?: boolean;
  previewStatus?: "loading" | "ready" | "error";
  onSelect: () => void;
  onPreviewError: (failedUrl?: string) => void;
  onPlay: () => void;
}) {
  const kindLabel = asset.composite ? "五视图" : assetKindLabel(asset.kind);
  const status =
    previewStatus === "loading"
      ? "预览加载中…"
      : previewStatus === "error"
        ? "预览加载失败，点击重试"
        : asset.delivery === "direct"
          ? "生成完成"
          : asset.saved
            ? "永久保存"
            : "处理中";
  const statusTone =
    previewStatus === "error"
      ? "is-error"
      : previewStatus === "loading" || status === "处理中"
        ? "is-pending"
        : "is-ready";
  return (
    <button
      type="button"
      className={`content-asset content-asset--${asset.kind} ${selected ? "is-selected" : ""} ${asset.composite ? "content-asset--composite" : ""} ${organize ? "is-organizing" : ""} ${organize && checked ? "is-checked" : ""}`}
      onClick={onSelect}
      aria-label={`选择素材 ${asset.name}`}
      aria-pressed={organize ? checked : undefined}
    >
      {organize ? (
        <span aria-hidden="true" className="content-asset__check">
          {checked ? "✓" : ""}
        </span>
      ) : null}
      <Media
        asset={asset}
        alt={asset.name}
        fitContainer
        onError={onPreviewError}
        onPlay={onPlay}
      />
      <div className="content-asset__title-row">
        <strong title={asset.name}>{asset.name}</strong>
        <span className="content-asset__kind">{kindLabel}</span>
      </div>
      <span className="content-asset__meta">
        {asset.group} · {asset.composite ? "1 套五视图" : kindLabel}
      </span>
      <span className={`content-asset__status ${statusTone}`}>{status}</span>
    </button>
  );
}

export function MaterialsPage() {
  const { user } = useStudio();
  return <MaterialsPageContent key={user.id} />;
}

function MaterialsPageContent() {
  const {
    user,
    data,
    state,
    review,
    patchState,
    patchDraft,
    updateData,
    navigate,
    notify,
  } = useStudio();
  const [kind, setKind] = useState<"全部" | StudioAsset["kind"]>("全部");
  const [source, setSource] = useState<"" | MaterialItem["source"]>("");
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [remotePage, setRemotePage] = useState<MaterialPage | null>(null);
  const [remoteError, setRemoteError] = useState<string>();
  const [remoteLoading, setRemoteLoading] = useState(false);
  const [selectedAsset, setSelectedAsset] = useState<StudioAsset>();
  const [characterView, setCharacterView] = useState<StudioAsset>();
  const [uploadProgress, setUploadProgress] = useState<number>();
  const [busyAction, setBusyAction] = useState<string>();
  const [renameValue, setRenameValue] = useState("");
  const [groupValue, setGroupValue] = useState("");
  const [groupNewName, setGroupNewName] = useState("");
  // MATERIAL-UX-01：分组导航与整理模式的页面态。group 为 undefined 时不过滤；
  // 非空时与素材的有效分组精确匹配。
  const [group, setGroup] = useState<string | undefined>(undefined);
  const [groups, setGroups] = useState<MaterialGroupItem[]>([]);
  const [groupsRevision, setGroupsRevision] = useState(0);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const [organize, setOrganize] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkGroupChoice, setBulkGroupChoice] = useState("");
  const [bulkGroupDraft, setBulkGroupDraft] = useState("");
  const [uploadGroup, setUploadGroup] = useState("我的上传");
  const [uploadGroupDraft, setUploadGroupDraft] = useState("");
  const [previewStates, setPreviewStates] = useState<MaterialPreviewStates>({});
  // MATERIAL-THUMBS-B：视频瓦片封面（键 = 授权 id；7 天签名，img 直接展示）。
  const [thumbnailUrls, setThumbnailUrls] = useState<Record<string, string>>(
    {},
  );
  const visiblePreviewIdsRef = useRef(new Set<string>());
  const previewRequestVersionsRef = useRef(new Map<string, number>());
  const previewLoadingIdsRef = useRef(new Set<string>());
  // MATERIAL-PERF-A（P0-4）：失败/过期预览的有限自动重试记账。
  const previewRetryCountsRef = useRef(new Map<string, number>());
  // 缩略图单独记账：本体授权成功会清零上面那个计数，而缩略图 404 时本体往往
  // 是好的（抽帧失败而已），共用一个计数等于上限永不生效。
  const thumbnailRetryCountsRef = useRef(new Map<string, number>());
  const previewRetryTimersRef = useRef(
    new Set<ReturnType<typeof setTimeout>>(),
  );
  const previewResourcesRef = useRef(
    new Map<string, { url: string; release: () => void }>(),
  );
  const cacheControllersRef = useRef(new Set<AbortController>());
  const warmingIdsRef = useRef(new Set<string>());
  const evictionRef = useRef(new Map<string, Promise<void>>());
  const aliveRef = useRef(true);
  const cacheRevisionRef = useRef(0);
  const cacheSuppressedRef = useRef(false);
  const [cacheUsage, setCacheUsage] = useState<{
    bytes: number;
    limitBytes: number;
    available: boolean;
  }>();
  const [cacheMessage, setCacheMessage] = useState("");
  const [clearingCache, setClearingCache] = useState(false);
  const uploadInputRef = useRef<HTMLInputElement | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const reviewAssets = data.assets.filter(
    (asset) => kind === "全部" || asset.kind === kind,
  );
  const remoteAssets = useMemo(
    () => remotePage?.items.map(studioAssetFromMaterial) ?? [],
    [remotePage],
  );
  const assets = review ? reviewAssets : remoteAssets;
  const visibleSelected = assets.find(
    (asset) => asset.id === state.selectedAssetId,
  );
  const selected =
    selectedAsset &&
    (state.selectedAssetId === undefined ||
      selectedAsset.id === state.selectedAssetId)
      ? selectedAsset
      : (visibleSelected ??
        data.assets.find((asset) => asset.id === state.selectedAssetId));
  const selectedIndex = reviewAssets.findIndex(
    (asset) => asset.id === state.selectedAssetId,
  );
  const selectedInput =
    selected?.composite && characterView?.contactSheetId === selected.id
      ? characterView
      : selected;
  const [page, setPage] = useState(() =>
    review && selectedIndex >= 0 ? Math.floor(selectedIndex / pageSize) + 1 : 1,
  );
  const total = review ? reviewAssets.length : (remotePage?.total ?? 0);
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const selectedAssetIdRef = useRef(state.selectedAssetId);
  const refreshCacheUsage = useCallback(async () => {
    const revision = cacheRevisionRef.current;
    try {
      const usage = await getMaterialCacheUsage(user.id);
      if (aliveRef.current && revision === cacheRevisionRef.current)
        setCacheUsage(usage);
    } catch {
      /* Cache status must not prevent online preview. */
    }
  }, [user.id]);
  useEffect(() => {
    aliveRef.current = true;
    if (!review) void refreshCacheUsage();
    return () => {
      aliveRef.current = false;
      cacheRevisionRef.current += 1;
      for (const controller of cacheControllersRef.current) controller.abort();
      for (const resource of previewResourcesRef.current.values())
        resource.release();
      previewResourcesRef.current.clear();
      for (const timer of previewRetryTimersRef.current) clearTimeout(timer);
      previewRetryTimersRef.current.clear();
      previewRetryCountsRef.current.clear();
      thumbnailRetryCountsRef.current.clear();
    };
  }, [refreshCacheUsage, review]);
  // Latest-ref 桥：loadPreview 的失败分支通过它调度自动重试（P0-4），
  // 避免与 retryPreview 相互依赖造成的 useCallback 环。
  const retryPreviewRef = useRef<(asset: StudioAsset) => void>(() => {});
  const loadPreview = useCallback(
    async (asset: StudioAsset) => {
      if (
        asset.url ||
        !asset.allowedActions?.includes("preview") ||
        previewLoadingIdsRef.current.has(asset.id)
      )
        return;
      previewLoadingIdsRef.current.add(asset.id);
      const requestVersion =
        (previewRequestVersionsRef.current.get(asset.id) ?? 0) + 1;
      previewRequestVersionsRef.current.set(asset.id, requestVersion);
      const revision = cacheRevisionRef.current;
      const controller = new AbortController();
      cacheControllersRef.current.add(controller);
      setPreviewStates((current) => ({
        ...current,
        [asset.id]: { status: "loading" },
      }));
      try {
        await evictionRef.current.get(asset.id);
        const result = asset.assetId
          ? await getMaterialCachedPreview(
              user.id,
              asset.previewAssetId ?? asset.assetId,
              {
                populate: asset.kind === "image" && !cacheSuppressedRef.current,
                signal: controller.signal,
              },
            )
          : undefined;
        const url =
          result?.url ??
          (asset.generationTaskId
            ? await createGenerationTaskPreviewUrl(asset.generationTaskId)
            : undefined);
        if (
          !aliveRef.current ||
          revision !== cacheRevisionRef.current ||
          previewRequestVersionsRef.current.get(asset.id) !== requestVersion ||
          !visiblePreviewIdsRef.current.has(asset.id)
        ) {
          result?.release();
          return;
        }
        previewResourcesRef.current.get(asset.id)?.release();
        if (result) previewResourcesRef.current.set(asset.id, result);
        setPreviewStates((current) => ({
          ...current,
          [asset.id]: url
            ? {
                status: "ready",
                url,
                ...(result?.cached ? { cached: true } : {}),
              }
            : { status: "error" },
        }));
        if (url) previewRetryCountsRef.current.delete(asset.id);
        else retryPreviewRef.current(asset);
        if (result?.cached) void refreshCacheUsage();
      } catch {
        if (
          !aliveRef.current ||
          revision !== cacheRevisionRef.current ||
          previewRequestVersionsRef.current.get(asset.id) !== requestVersion ||
          !visiblePreviewIdsRef.current.has(asset.id)
        )
          return;
        setPreviewStates((current) => ({
          ...current,
          [asset.id]: { status: "error" },
        }));
        retryPreviewRef.current(asset);
      } finally {
        cacheControllersRef.current.delete(controller);
        if (previewRequestVersionsRef.current.get(asset.id) === requestVersion)
          previewLoadingIdsRef.current.delete(asset.id);
      }
    },
    [refreshCacheUsage, user.id],
  );

  // MATERIAL-PERF-A（P0-2）：整页可见素材一次批量授权，替代逐瓦片
  // download-url + 元数据往返。首屏只读取已有缓存或返回在线地址，不在批量
  // 请求内下载 24 份原图；已有本机缓存仍优先使用。仅 generationTaskId
  // 的素材保持单资产回退通道。批量整体失败（超时/网络）时退化为逐条路径。
  const loadPreviewsBatch = useCallback(
    async (assets: StudioAsset[]) => {
      const pending = assets.filter(
        (asset) =>
          !asset.url &&
          asset.allowedActions?.includes("preview") &&
          (asset.previewAssetId ?? asset.assetId),
      );
      if (!pending.length) return;
      const revision = cacheRevisionRef.current;
      const controller = new AbortController();
      cacheControllersRef.current.add(controller);
      let batchFailed = false;
      const versions = new Map<string, number>();
      for (const asset of pending) {
        previewLoadingIdsRef.current.add(asset.id);
        const version =
          (previewRequestVersionsRef.current.get(asset.id) ?? 0) + 1;
        previewRequestVersionsRef.current.set(asset.id, version);
        versions.set(asset.id, version);
        setPreviewStates((current) =>
          current[asset.id]
            ? current
            : { ...current, [asset.id]: { status: "loading" } },
        );
      }
      try {
        const { previews, thumbnails } = await getMaterialBatchPreviews(
          user.id,
          pending.map((asset) => ({
            id: asset.previewAssetId ?? asset.assetId ?? "",
            populate: false,
          })),
          { signal: controller.signal },
        );
        if (!aliveRef.current || revision !== cacheRevisionRef.current) {
          for (const resource of Object.values(previews)) resource.release();
          return;
        }
        setThumbnailUrls((current) => {
          const next = { ...current };
          for (const asset of pending) {
            const authId = asset.previewAssetId ?? asset.assetId ?? "";
            if (
              visiblePreviewIdsRef.current.has(asset.id) &&
              thumbnails[authId]
            )
              next[authId] = thumbnails[authId];
            else delete next[authId];
          }
          return next;
        });
        setPreviewStates((current) => {
          const next = { ...current };
          for (const asset of pending) {
            if (
              previewRequestVersionsRef.current.get(asset.id) !==
              versions.get(asset.id)
            )
              continue;
            if (!visiblePreviewIdsRef.current.has(asset.id)) continue;
            const resource =
              previews[asset.previewAssetId ?? asset.assetId ?? ""];
            previewResourcesRef.current.get(asset.id)?.release();
            if (resource) {
              previewResourcesRef.current.set(asset.id, resource);
              previewRetryCountsRef.current.delete(asset.id);
              next[asset.id] = {
                status: "ready",
                url: resource.url,
                ...(resource.cached ? { cached: true } : {}),
              };
            } else {
              next[asset.id] = { status: "error" };
            }
          }
          return next;
        });
      } catch {
        // 批量整体失败（超时/网络）：先在 finally 释放逐瓦片加载标记，
        // 再退回单资产路径（loadPreview 会因加载标记早退，顺序不可颠倒）。
        if (aliveRef.current && revision === cacheRevisionRef.current)
          batchFailed = true;
      } finally {
        cacheControllersRef.current.delete(controller);
        for (const asset of pending) {
          if (
            previewRequestVersionsRef.current.get(asset.id) ===
            versions.get(asset.id)
          )
            previewLoadingIdsRef.current.delete(asset.id);
        }
        if (batchFailed) {
          for (const asset of pending) void loadPreview(asset);
        }
      }
    },
    [loadPreview, user.id],
  );

  // MATERIAL-PERF-A（P0-4）：失败/过期预览有限次自动重签（1s/3s 退避），
  // 不再要求用户点击瓦片或重进页面。
  const retryPreview = (asset: StudioAsset) => {
    const attempts = previewRetryCountsRef.current.get(asset.id) ?? 0;
    if (attempts >= 2) return;
    previewRetryCountsRef.current.set(asset.id, attempts + 1);
    const timer = setTimeout(
      () => {
        previewRetryTimersRef.current.delete(timer);
        if (!aliveRef.current) return;
        if (!visiblePreviewIdsRef.current.has(asset.id)) return;
        void loadPreview(asset);
      },
      attempts === 0 ? 1_000 : 3_000,
    );
    previewRetryTimersRef.current.add(timer);
  };
  retryPreviewRef.current = retryPreview;

  const warmPreview = async (asset: StudioAsset) => {
    if (
      review ||
      !asset.assetId ||
      previewStates[asset.id]?.cached ||
      warmingIdsRef.current.has(asset.id) ||
      clearingCache ||
      cacheSuppressedRef.current
    )
      return;
    warmingIdsRef.current.add(asset.id);
    const revision = cacheRevisionRef.current;
    const controller = new AbortController();
    cacheControllersRef.current.add(controller);
    try {
      await evictionRef.current.get(asset.id);
      const result = await getMaterialCachedPreview(
        user.id,
        asset.previewAssetId ?? asset.assetId,
        { populate: true, signal: controller.signal },
      );
      result.release(); // Keep the currently playing source stable.
      if (aliveRef.current && revision === cacheRevisionRef.current) {
        setCacheMessage(
          result.cached
            ? "已缓存到本机，下次预览优先使用。"
            : "当前素材继续在线预览，未写入本机缓存。",
        );
        void refreshCacheUsage();
      }
    } catch {
      // Background caching must never interrupt the active player.
    } finally {
      cacheControllersRef.current.delete(controller);
    }
  };

  const invalidatePreview = (asset: StudioAsset, failedUrl?: string) => {
    const authId = asset.previewAssetId ?? asset.assetId ?? "";
    // 缩略图失效走单独一支：它报上来的是 poster 而不是 previewStates.url，
    // 落到下面的 wasReady 判据必然不匹配，瓦片会一直挂着失效的封面。客户版
    // 签名绑 session_epoch，换一次会话就会整片 403——表现为「用着用着图没了，
    // 刷新一下又好」。清掉本地缓存的地址并重新批量授权即可自愈。
    if (failedUrl && authId && thumbnailUrls[authId] === failedUrl) {
      setThumbnailUrls((current) => {
        const next = { ...current };
        delete next[authId];
        return next;
      });
      // 挂同一个有限次计数：抽帧失败的视频每次都会签出一条新地址却依旧 404，
      // 不设上限就是「重签 → 404 → 重签」的死循环。用尽次数后瓦片保留占位。
      const attempts = thumbnailRetryCountsRef.current.get(asset.id) ?? 0;
      if (attempts >= 2) return;
      thumbnailRetryCountsRef.current.set(asset.id, attempts + 1);
      void loadPreviewsBatch([asset]);
      return;
    }
    const wasReady = previewStates[asset.id]?.url === failedUrl;
    if (!wasReady) return;
    previewResourcesRef.current.get(asset.id)?.release();
    previewResourcesRef.current.delete(asset.id);
    if (failedUrl?.startsWith("blob:") && asset.assetId) {
      const eviction = evictMaterialCachedPreview(
        user.id,
        asset.previewAssetId ?? asset.assetId,
      ).catch(() => {});
      evictionRef.current.set(asset.id, eviction);
    }
    warmingIdsRef.current.delete(asset.id);
    setPreviewStates((current) =>
      failMaterialPreview(current, asset.id, failedUrl),
    );
    // MATERIAL-PERF-A（P0-4）：签名过期/媒体加载失败的瓦片自动重签一次，
    // 不再永久置灰等待用户点击或重进页面。
    if (failedUrl && !failedUrl.startsWith("blob:"))
      retryPreviewRef.current(asset);
  };

  const clearLocalCache = async () => {
    setClearingCache(true);
    cacheSuppressedRef.current = true;
    cacheRevisionRef.current += 1;
    for (const controller of cacheControllersRef.current) controller.abort();
    previewLoadingIdsRef.current.clear();
    try {
      await clearMaterialCache(user.id);
      if (!aliveRef.current) return;
      // Existing Blob URLs may finish playing; their persistent copies are gone.
      warmingIdsRef.current = new Set(visiblePreviewIdsRef.current);
      setCacheMessage("本机缓存已清理，云端素材保留。当前播放不受影响。");
      await refreshCacheUsage();
    } catch {
      if (aliveRef.current) setCacheMessage("清理本机缓存失败，请重试。");
    } finally {
      if (aliveRef.current) {
        setClearingCache(false);
        for (const asset of remoteAssets) {
          if (previewStates[asset.id]?.status !== "ready")
            void loadPreview(asset);
        }
      }
    }
  };

  useEffect(() => {
    if (review) return;
    // refreshRevision 仅作为“强制重拉”触发器，不参与请求参数。
    void refreshRevision;
    let current = true;
    setRemoteLoading(true);
    setRemoteError(undefined);
    void listMaterials({
      mediaType: kind === "全部" ? undefined : kind,
      source: source || undefined,
      query: query || undefined,
      group,
      page,
      pageSize,
    })
      .then((result) => {
        if (current) setRemotePage(result);
      })
      .catch((error: unknown) => {
        if (current) {
          setRemoteError(
            error instanceof Error ? error.message : "读取素材库失败",
          );
        }
      })
      .finally(() => {
        if (current) setRemoteLoading(false);
      });
    return () => {
      current = false;
    };
  }, [group, kind, page, query, refreshRevision, review, source]);

  // MATERIAL-UX-01：分组导航计数（随素材变动刷新）。
  useEffect(() => {
    if (review) return;
    // groupsRevision 仅作为“强制重拉”触发器。
    void groupsRevision;
    let current = true;
    void listMaterialGroups()
      .then((result) => {
        if (current) setGroups(result.items);
      })
      .catch(() => {
        /* 分组导航失败不阻塞素材浏览。 */
      });
    return () => {
      current = false;
    };
  }, [groupsRevision, review]);

  useEffect(() => {
    if (!review && remotePage?.page === page && page > pages) setPage(pages);
  }, [page, pages, remotePage?.page, review]);

  useEffect(() => {
    void kind;
    void page;
    void query;
    void source;
    void group;
    cacheSuppressedRef.current = false;
    // 列表参数变化后旧勾选可能已不可见：清空避免“已选 0 项”与按钮状态矛盾。
    setSelectedIds(new Set());
  }, [group, kind, page, query, source]);

  useEffect(() => {
    if (review || remotePage?.page !== page) return;
    const visibleIds = new Set(remoteAssets.map((asset) => asset.id));
    visiblePreviewIdsRef.current = visibleIds;
    for (const [id, resource] of previewResourcesRef.current) {
      if (!visibleIds.has(id)) {
        resource.release();
        previewResourcesRef.current.delete(id);
        warmingIdsRef.current.delete(id);
      }
    }
    setPreviewStates((current) =>
      Object.fromEntries(
        Object.entries(current).filter(([id]) => visibleIds.has(id)),
      ),
    );
    setThumbnailUrls((current) => {
      const visibleAuthIds = new Set(
        remoteAssets.map(
          (asset) => asset.previewAssetId ?? asset.assetId ?? "",
        ),
      );
      return Object.fromEntries(
        Object.entries(current).filter(([id]) => visibleAuthIds.has(id)),
      );
    });
    // 可见素材一次批量授权；仅 generationTaskId 素材走单资产回退。
    void loadPreviewsBatch(remoteAssets);
    for (const asset of remoteAssets) {
      if (asset.url || asset.assetId || asset.previewAssetId) continue;
      if (asset.allowedActions?.includes("preview") && asset.generationTaskId)
        void loadPreview(asset);
    }
    return () => {
      for (const id of visibleIds) {
        previewLoadingIdsRef.current.delete(id);
        previewRequestVersionsRef.current.set(
          id,
          (previewRequestVersionsRef.current.get(id) ?? 0) + 1,
        );
      }
    };
  }, [
    loadPreview,
    loadPreviewsBatch,
    page,
    remoteAssets,
    remotePage?.page,
    review,
  ]);

  useEffect(() => {
    if (selectedAssetIdRef.current === state.selectedAssetId) return;
    selectedAssetIdRef.current = state.selectedAssetId;
    if (review && selectedIndex >= 0) {
      setPage(Math.floor(selectedIndex / pageSize) + 1);
    }
  }, [review, selectedIndex, state.selectedAssetId]);

  useEffect(() => {
    setRenameValue(selected?.name ?? "");
    setGroupValue(selected?.group ?? "");
    setGroupNewName("");
  }, [selected?.group, selected?.name]);

  const currentAssets = review
    ? assets.slice((page - 1) * pageSize, page * pageSize)
    : assets;

  const retainForDraft = (asset: StudioAsset) => {
    const retained = asset.url?.startsWith("blob:")
      ? { ...asset, url: undefined }
      : asset;
    updateData((current) => ({
      ...current,
      assets: [
        retained,
        ...current.assets.filter((item) => item.id !== asset.id),
      ],
    }));
  };

  const handleUpload = async (file: File) => {
    setBusyAction("upload");
    setUploadProgress(0);
    try {
      // MATERIAL-UX-01：标题区可选上传目标分组，默认“我的上传”。
      const targetGroup =
        uploadGroup === "__new__" ? uploadGroupDraft.trim() : uploadGroup;
      const intent = await createMaterialUploadIntent(file, {
        title: file.name,
        group: targetGroup || "我的上传",
      });
      const completed = await putMaterial(intent, file, setUploadProgress);
      const asset = studioAssetFromMaterial(completed);
      retainForDraft(asset);
      setSelectedAsset(asset);
      patchState({ selectedAssetId: asset.id });
      setKind(completed.media_type);
      setPage(1);
      // 当前筛选不含新素材时不在本地插入，交由刷新纠正列表。
      if (group === undefined || group === completed.group) {
        setRemotePage((current) => ({
          items: [completed, ...(current?.items ?? [])]
            .filter(
              (item, index, items) =>
                items.findIndex((candidate) => candidate.id === item.id) ===
                index,
            )
            .slice(0, pageSize),
          page: 1,
          page_size: pageSize,
          total: (current?.total ?? 0) + 1,
        }));
      }
      notify(`素材“${completed.title}”已上传并永久保存`);
      refreshMaterials();
    } catch (error) {
      notify(error instanceof Error ? error.message : "上传素材失败");
    } finally {
      setBusyAction(undefined);
      setUploadProgress(undefined);
      if (uploadInputRef.current) uploadInputRef.current.value = "";
    }
  };

  const saveName = async () => {
    const title = renameInputRef.current?.value.trim() ?? renameValue.trim();
    if (!selected?.materialId || !title) return;
    setBusyAction("rename");
    try {
      const updated = studioAssetFromMaterial(
        await updateMaterial(selected.materialId, {
          title,
        }),
      );
      setSelectedAsset({ ...updated, url: selected.url });
      setRemotePage((current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.id === updated.materialId
                  ? { ...item, title: updated.name }
                  : item,
              ),
            }
          : current,
      );
      notify("素材名称已保存");
    } catch (error) {
      notify(error instanceof Error ? error.message : "更新素材失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const removeSelected = async () => {
    if (!selected?.materialId) return;
    setBusyAction("hide");
    try {
      await hideMaterial(selected.materialId);
      if (selected.assetId) {
        await evictMaterialCachedPreview(
          user.id,
          selected.previewAssetId ?? selected.assetId,
        ).catch(() => {});
        void refreshCacheUsage();
      }
      setSelectedAsset(undefined);
      patchState({ selectedAssetId: undefined });
      setRemotePage((current) =>
        current
          ? {
              ...current,
              items: current.items.filter(
                (item) => item.id !== selected.materialId,
              ),
              total: Math.max(0, current.total - 1),
            }
          : current,
      );
      notify("素材已从素材库移除，原业务记录仍保留");
      refreshMaterials();
    } catch (error) {
      notify(error instanceof Error ? error.message : "移除素材失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  // MATERIAL-UX-01：分组导航与整理模式（本页勾选、批量整理）。
  const selectGroup = (name: string | undefined) => {
    setGroup(name);
    setPage(1);
    setSelectedIds(new Set());
  };

  const toggleOrganize = () => {
    setOrganize((current) => !current);
    setSelectedIds(new Set());
    setBulkGroupChoice("");
    setBulkGroupDraft("");
  };

  const toggleSelected = (assetId: string) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(assetId)) next.delete(assetId);
      else next.add(assetId);
      return next;
    });
  };

  const selectablePageIds = currentAssets
    .filter((asset) => asset.materialId)
    .map((asset) => asset.id);
  const allPageSelected =
    selectablePageIds.length > 0 &&
    selectablePageIds.every((id) => selectedIds.has(id));

  const toggleSelectCurrentPage = () => {
    setSelectedIds((current) => {
      const next = new Set(current);
      for (const id of selectablePageIds) {
        if (allPageSelected) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  };

  const refreshMaterials = () => {
    setGroupsRevision((current) => current + 1);
    setRefreshRevision((current) => current + 1);
  };

  const bulkTargets = currentAssets.filter(
    (asset) => selectedIds.has(asset.id) && asset.materialId,
  );

  const runBulkUpdate = async (
    update: MaterialBulkUpdate,
    describe: (result: MaterialBulkResult) => string,
  ) => {
    const materialIds = [
      ...new Set(
        bulkTargets
          .map((asset) => asset.materialId)
          .filter((id): id is string => Boolean(id)),
      ),
    ];
    if (materialIds.length === 0) {
      notify("请先选择要整理的素材");
      return;
    }
    setBusyAction("bulk");
    try {
      const result = await bulkUpdateMaterials({
        material_ids: materialIds,
        update,
      });
      notify(describe(result));
      setSelectedIds(new Set());
      refreshMaterials();
    } catch (error) {
      notify(error instanceof Error ? error.message : "批量整理素材失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const bulkMoveToGroup = () => {
    const target =
      bulkGroupChoice === "__new__" ? bulkGroupDraft.trim() : bulkGroupChoice;
    if (!target) {
      notify("请先选择或填写目标分组");
      return;
    }
    void runBulkUpdate({ group: target }, (result) =>
      result.skipped > 0
        ? `已移动 ${result.updated} 项到“${target}”，${result.skipped} 项不支持调整分组`
        : `已移动 ${result.updated} 项到“${target}”`,
    );
  };

  const bulkRestoreDefault = () => {
    void runBulkUpdate({ group: null }, (result) =>
      result.skipped > 0
        ? `已恢复 ${result.updated} 项默认分组，${result.skipped} 项未变更`
        : `已恢复 ${result.updated} 项默认分组`,
    );
  };

  const bulkHide = () => {
    void runBulkUpdate({ hidden: true }, (result) =>
      result.skipped > 0
        ? `已移除 ${result.updated} 项，${result.skipped} 项未变更`
        : `已移除 ${result.updated} 项素材`,
    );
  };

  const bulkDownload = async () => {
    const targets = bulkTargets.filter(
      (asset): asset is StudioAsset & { assetId: string } =>
        Boolean(asset.assetId),
    );
    if (targets.length === 0) {
      notify("所选素材暂无可下载文件");
      return;
    }
    setBusyAction("bulk-download");
    let failed = 0;
    try {
      for (const [index, asset] of targets.entries()) {
        // 浏览器对连续下载有节流，逐项间隔触发避免被拦截。
        if (index > 0) await new Promise((resolve) => setTimeout(resolve, 150));
        try {
          await downloadMaterialAsset(asset.assetId, asset.name);
        } catch {
          failed += 1;
        }
      }
      notify(
        failed > 0
          ? `已开始下载 ${targets.length - failed} 项，${failed} 项失败`
          : `已开始下载 ${targets.length} 项素材`,
      );
    } finally {
      setBusyAction(undefined);
    }
  };

  const saveGroup = async () => {
    // 下拉切到“新建分组…”（哨兵 __new__）时取新建输入框的值。
    const target =
      groupValue === "__new__" ? groupNewName.trim() : groupValue.trim();
    if (!selected?.materialId || !target) return;
    setBusyAction("group");
    try {
      const updated = studioAssetFromMaterial(
        await updateMaterial(selected.materialId, {
          group: target,
        }),
      );
      setSelectedAsset({ ...updated, url: selected.url });
      setGroupValue(updated.group);
      setGroupNewName("");
      setRemotePage((current) =>
        current
          ? {
              ...current,
              items: current.items.map((item) =>
                item.id === updated.materialId
                  ? { ...item, group: updated.group }
                  : item,
              ),
            }
          : current,
      );
      notify("素材分组已保存");
      refreshMaterials();
    } catch (error) {
      notify(error instanceof Error ? error.message : "更新素材分组失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const downloadSelected = async () => {
    if (!selectedInput?.assetId) return;
    setBusyAction("download");
    try {
      await downloadMaterialAsset(selectedInput.assetId, selectedInput.name);
      notify("素材下载已开始");
    } catch (error) {
      notify(error instanceof Error ? error.message : "下载素材失败");
    } finally {
      setBusyAction(undefined);
    }
  };

  const applyAsReference = (asset: StudioAsset) => {
    retainForDraft(asset);
    patchDraft({
      referenceIds: [...new Set([...state.draft.referenceIds, asset.id])],
    });
    navigate("reference", { returnTo: "materials" });
  };

  return (
    <section className="content-page content-materials">
      <header className="content-title">
        <div>
          <h1>素材库</h1>
          <p>统一管理和复用乡墅创作素材</p>
        </div>
        <input
          accept=".jpg,.jpeg,.png,.mp3,.mp4,.mov"
          aria-label="选择上传素材"
          hidden
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void handleUpload(file);
          }}
          ref={uploadInputRef}
          type="file"
        />
        {!review ? (
          <div className="content-material-upload-group">
            <select
              aria-label="上传目标分组"
              value={uploadGroup}
              onChange={(event) => {
                setUploadGroup(event.target.value);
                if (event.target.value !== "__new__") setUploadGroupDraft("");
              }}
            >
              {[
                ...new Set([
                  uploadGroup === "__new__" ? "我的上传" : uploadGroup,
                  "我的上传",
                  ...groups.map((item) => item.name),
                ]),
              ].map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
              <option value="__new__">新建分组…</option>
            </select>
            {uploadGroup === "__new__" ? (
              <input
                aria-label="上传新分组名称"
                maxLength={80}
                placeholder="新分组名称"
                value={uploadGroupDraft}
                onChange={(event) => setUploadGroupDraft(event.target.value)}
              />
            ) : null}
          </div>
        ) : null}
        <Button
          className="content-title-action"
          disabled={busyAction === "upload"}
          variant="primary"
          onClick={() =>
            review
              ? notify("审核模式保留示例素材，不执行真实上传")
              : uploadInputRef.current?.click()
          }
        >
          {uploadProgress === undefined
            ? "上传素材"
            : `上传中 ${uploadProgress}%`}
        </Button>
      </header>
      <Tabs
        items={[
          { id: "全部", label: "全部" },
          { id: "video", label: "视频" },
          { id: "image", label: "图片" },
          { id: "audio", label: "音频" },
        ]}
        value={kind}
        onChange={(value) => {
          setKind(value as typeof kind);
          setPage(1);
        }}
      />
      {!review ? (
        <section aria-label="本机素材缓存" className="content-material-cache">
          <span>
            {cacheUsage?.available
              ? `本机缓存 ${(cacheUsage.bytes / 1024 / 1024).toFixed(1)} MB · 总上限 ${Math.round(cacheUsage.limitBytes / 1024 / 1024)} MB`
              : cacheUsage
                ? "当前浏览器暂不支持本机缓存，使用在线预览。"
                : "正在读取本机缓存…"}
          </span>
          <Button
            variant="quiet"
            disabled={clearingCache || !cacheUsage?.available}
            onClick={() => void clearLocalCache()}
          >
            {clearingCache ? "正在清理…" : "清理本机缓存"}
          </Button>
          <p>
            首屏优先在线预览并复用已有本机缓存；视频和音频首次播放时后台缓存。
            单个文件不超过 50 MB，超限继续在线预览。
          </p>
          {cacheMessage ? <p role="status">{cacheMessage}</p> : null}
        </section>
      ) : null}
      {!review ? (
        <form
          aria-label="素材筛选"
          className="content-material-filters"
          onSubmit={(event) => {
            event.preventDefault();
            setPage(1);
            setQuery(queryInput.trim());
          }}
        >
          <input
            aria-label="搜索素材"
            maxLength={120}
            placeholder="搜索素材名称"
            value={queryInput}
            onChange={(event) => setQueryInput(event.target.value)}
          />
          <select
            aria-label="素材来源"
            value={source}
            onChange={(event) => {
              setSource(event.target.value as typeof source);
              setPage(1);
            }}
          >
            <option value="">全部来源</option>
            <option value="upload">我的上传</option>
            <option value="project">项目素材</option>
            <option value="character">人物素材</option>
            <option value="oral">口播成片</option>
            <option value="generation">视频成片</option>
          </select>
          <Button type="submit" variant="outline">
            搜索
          </Button>
        </form>
      ) : null}
      <section className="content-material-layout">
        {!review ? (
          <aside aria-label="素材分组导航" className="content-material-groups">
            <button
              aria-label="筛选分组 全部素材"
              aria-pressed={group === undefined}
              className={`content-material-group ${group === undefined ? "is-active" : ""}`}
              type="button"
              onClick={() => selectGroup(undefined)}
            >
              <span className="content-material-group__name">全部素材</span>
            </button>
            {groups.map((item) => (
              <button
                key={item.name}
                aria-label={`筛选分组 ${item.name}`}
                aria-pressed={group === item.name}
                className={`content-material-group ${group === item.name ? "is-active" : ""}`}
                type="button"
                onClick={() => selectGroup(item.name)}
              >
                <span className="content-material-group__name">
                  {item.name}
                </span>
                <span className="content-material-group__count">
                  {item.count}
                </span>
              </button>
            ))}
          </aside>
        ) : null}
        <div className="content-material-list">
          {!review ? (
            <div className="content-material-toolbar">
              <span className="content-material-toolbar__summary">
                {organize ? `已选 ${bulkTargets.length} 项` : `共 ${total} 条`}
              </span>
              {organize ? (
                <>
                  <Button
                    disabled={selectablePageIds.length === 0}
                    variant="quiet"
                    onClick={toggleSelectCurrentPage}
                  >
                    {allPageSelected ? "取消本页" : "全选本页"}
                  </Button>
                  <Button
                    disabled={selectedIds.size === 0}
                    variant="quiet"
                    onClick={() => setSelectedIds(new Set())}
                  >
                    清空选择
                  </Button>
                </>
              ) : null}
              <Button
                variant={organize ? "primary" : "outline"}
                onClick={toggleOrganize}
              >
                {organize ? "退出整理" : "整理素材"}
              </Button>
            </div>
          ) : null}
          {organize ? (
            <div
              aria-label="批量整理"
              className="content-material-bulkbar"
              role="toolbar"
            >
              <select
                aria-label="批量目标分组"
                value={bulkGroupChoice}
                onChange={(event) => setBulkGroupChoice(event.target.value)}
              >
                <option value="">选择目标分组…</option>
                {groups.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                  </option>
                ))}
                <option value="__new__">新建分组…</option>
              </select>
              {bulkGroupChoice === "__new__" ? (
                <input
                  aria-label="批量新分组名称"
                  maxLength={80}
                  placeholder="新分组名称"
                  value={bulkGroupDraft}
                  onChange={(event) => setBulkGroupDraft(event.target.value)}
                />
              ) : null}
              <Button
                disabled={bulkTargets.length === 0 || busyAction !== undefined}
                variant="primary"
                onClick={bulkMoveToGroup}
              >
                移入分组
              </Button>
              <Button
                disabled={bulkTargets.length === 0 || busyAction !== undefined}
                variant="outline"
                onClick={bulkRestoreDefault}
              >
                恢复默认分组
              </Button>
              <Button
                disabled={bulkTargets.length === 0 || busyAction !== undefined}
                variant="outline"
                onClick={() => void bulkDownload()}
              >
                批量下载
              </Button>
              <Button
                disabled={bulkTargets.length === 0 || busyAction !== undefined}
                variant="quiet"
                onClick={bulkHide}
              >
                移入回收侧
              </Button>
            </div>
          ) : null}
          <div className="content-asset-grid">
            {currentAssets.map((asset) => {
              const authId = asset.previewAssetId ?? asset.assetId ?? "";
              // MATERIAL-THUMBS-B / MATERIAL-UX-02：带封面的视频瓦片用 img 展示
              // 缩略图（懒加载），不再让浏览器经服务端代理流式拉原视频；图片瓦片
              // 同样走派生缩略图（单张手机照片可达 10MB）；点开详情仍用原图/原视频。
              const thumbnailUrl =
                asset.kind === "video" || asset.kind === "image"
                  ? thumbnailUrls[authId]
                  : undefined;
              return (
                <AssetCard
                  key={asset.id}
                  asset={{
                    ...asset,
                    url:
                      asset.kind === "image"
                        ? (thumbnailUrl ??
                          asset.url ??
                          previewStates[asset.id]?.url)
                        : thumbnailUrl
                          ? undefined
                          : (asset.url ?? previewStates[asset.id]?.url),
                    poster: thumbnailUrl ?? asset.poster,
                  }}
                  selected={selected?.id === asset.id}
                  organize={organize}
                  checked={selectedIds.has(asset.id)}
                  previewStatus={previewStates[asset.id]?.status}
                  onSelect={() => {
                    if (organize) {
                      toggleSelected(asset.id);
                      return;
                    }
                    if (previewStates[asset.id]?.status === "error")
                      void loadPreview(asset);
                    setSelectedAsset(asset);
                    patchState({ selectedAssetId: asset.id });
                  }}
                  onPreviewError={(failedUrl) => {
                    invalidatePreview(asset, failedUrl);
                  }}
                  onPlay={() => void warmPreview(asset)}
                />
              );
            })}
          </div>
          {remoteLoading ? <Hint>正在读取云端素材…</Hint> : null}
          {remoteError ? <Hint>{remoteError}</Hint> : null}
          {!remoteLoading && !remoteError && currentAssets.length === 0 ? (
            <Empty title="暂无素材" description="上传后即可跨项目复用。" />
          ) : null}
          {total > pageSize ? (
            <nav className="content-pagination" aria-label="素材分页">
              <span>
                共 {total} 条 · 每页 {pageSize} 条
              </span>
              <Button
                aria-label="上一页"
                disabled={page === 1}
                variant="outline"
                onClick={() => setPage((currentPage) => currentPage - 1)}
              >
                ‹
              </Button>
              {visiblePageButtons(page, pages).flatMap(
                (entry, entryIndex, all) =>
                  entry === "…"
                    ? [
                        <span
                          key={`gap-${all[entryIndex + 1]}`}
                          className="content-page-gap"
                        >
                          …
                        </span>,
                      ]
                    : [
                        <Button
                          key={entry}
                          variant={page === entry ? "primary" : "quiet"}
                          onClick={() => setPage(entry)}
                        >
                          {entry}
                        </Button>,
                      ],
              )}
              <Button
                aria-label="下一页"
                disabled={page === pages}
                variant="outline"
                onClick={() => setPage((currentPage) => currentPage + 1)}
              >
                ›
              </Button>
            </nav>
          ) : null}
        </div>
        <Panel className="content-inspector">
          {selected ? (
            <>
              <h2>{selected.name}</h2>
              {selected.composite && selected.characterViews?.length ? (
                <CharacterMaterialViews
                  key={selected.id}
                  userId={user.id}
                  cacheRevision={cacheRevisionRef.current}
                  cacheClearing={clearingCache}
                  cachePopulateAllowed={!cacheSuppressedRef.current}
                  asset={selected}
                  onSelected={setCharacterView}
                />
              ) : (
                <Media
                  asset={{
                    ...selected,
                    url: selected.url ?? previewStates[selected.id]?.url,
                    poster:
                      (selected.kind === "video"
                        ? thumbnailUrls[
                            selected.previewAssetId ?? selected.assetId ?? ""
                          ]
                        : undefined) ?? selected.poster,
                  }}
                  alt={selected.name}
                  onError={(failedUrl) => {
                    invalidatePreview(selected, failedUrl);
                  }}
                  onPlay={() => void warmPreview(selected)}
                />
              )}
              <dl>
                <dt>类型</dt>
                <dd>
                  {selected.composite
                    ? "五视图合成图 · 1 套"
                    : assetKindLabel(selected.kind)}
                </dd>
                <dt>来源</dt>
                <dd>{selected.source}</dd>
                <dt>归属</dt>
                <dd>{selected.personId ? "人物库" : selected.group}</dd>
                <dt>状态</dt>
                <dd>
                  {selected.delivery === "direct"
                    ? "生成完成，尚未归档"
                    : selected.saved
                      ? "云端永久保存"
                      : "处理中"}
                </dd>
              </dl>
              {selected.composite && selected.personId ? (
                <Button
                  variant="outline"
                  onClick={() =>
                    navigate("person-photos", {
                      selectedPersonId: selected.personId,
                    })
                  }
                >
                  查看人物与单独视角
                </Button>
              ) : null}
              {selected.kind === "audio" &&
              (review || selected.allowedUses?.includes("oral_audio")) ? (
                <Button
                  variant="primary"
                  onClick={() => {
                    retainForDraft(selected);
                    patchDraft({
                      audioId: selected.id,
                      ipId: selected.personId,
                      voiceId: undefined,
                    });
                    navigate("oral-audio", { returnTo: "materials" });
                  }}
                >
                  用于音频口播
                </Button>
              ) : null}
              {selected.kind === "image" &&
              selectedInput?.allowedUses?.includes("original_frame") ? (
                <Button
                  variant="outline"
                  onClick={() => {
                    retainForDraft(selectedInput);
                    patchDraft({
                      originalImageId: selectedInput.id,
                      frameConfirmed: false,
                    });
                    navigate("replica", { returnTo: "materials" });
                  }}
                >
                  用作原画面
                </Button>
              ) : null}
              {selected.kind === "image" &&
              selectedInput?.allowedUses?.includes("first_frame") ? (
                <Button
                  variant="outline"
                  onClick={() => {
                    retainForDraft(selectedInput);
                    patchDraft({ firstFrameId: selectedInput.id });
                    navigate("video", { returnTo: "materials" });
                  }}
                >
                  用作首帧
                </Button>
              ) : null}
              {selected.kind === "image" &&
              selectedInput?.allowedUses?.includes("tail_frame") ? (
                <Button
                  variant="outline"
                  onClick={() => {
                    retainForDraft(selectedInput);
                    patchDraft({ tailFrameId: selectedInput.id });
                    navigate("video", { returnTo: "materials" });
                  }}
                >
                  用作尾帧
                </Button>
              ) : null}
              {selectedInput?.allowedUses?.includes("reference") ? (
                <Button
                  variant="outline"
                  onClick={() => applyAsReference(selectedInput)}
                >
                  用于参考生视频
                </Button>
              ) : null}
              {selected.materialId &&
              selected.allowedActions?.includes("rename") ? (
                <>
                  <div className="content-material-manage">
                    <Field label="素材名称">
                      <input
                        aria-label="素材名称"
                        maxLength={120}
                        onChange={(event) => setRenameValue(event.target.value)}
                        ref={renameInputRef}
                        value={renameValue}
                      />
                    </Field>
                    <Button
                      disabled={busyAction === "rename" || !renameValue.trim()}
                      onClick={() => void saveName()}
                      variant="outline"
                    >
                      保存名称
                    </Button>
                  </div>
                  <div className="content-material-manage">
                    <Field label="素材分组">
                      <select
                        aria-label="素材分组"
                        value={groupValue}
                        onChange={(event) => {
                          setGroupValue(event.target.value);
                          if (event.target.value !== "__new__")
                            setGroupNewName("");
                        }}
                      >
                        {[
                          ...new Set([
                            ...(groupValue && groupValue !== "__new__"
                              ? [groupValue]
                              : []),
                            ...groups.map((item) => item.name),
                          ]),
                        ].map((name) => (
                          <option key={name} value={name}>
                            {name}
                          </option>
                        ))}
                        <option value="__new__">新建分组…</option>
                      </select>
                    </Field>
                    {groupValue === "__new__" ? (
                      <Field label="新分组名称">
                        <input
                          aria-label="新分组名称"
                          maxLength={80}
                          placeholder="输入新分组名称"
                          value={groupNewName}
                          onChange={(event) =>
                            setGroupNewName(event.target.value)
                          }
                        />
                      </Field>
                    ) : null}
                    <Button
                      disabled={
                        busyAction === "group" ||
                        (groupValue === "__new__"
                          ? !groupNewName.trim()
                          : !groupValue.trim())
                      }
                      onClick={() => void saveGroup()}
                      variant="outline"
                    >
                      保存分组
                    </Button>
                  </div>
                </>
              ) : null}
              {selected.assetId &&
              selected.allowedActions?.includes("download") ? (
                <Button
                  disabled={busyAction === "download"}
                  onClick={() => void downloadSelected()}
                  variant="outline"
                >
                  下载素材
                </Button>
              ) : null}
              {selected.materialId &&
              selected.allowedActions?.includes("hide") ? (
                <Button
                  disabled={busyAction === "hide"}
                  onClick={() => void removeSelected()}
                  variant="quiet"
                >
                  从素材库移除
                </Button>
              ) : null}
              {selected.delivery === "direct" ? (
                <Hint>该结果可预览，归档完成后可作为云端素材复用。</Hint>
              ) : null}
            </>
          ) : (
            <Empty
              title="选择一个素材"
              description="统一选择后再进入相应创作流程。"
            />
          )}
        </Panel>
      </section>
    </section>
  );
}

function publishPlatformLabel(platform: string) {
  return platform === "douyin"
    ? "抖音"
    : platform === "wechat_channels"
      ? "视频号"
      : "小红书";
}
function formCoverId(
  drafts: StudioPublishDraft[] | undefined,
  assetId: string | undefined,
) {
  return drafts?.find((draft) => draft.assetId === assetId)?.coverId;
}
export function parsePublishDrafts(value: unknown): StudioPublishDraft[] {
  if (!Array.isArray(value) || value.length > 50)
    throw new Error("云端发布草稿格式不正确，请联系支持。");
  return value.map((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      typeof item.id !== "string" ||
      typeof item.assetId !== "string" ||
      typeof item.title !== "string" ||
      typeof item.description !== "string" ||
      typeof item.account !== "string" ||
      !["抖音", "视频号", "小红书"].includes(item.platform) ||
      !Array.isArray(item.tags) ||
      item.tags.some((tag: unknown) => typeof tag !== "string") ||
      (item.coverId !== undefined && typeof item.coverId !== "string") ||
      (item.scheduledAt !== undefined && typeof item.scheduledAt !== "string")
    )
      throw new Error("云端发布草稿内容不完整，请联系支持。");
    return {
      id: item.id,
      assetId: item.assetId,
      coverId: item.coverId,
      platform: item.platform,
      account: item.account,
      title: item.title,
      description: item.description,
      tags: item.tags,
      ...(item.scheduledAt ? { scheduledAt: item.scheduledAt } : {}),
    };
  });
}

/** `<input type="datetime-local">` value (local wall clock) → ISO instant, or null. */
export function isoFromLocalDateTimeInput(value: string): string | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

/** ISO instant → `<input type="datetime-local">` value in the viewer's zone. */
export function localDateTimeInputFromIso(value: string | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const PUBLISH_SCHEDULE_MIN_LEAD_MS = 2 * 60 * 1000;
const PUBLISH_SCHEDULE_MAX_LEAD_MS = 30 * 24 * 60 * 60 * 1000;

export function validatePublishSchedule(
  iso: string | null,
  now: Date = new Date(),
): string | null {
  if (iso === null) return null;
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return "请填写有效的定时时间。";
  if (at < now.getTime() + PUBLISH_SCHEDULE_MIN_LEAD_MS)
    return "定时发布至少需要提前 2 分钟。";
  if (at > now.getTime() + PUBLISH_SCHEDULE_MAX_LEAD_MS)
    return "定时发布最多只能提前 30 天。";
  return null;
}

function newPublishDraft(
  asset: StudioAsset | undefined,
  review: boolean,
): StudioPublishDraft {
  return {
    id: `publish-${asset?.id ?? "unsaved"}`,
    assetId: asset?.id ?? "",
    coverId: asset?.id,
    platform: "抖音",
    account: review ? "张工说乡墅" : "",
    title: asset?.name ?? "",
    description: review
      ? "主体之外，门窗、水电、防水和庭院，也要提前规划。"
      : "",
    tags: review ? ["农村自建房", "建房预算"] : [],
  };
}

export function PublishPage() {
  const {
    data,
    navigate,
    notify,
    patchState,
    review,
    state,
    user,
    updateData,
  } = useStudio();
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const completedResultIds = new Set(
    data.tasks
      .filter((task) => task.status === "completed" && task.resultId)
      .map((task) => task.resultId),
  );
  const [cloudRevision, setCloudRevision] = useState<number | null>(
    review ? 0 : null,
  );
  const [cloudError, setCloudError] = useState("");
  const [actionError, setActionError] = useState("");
  const [actionBusy, setActionBusy] = useState(false);
  const [reload, setReload] = useState(0);
  // Delivery accounts live on the server (cloud QR logins + imported desktop logins);
  // local WebView2 profiles only back the manual "前往官方发布" fallback on desktop.
  const [accounts, setAccounts] = useState<CloudPublishAccount[]>([]);
  const [localProfiles, setLocalProfiles] = useState<LocalPublishAccount[]>([]);
  const [recordsToken, setRecordsToken] = useState(0);
  const [scheduleMode, setScheduleMode] = useState<"now" | "later">("now");
  const [scheduleInput, setScheduleInput] = useState("");
  const [cloudVideos, setCloudVideos] = useState<{
    requested: string[];
    available: string[];
  }>({ requested: [], available: [] });
  const operation = useRef(0);
  const mutationPending = useRef(false);
  const coverInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    void reload;
    void user.id;
    const current = ++operation.current;
    if (review) return;
    setCloudRevision(null);
    setCloudError("");
    setCloudVideos({ requested: [], available: [] });
    void (async () => {
      try {
        const record = await getStudioDraft("publishing");
        const drafts = parsePublishDrafts(record.payload.drafts);
        const ids = [
          ...new Set(
            drafts.flatMap((draft) => [
              draft.assetId,
              ...(draft.coverId ? [draft.coverId] : []),
            ]),
          ),
        ];
        const materials = ids.length
          ? await resolveMaterials(
              ids.map((id) => (id.startsWith("asset:") ? id : `asset:${id}`)),
            )
          : { items: [] };
        if (operation.current !== current) return;
        const previewResults = await Promise.allSettled(
          materials.items.map(async (item) => {
            const asset = studioAssetFromMaterial(item);
            if (!item.asset_id || !item.allowed_actions.includes("preview"))
              return asset;
            return {
              ...asset,
              url: (await getAssetDownloadUrl(item.asset_id)).url,
            };
          }),
        );
        if (operation.current !== current) return;
        const assets = previewResults.map((result, index) =>
          result.status === "fulfilled"
            ? result.value
            : studioAssetFromMaterial(materials.items[index]),
        );
        if (previewResults.some((result) => result.status === "rejected"))
          setActionError(
            "部分草稿素材预览暂时不可用，可重新加载；草稿内容已保留。",
          );
        setCloudVideos({
          requested: drafts.map((draft) => draft.assetId),
          available: materials.items
            .filter(
              (item) =>
                item.media_type === "video" &&
                item.status === "ready" &&
                item.allowed_actions.includes("download"),
            )
            .map((item) => item.asset_id ?? item.id),
        });
        updateData((previous) => ({
          ...previous,
          assets: [
            ...previous.assets.filter(
              (asset) => !assets.some((next) => next.id === asset.id),
            ),
            ...assets,
          ],
        }));
        patchState({ publishDrafts: drafts });
        setCloudRevision(record.revision);
      } catch (cause) {
        if (operation.current !== current) return;
        if (apiErrorCode(cause) === "STUDIO_DRAFT_NOT_FOUND") {
          patchState({ publishDrafts: [] });
          setCloudRevision(0);
        } else
          setCloudError(
            cause instanceof Error
              ? cause.message
              : "读取发布草稿失败，请重试。",
          );
      }
    })();
    return () => {
      operation.current += 1;
    };
  }, [review, user.id, reload, patchState, updateData]);
  useEffect(() => {
    void reload;
    let active = true;
    setAccounts([]);
    setLocalProfiles([]);
    if (review) return;
    void listCloudPublishAccounts()
      .then((value) => {
        if (active) setAccounts(value);
      })
      .catch((cause) => {
        if (active)
          setActionError(
            cause instanceof Error ? cause.message : "读取发布账号失败",
          );
      });
    if (canUseLocalPublishAccounts())
      void listLocalPublishAccounts(user.id)
        .then((value) => {
          if (active) setLocalProfiles(value);
        })
        .catch(() => {
          // Manual fallback only; the server-side list is the delivery source.
        });
    return () => {
      active = false;
    };
  }, [user.id, review, reload]);
  const selectedAsset = data.assets.find(
    (asset) =>
      asset.id === state.selectedAssetId &&
      asset.kind === "video" &&
      (cloudVideos.requested.includes(asset.id)
        ? cloudVideos.available.includes(asset.id)
        : completedResultIds.size === 0 || completedResultIds.has(asset.id)),
  );
  const coverCandidates = [
    ...(selectedAsset ? [selectedAsset] : []),
    ...data.assets.filter(
      (asset) =>
        asset.id !== selectedAsset?.id &&
        asset.kind === "image" &&
        (asset.id === formCoverId(state.publishDrafts, state.selectedAssetId) ||
          Boolean(asset.personId) ||
          asset.group.includes("场景") ||
          asset.group === "发布封面"),
    ),
  ];
  const savedDraftForAsset = state.publishDrafts?.find(
    (draft) => draft.assetId === selectedAsset?.id,
  );
  const visibleDrafts =
    state.publishDrafts ??
    (review && selectedAsset ? [newPublishDraft(selectedAsset, review)] : []);
  const [form, setForm] = useState<StudioPublishDraft>(
    () => savedDraftForAsset ?? newPublishDraft(selectedAsset, review),
  );
  const [tagInput, setTagInput] = useState("");
  const [saveNotice, setSaveNotice] = useState(false);
  const draftCount = visibleDrafts.length;
  const selectedAccount = accounts.find(
    (account) =>
      account.id === form.account &&
      publishPlatformLabel(account.platform) === form.platform,
  );
  const platformAccounts = accounts.filter(
    (account) => publishPlatformLabel(account.platform) === form.platform,
  );
  const selectedLocalProfile = selectedAccount
    ? localProfiles.find(
        (profile) =>
          profile.platform === selectedAccount.platform &&
          profile.platform_user_id === selectedAccount.platform_user_id,
      )
    : undefined;
  const platformDeliverable = form.platform !== "小红书";
  const selectedCover =
    coverCandidates.find((asset) => asset.id === form.coverId) ??
    coverCandidates[0];

  useEffect(() => {
    const next = savedDraftForAsset ?? newPublishDraft(selectedAsset, review);
    setForm(next);
    setScheduleMode(next.scheduledAt ? "later" : "now");
    setScheduleInput(localDateTimeInputFromIso(next.scheduledAt));
    setSaveNotice(false);
  }, [review, savedDraftForAsset, selectedAsset]);

  const updateForm = (patch: Partial<StudioPublishDraft>) => {
    setSaveNotice(false);
    setForm((current) => ({ ...current, ...patch }));
  };

  const addTag = () => {
    const tag = tagInput.trim();
    if (
      !tag ||
      tag.length > 50 ||
      form.tags.length >= 10 ||
      form.tags.includes(tag)
    ) {
      setTagInput("");
      return;
    }
    updateForm({ tags: [...form.tags, tag] });
    setTagInput("");
  };

  const savePublishDraft = async () => {
    if (!selectedAsset || mutationPending.current || cloudRevision === null)
      return;
    const scheduledAt =
      scheduleMode === "later"
        ? (isoFromLocalDateTimeInput(scheduleInput) ?? undefined)
        : undefined;
    const savedDraft = {
      ...form,
      ...(scheduledAt ? { scheduledAt } : {}),
      id: savedDraftForAsset?.id ?? `publish-${selectedAsset.id}`,
      assetId: selectedAsset.id,
      coverId: selectedCover?.id,
    };
    const currentDrafts = state.publishDrafts ?? [];
    const drafts = savedDraftForAsset
      ? currentDrafts.map((draft) =>
          draft.id === savedDraftForAsset.id ? savedDraft : draft,
        )
      : [...currentDrafts, savedDraft];
    if (drafts.length > 50) {
      setActionError("最多保存 50 条发布草稿，请先移除旧草稿。");
      return;
    }
    mutationPending.current = true;
    setActionBusy(true);
    setActionError("");
    const current = operation.current;
    try {
      if (!review) {
        const record = await saveStudioDraft(
          "publishing",
          { drafts },
          false,
          cloudRevision,
        );
        if (current !== operation.current) return;
        setCloudRevision(record.revision);
      }
      patchState({ publishDrafts: drafts });
      setForm(savedDraft);
      setSaveNotice(true);
    } catch (cause) {
      if (current === operation.current)
        setActionError(
          cause instanceof Error ? cause.message : "保存草稿失败，请重试。",
        );
    } finally {
      mutationPending.current = false;
      if (current === operation.current) setActionBusy(false);
    }
  };

  async function submitPublish() {
    if (!selectedAsset || !selectedAccount || mutationPending.current) return;
    const scheduledAt =
      scheduleMode === "later"
        ? isoFromLocalDateTimeInput(scheduleInput)
        : null;
    if (scheduleMode === "later" && scheduledAt === null) {
      setActionError("请填写定时发布时间。");
      return;
    }
    const scheduleError = validatePublishSchedule(scheduledAt);
    if (scheduleError) {
      setActionError(scheduleError);
      return;
    }
    if (!platformDeliverable) {
      setActionError("小红书自动发布即将上线，请先前往官方页面发布。");
      return;
    }
    await perform(async () => {
      const record = await createPublishRecord({
        account_id: selectedAccount.id,
        video_material_id:
          selectedAsset.materialId ??
          `asset:${selectedAsset.assetId ?? selectedAsset.id}`,
        cover_material_id:
          selectedCover && selectedCover.kind === "image"
            ? (selectedCover.materialId ??
              `asset:${selectedCover.assetId ?? selectedCover.id}`)
            : null,
        title: form.title.trim(),
        description: form.description.trim(),
        tags: form.tags,
        scheduled_at: scheduledAt,
      });
      setRecordsToken((value) => value + 1);
      notifyRef.current(
        record.scheduled_at
          ? `已加入定时发布队列 · ${publishPlatformLabel(record.platform)}`
          : `已提交发布 · ${publishPlatformLabel(record.platform)}`,
      );
    });
  }

  async function uploadCover(file: File) {
    if (!file.type.startsWith("image/") || file.size > 20 * 1024 * 1024) {
      setActionError("封面请选择 20 MB 以内的图片。");
      return;
    }
    if (mutationPending.current) return;
    mutationPending.current = true;
    setActionBusy(true);
    setActionError("");
    const current = operation.current;
    try {
      const intent = await createMaterialUploadIntent(file, {
        title: file.name,
        group: "发布封面",
      });
      const asset = studioAssetFromMaterial(
        await putMaterial(intent, file, () => {}),
      );
      if (current !== operation.current) return;
      updateData((previous) => ({
        ...previous,
        assets: [
          ...previous.assets.filter((item) => item.id !== asset.id),
          asset,
        ],
      }));
      updateForm({ coverId: asset.id });
    } catch (cause) {
      if (current === operation.current)
        setActionError(cause instanceof Error ? cause.message : "上传封面失败");
    } finally {
      mutationPending.current = false;
      if (current === operation.current) setActionBusy(false);
    }
  }
  async function perform(action: () => Promise<void>) {
    if (mutationPending.current) return;
    mutationPending.current = true;
    setActionBusy(true);
    setActionError("");
    try {
      await action();
    } catch (cause) {
      setActionError(
        cause instanceof Error ? cause.message : "操作失败，请重试。",
      );
    } finally {
      mutationPending.current = false;
      setActionBusy(false);
    }
  }

  async function removeCurrentDraft() {
    if (
      !savedDraftForAsset ||
      cloudRevision === null ||
      !window.confirm("移除当前发布草稿？视频与封面素材会保留。")
    )
      return;
    const current = operation.current;
    const drafts = (state.publishDrafts ?? []).filter(
      (draft) => draft.id !== savedDraftForAsset.id,
    );
    await perform(async () => {
      if (!review) {
        const record = await saveStudioDraft(
          "publishing",
          { drafts },
          false,
          cloudRevision,
        );
        if (current !== operation.current) return;
        setCloudRevision(record.revision);
      }
      patchState({ publishDrafts: drafts });
      setForm(newPublishDraft(selectedAsset, review));
      setSaveNotice(false);
    });
  }

  return (
    <section className="content-page content-publish">
      <header className="content-title">
        <div>
          <h1>发布管理</h1>
          <p>选择成片与账号，立即发布或定时发布；发布结果在下方记录中回收</p>
        </div>
      </header>
      <section className="content-publish-layout">
        <Panel className="content-publish-drafts">
          <div className="content-publish-draft-tabs">
            <strong>
              发布草稿 <b>{draftCount}</b>
            </strong>
            <span>草稿保存在云端，可反复编辑</span>
          </div>
          {visibleDrafts.length ? (
            visibleDrafts.map((draft) => {
              const draftAsset = data.assets.find(
                (asset) => asset.id === draft.assetId,
              );
              const isReviewSample = !state.publishDrafts;
              return (
                <button
                  className="content-publish-draft-card"
                  key={draft.id}
                  disabled={actionBusy || cloudRevision === null}
                  onClick={() => {
                    setForm(draft);
                    setSaveNotice(false);
                    patchState({ selectedAssetId: draft.assetId });
                  }}
                  type="button"
                >
                  {draftAsset ? (
                    <Media
                      asset={draftAsset}
                      alt={`${draft.title} 草稿封面`}
                      presentation="video"
                    />
                  ) : (
                    <div className="content-publish-draft-card-empty">
                      <Icon name="video" />
                    </div>
                  )}
                  <span>
                    <strong>{draft.title || "未命名发布草稿"}</strong>
                    <small>来源：任务中心</small>
                    <i>已保存 · 待发布</i>
                    <small>{isReviewSample ? "审核示例" : "云端草稿"}</small>
                  </span>
                </button>
              );
            })
          ) : (
            <Empty
              title="暂无发布草稿"
              description={
                selectedAsset
                  ? "填写右侧信息后保存到云端。"
                  : "从任务中心选择一条已完成的视频后创建草稿。"
              }
            />
          )}
          {!review && (
            <div className="content-publish-records-section">
              <div className="content-publish-draft-tabs">
                <strong>发布记录</strong>
                <span>排队 / 发布中会自动刷新</span>
              </div>
              <PublishRecordsPanel
                refreshToken={recordsToken}
                disabled={actionBusy || user.role === "auditor"}
              />
            </div>
          )}
        </Panel>
        <Panel className="content-publish-editor">
          <header className="content-publish-editor-heading">
            <h2>编辑发布草稿</h2>
            <Hint>发布文案独立于口播终稿；点击发布后由服务端投递到平台。</Hint>
          </header>
          <div className="content-publish-media-grid">
            <section className="content-publish-preview">
              <h3>视频预览</h3>
              {selectedAsset ? (
                <Media
                  asset={selectedAsset}
                  alt={`${selectedAsset.name} 视频预览`}
                />
              ) : (
                <Empty
                  title="暂无可发布成片"
                  description="请从任务中心选择一条已完成的视频。"
                />
              )}
            </section>
            <section className="content-publish-cover">
              <h3>封面选择</h3>
              {coverCandidates.length ? (
                <div className="content-cover-options">
                  {coverCandidates.map((asset, index) => (
                    <button
                      disabled={actionBusy || cloudRevision === null}
                      aria-label={`选择封面 ${asset.name}`}
                      className={
                        selectedCover?.id === asset.id ? "is-selected" : ""
                      }
                      key={asset.id}
                      onClick={() => updateForm({ coverId: asset.id })}
                      type="button"
                    >
                      <Media
                        asset={asset}
                        alt={`封面 ${index + 1}`}
                        presentation="video"
                      />
                    </button>
                  ))}
                </div>
              ) : (
                <div className="content-cover-empty">
                  <Icon name="image" />
                  <span>暂无可选封面</span>
                </div>
              )}
              <input
                hidden
                ref={coverInput}
                type="file"
                accept="image/*"
                aria-label="上传发布封面"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  event.target.value = "";
                  if (file) void uploadCover(file);
                }}
              />
              <Button
                disabled={review || actionBusy || user.role === "auditor"}
                variant="outline"
                onClick={() => coverInput.current?.click()}
              >
                上传自定义封面
              </Button>
            </section>
          </div>
          <div className="content-publish-fields">
            <Field label="发布平台">
              <div className="content-platform-options">
                {(["抖音", "视频号", "小红书"] as const).map((platform) => (
                  <Button
                    disabled={actionBusy || cloudRevision === null}
                    aria-pressed={form.platform === platform}
                    key={platform}
                    onClick={() => updateForm({ platform, account: "" })}
                    variant={form.platform === platform ? "primary" : "outline"}
                  >
                    <PlatformLogo platform={platform} /> {platform}
                  </Button>
                ))}
              </div>
            </Field>
            <Field label="发布账号">
              {review ? (
                <div className="content-publish-account">
                  <Button
                    aria-pressed={form.account === "张工说乡墅"}
                    onClick={() => updateForm({ account: "张工说乡墅" })}
                    variant="outline"
                  >
                    张工说乡墅
                  </Button>
                  <span>审核示例账号</span>
                </div>
              ) : platformAccounts.length ? (
                <div className="content-publish-account">
                  {/* A select cannot render pictures, so the chosen account's
                      avatar sits beside it to confirm the target at a glance. */}
                  {selectedAccount && (
                    <AccountAvatar account={selectedAccount} size={28} />
                  )}
                  <select
                    disabled={actionBusy || cloudRevision === null}
                    aria-label="选择发布账号"
                    value={selectedAccount?.id ?? ""}
                    onChange={(event) =>
                      updateForm({ account: event.target.value })
                    }
                  >
                    <option value="">请选择发布账号</option>
                    {platformAccounts.map((account) => (
                      <option key={account.id} value={account.id}>
                        {account.username} · {account.platform_user_id}
                        {account.status === "invalid" ? "（登录态失效）" : ""}
                      </option>
                    ))}
                  </select>
                </div>
              ) : (
                <div className="content-publish-account-empty">
                  <span>尚未连接该平台账号，请先扫码连接</span>
                  <Button onClick={() => navigate("profile")} variant="quiet">
                    前往用户档案管理账号
                  </Button>
                </div>
              )}
              {selectedAccount?.status === "invalid" && (
                <p role="alert">
                  该账号登录态已失效，请在用户档案中重新扫码后再发布。
                </p>
              )}
              {!platformDeliverable && (
                <p role="status">
                  小红书自动发布即将上线；现在可保存草稿、复制文案后前往官方页面发布。
                </p>
              )}
            </Field>
            <Field label="发布时间">
              <div className="content-publish-schedule">
                <Button
                  disabled={actionBusy || cloudRevision === null}
                  aria-pressed={scheduleMode === "now"}
                  variant={scheduleMode === "now" ? "primary" : "outline"}
                  onClick={() => {
                    setScheduleMode("now");
                    setSaveNotice(false);
                  }}
                >
                  立即
                </Button>
                <Button
                  disabled={actionBusy || cloudRevision === null}
                  aria-pressed={scheduleMode === "later"}
                  variant={scheduleMode === "later" ? "primary" : "outline"}
                  onClick={() => {
                    setScheduleMode("later");
                    setSaveNotice(false);
                  }}
                >
                  定时
                </Button>
                {scheduleMode === "later" && (
                  <input
                    type="datetime-local"
                    aria-label="定时发布时间"
                    disabled={actionBusy || cloudRevision === null}
                    min={localDateTimeInputFromIso(
                      new Date(
                        Date.now() + PUBLISH_SCHEDULE_MIN_LEAD_MS,
                      ).toISOString(),
                    )}
                    value={scheduleInput}
                    onChange={(event) => {
                      setScheduleInput(event.target.value);
                      setSaveNotice(false);
                    }}
                  />
                )}
              </div>
            </Field>
            <Field label="发布标题">
              <input
                disabled={actionBusy || cloudRevision === null}
                onChange={(event) => updateForm({ title: event.target.value })}
                placeholder="填写发布标题"
                value={form.title}
                maxLength={300}
              />
            </Field>
            <Field label="发布描述">
              <textarea
                disabled={actionBusy || cloudRevision === null}
                onChange={(event) =>
                  updateForm({ description: event.target.value })
                }
                placeholder="填写发布说明"
                value={form.description}
                maxLength={5000}
              />
            </Field>
            <Field label="标签">
              <div className="content-publish-tags">
                {form.tags.map((tag) => (
                  <Button
                    key={tag}
                    disabled={actionBusy || cloudRevision === null}
                    aria-label={`移除标签 ${tag}`}
                    onClick={() =>
                      updateForm({
                        tags: form.tags.filter((value) => value !== tag),
                      })
                    }
                  >
                    # {tag} ×
                  </Button>
                ))}
                <input
                  disabled={actionBusy || cloudRevision === null}
                  aria-label="添加标签"
                  onChange={(event) => setTagInput(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    addTag();
                  }}
                  placeholder="输入标签后按 Enter"
                  value={tagInput}
                />
              </div>
            </Field>
          </div>
          {saveNotice && (
            <p className="content-publish-save-notice">
              {review
                ? "已保存审核示例草稿，未同步到云端。"
                : "已保存到云端，可在刷新后继续编辑。"}
            </p>
          )}
          {cloudRevision === null && !cloudError && (
            <p role="status">正在加载云端发布草稿…</p>
          )}
          {cloudError && <p role="alert">{cloudError}</p>}
          {actionError && <p role="alert">{actionError}</p>}
          {!review && (
            <Button
              disabled={actionBusy}
              onClick={() => setReload((value) => value + 1)}
            >
              重新加载云端草稿与本机账号
            </Button>
          )}
          <p>
            点击「立即发布」或「定时发布」后，服务端会用所选账号的登录态自动上传并创建作品；桌面端仍可打开官方页面手动发布。
          </p>
          <div className="content-publish-actions">
            <Button
              disabled={
                !selectedAsset ||
                actionBusy ||
                cloudRevision === null ||
                user.role === "auditor"
              }
              onClick={() => void savePublishDraft()}
              variant="outline"
            >
              保存草稿
            </Button>
            <Button
              disabled={
                !savedDraftForAsset ||
                cloudRevision === null ||
                actionBusy ||
                user.role === "auditor"
              }
              onClick={() => void removeCurrentDraft()}
            >
              移除当前草稿
            </Button>
            <Button
              disabled={!selectedAsset || review || actionBusy}
              onClick={() =>
                selectedAsset &&
                void perform(() =>
                  downloadMaterialAsset(
                    selectedAsset.materialId ??
                      selectedAsset.assetId ??
                      selectedAsset.id,
                    `${form.title || "发布视频"}.mp4`,
                  ),
                )
              }
            >
              下载视频
            </Button>
            <Button
              disabled={selectedCover?.kind !== "image" || review || actionBusy}
              onClick={() =>
                selectedCover &&
                void perform(() =>
                  downloadMaterialAsset(
                    selectedCover.materialId ??
                      selectedCover.assetId ??
                      selectedCover.id,
                    selectedCover.name,
                  ),
                )
              }
            >
              下载封面
            </Button>
            <Button
              disabled={!form.title.trim() || review || actionBusy}
              onClick={() =>
                void perform(() =>
                  navigator.clipboard.writeText(
                    [
                      form.title,
                      form.description,
                      form.tags.map((tag) => `#${tag}`).join(" "),
                    ]
                      .filter(Boolean)
                      .join("\n"),
                  ),
                )
              }
            >
              复制发布文案
            </Button>
            <Button
              disabled={
                !selectedAccount ||
                selectedAccount.status === "invalid" ||
                !selectedAsset ||
                !platformDeliverable ||
                review ||
                actionBusy ||
                cloudRevision === null ||
                user.role === "auditor"
              }
              variant="primary"
              onClick={() => void submitPublish()}
            >
              {scheduleMode === "later" ? "定时发布" : "立即发布"}
            </Button>
            {canUseLocalPublishAccounts() && (
              <Button
                disabled={
                  !selectedLocalProfile ||
                  !selectedAsset ||
                  review ||
                  actionBusy ||
                  user.role === "auditor"
                }
                variant="outline"
                onClick={() =>
                  selectedLocalProfile &&
                  void perform(() =>
                    openLocalPublishAccount(user.id, selectedLocalProfile.id),
                  )
                }
              >
                前往官方发布
              </Button>
            )}
          </div>
        </Panel>
      </section>
    </section>
  );
}
