import { useCallback, useEffect, useRef, useState } from "react";
import {
  type AdminViralSearchTimeRange,
  adminActivationErrorMessage,
  adminSearchViralVideos,
  archiveCollectedViralVideo,
  type CollectedViralStatus,
  type CollectedViralVideo,
  collectViralNow,
  curateViralVideo,
  curateViralVideosBatch,
  fetchViralRuntimeControls,
  listCollectedViralVideos,
  previewCollectedViralVideo,
  refreshCollectedVideoStatistics,
  updateViralRuntimeControls,
  type ViralLibraryOverview,
  type ViralRuntimeControls,
  viralLibraryOverview,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { type BadgeTone, StatusBadge } from "./ui/StatusBadge";

type Action = "feature" | "unfeature" | "delete" | "archive";
type BatchAction = Exclude<Action, "archive">;
/** 「立即采集」不是对某条视频的动作，但共用同一个确认框与提交路径。 */
type PendingAction = Action | "collect";

type Pending = {
  action: PendingAction;
  items: CollectedViralVideo[];
  key: string;
};

type Filters = {
  platform: "" | "douyin" | "wechat_channels";
  status: "" | CollectedViralStatus;
  query: string;
  offset: number;
};

/** 服务端 `curation:batch` 的 items 上限，前端先拦住，避免整批白跑一趟。 */
const MAX_BATCH = 20;

const STAT_FIELDS: Array<
  [string, (video: CollectedViralVideo) => number | null]
> = [
  ["点赞", (video) => video.likes],
  ["评论", (video) => video.comments],
  ["分享", (video) => video.shares],
  ["收藏", (video) => video.collects],
];

// 归档/首页状态的三个小判定被「用户搜索发现」页共用（对同一视频的
// 操作语义必须两处一致），因此导出而非各自复制。
export function archiveBusy(video: CollectedViralVideo) {
  return (
    video.archive_status === "PENDING" || video.archive_status === "RUNNING"
  );
}

export function mediaReady(video: CollectedViralVideo) {
  return video.media_status === "SUCCEEDED" && Boolean(video.storage_uri);
}

export function archiveLabel(video: CollectedViralVideo) {
  if (video.archive_status === "PENDING") return "转存排队中";
  if (video.archive_status === "RUNNING") return "正在转存";
  if (mediaReady(video))
    return video.cover_required && !video.cover_key ? "封面待补齐" : "归档就绪";
  return video.media_status === "FAILED" || video.archive_status === "FAILED"
    ? "转存失败"
    : "待转存";
}

function archiveTone(video: CollectedViralVideo): BadgeTone {
  const label = archiveLabel(video);
  if (label === "归档就绪") return "good";
  if (label === "转存失败") return "danger";
  if (label === "转存排队中" || label === "正在转存") return "info";
  return "warn";
}

/**
 * 链接导入素材的标记（与服务端 `LINK_IMPORT_CATEGORY` 同字面量）。
 *
 * 链接导入只保证媒体本身可复刻：互动字段常常缺失，而且这类素材不进首页内容池
 * ——服务端策展接口会直接 409 拒绝。因此行内如实标注「字段待补全」并禁用展示，
 * 别让运营点了才被服务端教育一次。
 */
const LINK_IMPORT_CATEGORY = "链接导入";

function isLinkImported(video: CollectedViralVideo): boolean {
  return video.category === LINK_IMPORT_CATEGORY;
}

function platformLabel(platform: string) {
  return platform === "douyin" ? "抖音" : "视频号";
}

export function rowKey(video: { platform: string; video_id: string }) {
  return `${video.platform}:${video.video_id}`;
}

function displayDate(value: string | number, full = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(full
      ? ({ hour: "2-digit", minute: "2-digit", second: "2-digit" } as const)
      : {}),
  }).format(date);
}

/** 发布时间的展示口径：优先本地时间戳，其次上游给的展示串，都没有才写「未知」。 */
function publishedLabel(video: CollectedViralVideo) {
  if (video.published_at) return displayDate(video.published_at * 1000, true);
  return video.published_display?.trim() || "未知";
}

function formatDuration(durationMs: number) {
  const total = Math.round(durationMs / 1000);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/**
 * 互动数值的展示口径：拿到的数字照原样显示（0 也是真实值），拿不到时再分
 * 「接口没提供」与「还没补齐」——两者含义不同，不能都写成 0。
 */
function statisticValue(video: CollectedViralVideo, value: number | null) {
  if (typeof value === "number") return value.toLocaleString("zh-CN");
  return video.statistics_checked_at ? "未提供" : "—";
}

/**
 * 列表封面：服务端只下发站内代理地址。缺封面（未归档 / 补齐中）与加载失败
 * 都退回文字，避免一排空图让人以为数据丢了。
 */
function CoverThumb({ video }: { video: CollectedViralVideo }) {
  const [failed, setFailed] = useState(false);
  const cover = video.cover_url;
  return (
    <div className="admin-viral-thumb">
      {cover && !failed ? (
        <img
          alt=""
          loading="lazy"
          src={cover}
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="admin-viral-thumb__empty">
          {video.cover_required ? "封面待补齐" : "无封面"}
        </span>
      )}
      {video.duration_ms > 0 ? (
        <span className="admin-viral-thumb__duration">
          {formatDuration(video.duration_ms)}
        </span>
      ) : null}
    </div>
  );
}

function AuthorBadge({ video }: { video: CollectedViralVideo }) {
  const initial = (video.author || "?").trim().slice(0, 1);
  return (
    <span className="admin-viral-author">
      {video.author_avatar ? (
        <img alt="" loading="lazy" src={video.author_avatar} />
      ) : (
        <i aria-hidden="true">{initial}</i>
      )}
      <b title={video.author}>{video.author || "未知作者"}</b>
      {video.verified ? (
        <span className="admin-viral-verified" title="平台认证">
          ✓
        </span>
      ) : null}
    </span>
  );
}

function Tile({
  label,
  value,
  unit,
}: {
  label: string;
  value?: number | string;
  unit?: string;
}) {
  return (
    <div className="admin-viral-tile">
      <dt>{label}</dt>
      <dd>
        {value === undefined
          ? "—"
          : typeof value === "number"
            ? value.toLocaleString("zh-CN")
            : value}
        {unit ? <small>{unit}</small> : null}
      </dd>
    </div>
  );
}

function SegmentedGroup<T extends string>({
  label,
  value,
  options,
  disabled = false,
  onChange,
}: {
  label: string;
  value: T;
  options: Array<{ value: T; label: string; count?: number }>;
  disabled?: boolean;
  onChange: (next: T) => void;
}) {
  return (
    // biome-ignore lint/a11y/useSemanticElements: 分段切换是一排切换按钮而非表单字段，用 fieldset 会平添默认边框与最小宽度。
    <div aria-label={label} className="admin-viral-seg" role="group">
      {options.map((option) => (
        <button
          aria-pressed={option.value === value}
          className={option.value === value ? "is-active" : undefined}
          disabled={disabled}
          key={option.value || "all"}
          type="button"
          onClick={() => onChange(option.value)}
        >
          {option.label}
          {typeof option.count === "number" ? <em>{option.count}</em> : null}
        </button>
      ))}
    </div>
  );
}

function ViralRow({
  video,
  readOnly,
  saving,
  selected,
  onSelect,
  onDetail,
  onAction,
}: {
  video: CollectedViralVideo;
  readOnly: boolean;
  saving: boolean;
  selected: boolean;
  onSelect: () => void;
  onDetail: () => void;
  onAction: (action: Action) => void;
}) {
  const label = archiveLabel(video);
  const title = video.title || "未命名视频";
  return (
    <tr>
      <td data-label="选择">
        <input
          aria-label={`选择「${title}」`}
          checked={selected}
          type="checkbox"
          onChange={onSelect}
        />
      </td>
      <td data-label="视频信息">
        <div className="admin-viral-videocell">
          <CoverThumb video={video} />
          <div className="admin-viral-videocell__text">
            <div className="admin-viral-chips">
              <span className="admin-viral-platform">
                {platformLabel(video.platform)}
              </span>
              <span className="admin-viral-chip">
                {video.category || "未分类"}
              </span>
              {(video.tags ?? []).slice(0, 3).map((tag) => (
                <span className="admin-viral-chip" key={tag}>
                  #{tag}
                </span>
              ))}
            </div>
            <button
              aria-haspopup="dialog"
              className="admin-viral-titlelink"
              title={title}
              type="button"
              onClick={onDetail}
            >
              {title}
            </button>
            <div className="admin-viral-byline">
              <AuthorBadge video={video} />
            </div>
          </div>
        </div>
      </td>
      <td data-label="互动数据">
        <dl className="admin-viral-metrics">
          {STAT_FIELDS.map(([statLabel, read]) => (
            <div key={statLabel}>
              <dt>{statLabel}</dt>
              <dd>{statisticValue(video, read(video))}</dd>
            </div>
          ))}
        </dl>
        {isLinkImported(video) ? (
          <p className="admin-hint admin-viral-statistics-time">
            字段待补全（链接导入仅保证媒体本身）
          </p>
        ) : video.platform === "wechat_channels" ? (
          <p className="admin-hint admin-viral-statistics-time">
            {video.statistics_retry_at
              ? "获取失败，保留原值"
              : video.statistics_checked_at
                ? `更新于 ${displayDate(video.statistics_checked_at, true)}`
                : "互动待补齐"}
          </p>
        ) : null}
      </td>
      <td data-label="时间">
        <div className="admin-viral-times">
          <span>
            <em>发布</em>
            {publishedLabel(video)}
          </span>
          <span>
            <em>采集</em>
            {displayDate(video.created_at, true)}
          </span>
        </div>
      </td>
      <td data-label="归档 / 首页">
        <div className="admin-viral-status">
          <StatusBadge tone={archiveTone(video)}>{label}</StatusBadge>
          <span
            className={
              video.homepage_featured
                ? "admin-viral-featured"
                : "admin-viral-muted"
            }
          >
            {video.homepage_featured ? "展示中" : "未展示"}
          </span>
        </div>
      </td>
      <td data-label="操作">
        <div className="admin-viral-actions">
          <button type="button" onClick={onDetail}>
            详情
          </button>
          {!readOnly && label !== "归档就绪" ? (
            <button
              disabled={saving || archiveBusy(video)}
              type="button"
              onClick={() => onAction("archive")}
            >
              {archiveBusy(video)
                ? "后台转存中"
                : video.archive_status === "FAILED" ||
                    video.media_status === "FAILED"
                  ? "重试转存"
                  : "转存到云端"}
            </button>
          ) : null}
          {!readOnly ? (
            <button
              disabled={
                saving ||
                (!video.homepage_featured &&
                  (isLinkImported(video) || !mediaReady(video)))
              }
              title={
                !video.homepage_featured && isLinkImported(video)
                  ? "链接导入的素材不进首页；请由后台按关键词采集同主题内容"
                  : undefined
              }
              type="button"
              onClick={() =>
                onAction(video.homepage_featured ? "unfeature" : "feature")
              }
            >
              {video.homepage_featured ? "取消展示" : "展示到首页"}
            </button>
          ) : null}
          {!readOnly ? (
            <button
              className="admin-viral-action-danger"
              disabled={saving}
              type="button"
              onClick={() => onAction("delete")}
            >
              删除
            </button>
          ) : null}
        </div>
      </td>
    </tr>
  );
}

function ViralTable({
  videos,
  ariaLabel,
  selectAllLabel,
  selected,
  readOnly,
  saving,
  onSelect,
  onSelectAll,
  onDetail,
  onAction,
}: {
  videos: CollectedViralVideo[];
  ariaLabel: string;
  selectAllLabel: string;
  selected: string[];
  readOnly: boolean;
  saving: boolean;
  onSelect: (video: CollectedViralVideo) => void;
  onSelectAll: () => void;
  onDetail: (video: CollectedViralVideo) => void;
  onAction: (action: Action, video: CollectedViralVideo) => void;
}) {
  const keys = videos.map(rowKey);
  const allSelected =
    keys.length > 0 && keys.every((key) => selected.includes(key));
  return (
    <section
      aria-label={ariaLabel}
      className="admin-table-scroll admin-viral-table-scroll"
      // biome-ignore lint/a11y/noNoninteractiveTabindex: 键盘用户需要聚焦滚动区域，以方向键查看窄窗口中的完整表格。
      tabIndex={0}
    >
      <table className="admin-data-table admin-viral-table">
        <colgroup>
          <col className="admin-viral-col-select" />
          <col className="admin-viral-col-video" />
          <col className="admin-viral-col-metrics" />
          <col className="admin-viral-col-time" />
          <col className="admin-viral-col-status" />
          <col className="admin-viral-col-actions" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">
              <input
                aria-label={selectAllLabel}
                checked={allSelected}
                type="checkbox"
                onChange={onSelectAll}
              />
            </th>
            <th scope="col">视频信息</th>
            <th scope="col">互动数据</th>
            <th scope="col">时间</th>
            <th scope="col">归档 / 首页</th>
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {videos.map((video) => (
            <ViralRow
              key={rowKey(video)}
              readOnly={readOnly}
              saving={saving}
              selected={selected.includes(rowKey(video))}
              video={video}
              onAction={(action) => onAction(action, video)}
              onDetail={() => onDetail(video)}
              onSelect={() => onSelect(video)}
            />
          ))}
        </tbody>
      </table>
    </section>
  );
}

function pendingCopy(action: PendingAction, count: number) {
  const batch = count > 1;
  if (action === "collect")
    return {
      title: "立即采集爆款视频",
      description:
        "将按已配置的关键词触发一轮采集，采到的视频进入内容池，转存后才能展示到首页。供应商成本记平台账，不扣客户积分。",
    };
  if (action === "archive")
    return {
      title: "转存单条视频",
      description:
        "后台将获取此视频并转存到已配置的云存储，可能产生供应商调用费用。已完成的视频文件会复用；不会重新搜索整个列表或修改首页展示。请填写操作原因。",
    };
  if (action === "delete")
    return batch
      ? {
          title: `批量删除 ${count} 条视频`,
          description:
            "这些视频会从前台移除，后续采集也不会重新展示，已导入项目的素材保留。整批同事务执行，其中任一条已被删除则整批取消。",
        }
      : {
          title: "删除爆款视频",
          description:
            "该视频会从前台移除，后续采集也不会重新展示。已导入项目的素材保留。",
        };
  if (action === "unfeature")
    return batch
      ? {
          title: `批量取消首页展示（${count} 条）`,
          description: "取消后首页不再展示，视频仍保留在爆款列表。",
        }
      : {
          title: "取消首页展示",
          description:
            "取消后首页不再展示，视频仍保留在爆款列表。请填写操作原因。",
        };
  return batch
    ? {
        title: `批量展示 ${count} 条到首页`,
        description:
          "整批同事务执行：其中任一条尚未归档或已下架，整批取消。已归档视频会自动补齐封面。",
      }
    : {
        title: "展示到首页",
        description: "已归档视频会自动补齐封面后展示到首页。请填写操作原因。",
      };
}

function batchNotice(action: BatchAction, count: number) {
  if (action === "feature") return `已批量展示 ${count} 条视频到首页。`;
  if (action === "unfeature") return `已批量取消展示 ${count} 条视频。`;
  return `已删除 ${count} 条视频，前台不再展示。`;
}

export function ViralVideosPage({ readOnly = false }: { readOnly?: boolean }) {
  const [items, setItems] = useState<CollectedViralVideo[]>([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState<Filters>({
    platform: "",
    status: "",
    query: "",
    offset: 0,
  });
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [statisticsProgress, setStatisticsProgress] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [overview, setOverview] = useState<ViralLibraryOverview | null>(null);
  const [controls, setControls] = useState<ViralRuntimeControls | null>(null);
  const [controlsError, setControlsError] = useState("");
  // 详情抽屉只记行的 key，渲染时再从当前列表取最新一行：操作后列表刷新，
  // 抽屉里的状态跟着变，不必另做一份同步逻辑。
  const [detail, setDetail] = useState<{
    key: string;
    video: CollectedViralVideo;
  } | null>(null);
  const detailRef = useRef<HTMLElement | null>(null);
  const [preview, setPreview] = useState<{ key: string; url: string } | null>(
    null,
  );
  const [pending, setPending] = useState<Pending | null>(null);
  // 实时搜索：外呼数据源，命中视频直接并入内容池（不扣客户积分）。
  const [upstreamOpen, setUpstreamOpen] = useState(false);
  const [upstreamKeyword, setUpstreamKeyword] = useState("");
  const [upstreamPlatform, setUpstreamPlatform] = useState<
    "douyin" | "wechat_channels"
  >("douyin");
  const [upstreamTimeRange, setUpstreamTimeRange] =
    useState<AdminViralSearchTimeRange>("week");
  const [upstreamCategory, setUpstreamCategory] = useState("推荐");
  const [upstreamResults, setUpstreamResults] = useState<
    CollectedViralVideo[] | null
  >(null);
  const [upstreamCursor, setUpstreamCursor] = useState("");
  const [upstreamHasMore, setUpstreamHasMore] = useState(false);
  const [upstreamLoading, setUpstreamLoading] = useState(false);
  const [upstreamError, setUpstreamError] = useState("");
  const [upstreamNotice, setUpstreamNotice] = useState("");
  const [keywordBusy, setKeywordBusy] = useState(false);
  const generation = useRef(0);
  const previewGeneration = useRef(0);
  const statisticsKeys = useRef(new Map<string, string>());

  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const result = await listCollectedViralVideos({
        platform: filters.platform || undefined,
        status: filters.status || undefined,
        query: filters.query || undefined,
        offset: filters.offset,
      });
      if (current !== generation.current) return;
      setItems(result.items);
      setTotal(result.total);
    } catch (cause) {
      if (current === generation.current)
        setError(adminActivationErrorMessage(cause, "读取视频库失败"));
    } finally {
      if (current === generation.current) setLoading(false);
    }
  }, [filters]);

  const loadOverview = useCallback(async () => {
    try {
      setOverview(await viralLibraryOverview());
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取爆款视频概览失败"));
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      generation.current += 1;
      previewGeneration.current += 1;
    };
  }, [load]);

  useEffect(() => {
    void loadOverview();
    fetchViralRuntimeControls()
      .then((value) => {
        setControls(value);
        setControlsError("");
      })
      // 关键词只是页面上的提示条：读不到时退回一句说明，不惊动整页错误横幅。
      .catch(() => setControlsError("采集关键词暂时读取失败。"));
  }, [loadOverview]);

  useEffect(() => {
    if (!detail) return;
    detailRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDetail(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [detail]);

  function changeFilters(patch: Partial<Filters>) {
    setFilters((previous) => ({ ...previous, ...patch }));
    // 勾选只对「眼前这一屏」负责：换筛选、翻页后旧勾选不可见，留着会误操作。
    setSelected([]);
  }

  const selectedItems = items.filter((video) =>
    selected.includes(rowKey(video)),
  );
  // 批量展示会被链接导入素材整批拉黑（服务端 409），事先提醒一句，别让运营白填原因。
  const importedSelected = selectedItems.filter(isLinkImported).length;

  /** 视频库与实时搜索结果共用一份勾选，上限与服务端批量接口保持一致。 */
  function toggleKey(key: string) {
    if (selected.includes(key)) {
      setSelected(selected.filter((value) => value !== key));
      return;
    }
    if (selected.length >= MAX_BATCH) {
      setError(`一次最多批量操作 ${MAX_BATCH} 条视频，请分批处理。`);
      return;
    }
    setSelected([...selected, key]);
  }

  function toggleSelectAll(videos: CollectedViralVideo[]) {
    const keys = videos.map(rowKey);
    if (keys.length > 0 && keys.every((key) => selected.includes(key))) {
      setSelected(selected.filter((key) => !keys.includes(key)));
      return;
    }
    const next = [...selected];
    for (const key of keys) {
      if (next.includes(key)) continue;
      if (next.length >= MAX_BATCH) {
        setError(`一次最多批量操作 ${MAX_BATCH} 条视频，请分批处理。`);
        break;
      }
      next.push(key);
    }
    setSelected(next);
  }

  function choose(action: PendingAction, videos: CollectedViralVideo[]) {
    setError("");
    setPending({ action, items: videos, key: crypto.randomUUID() });
  }

  async function showPreview(video: CollectedViralVideo) {
    const current = ++previewGeneration.current;
    setError("");
    try {
      const result = await previewCollectedViralVideo(video);
      if (current === previewGeneration.current)
        setPreview({ key: rowKey(video), url: result.url });
    } catch (cause) {
      if (current === previewGeneration.current)
        setError(adminActivationErrorMessage(cause, "读取预览失败"));
    }
  }

  /** 搜索结果与视频库共用一套操作，成功后同步上游结果里的行状态。 */
  function patchUpstream(
    videos: CollectedViralVideo[],
    patch: Partial<CollectedViralVideo> | null,
  ) {
    const keys = new Set(videos.map(rowKey));
    setUpstreamResults((previous) =>
      previous === null
        ? previous
        : patch === null
          ? previous.filter((item) => !keys.has(rowKey(item)))
          : previous.map((item) =>
              keys.has(rowKey(item)) ? { ...item, ...patch } : item,
            ),
    );
  }

  async function confirm(reason: string) {
    if (!pending) return;
    const { action, items: rows, key } = pending;
    setSaving(true);
    setError("");
    try {
      if (action === "collect") {
        await collectViralNow(reason, key);
        setNotice("已触发立即采集，概览里的数字会随采集进度更新。");
      } else if (action === "archive") {
        await archiveCollectedViralVideo(rows[0], reason, key);
        setNotice(
          "已提交后台转存，可刷新数据查看进度。归档不会自动修改首页展示。",
        );
        patchUpstream(rows, { archive_status: "PENDING" });
      } else if (rows.length > 1) {
        const result = await curateViralVideosBatch(rows, action, reason, key);
        setNotice(batchNotice(action, result.count));
        patchUpstream(
          rows,
          action === "delete"
            ? null
            : { homepage_featured: action === "feature" },
        );
      } else {
        await curateViralVideo(rows[0], action, reason, key);
        setNotice(
          action === "delete"
            ? "视频已删除，前台不再展示。"
            : "首页展示设置已更新。",
        );
        patchUpstream(
          rows,
          action === "delete"
            ? null
            : { homepage_featured: action === "feature" },
        );
      }
      setPending(null);
      if (action === "delete") {
        setDetail(null);
        setPreview(null);
        setSelected([]);
      }
      await load();
      await loadOverview();
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "更新视频失败"));
    } finally {
      setSaving(false);
    }
  }

  async function runUpstreamSearch(cursor?: string) {
    if (readOnly || upstreamLoading) return;
    const keyword = upstreamKeyword.trim();
    if (!keyword) return;
    setUpstreamLoading(true);
    setUpstreamError("");
    setUpstreamNotice("");
    try {
      const result = await adminSearchViralVideos(
        {
          keyword,
          platform: upstreamPlatform,
          time_range: upstreamTimeRange,
          ...(cursor ? { cursor } : {}),
        },
        `实时搜索「${keyword}」`,
        crypto.randomUUID(),
      );
      setUpstreamResults((previous) =>
        cursor ? [...(previous ?? []), ...result.items] : result.items,
      );
      setUpstreamCursor(result.cursor ?? "");
      setUpstreamHasMore(Boolean(result.hasMore && result.cursor));
      // 命中行已并入内容池：列表与概览一起刷新，避免同一视频在两处状态不一致。
      if (!cursor) {
        await load();
        await loadOverview();
      }
    } catch (cause) {
      setUpstreamError(adminActivationErrorMessage(cause, "搜索爆款视频失败"));
    } finally {
      setUpstreamLoading(false);
    }
  }

  /** 把当前搜索词加入定时采集配置，让后续采集周期持续拉取该词。 */
  async function addUpstreamKeywordToCollection() {
    const keyword = upstreamKeyword.trim();
    if (readOnly || keywordBusy || !keyword) return;
    setKeywordBusy(true);
    setUpstreamError("");
    setUpstreamNotice("");
    try {
      const value = await fetchViralRuntimeControls();
      const keywords = value.keywords ?? [];
      if (
        keywords.some(
          (item) =>
            item.platform === upstreamPlatform && item.keyword === keyword,
        )
      ) {
        setUpstreamNotice("该关键词已在采集配置里，无需重复添加。");
        return;
      }
      if (keywords.length >= 20) {
        setUpstreamError(
          "采集关键词已达 20 条上限，请先在系统设置中移除部分关键词。",
        );
        return;
      }
      await updateViralRuntimeControls(
        {
          collection_enabled: value.collection_enabled,
          import_enabled: value.import_enabled,
          keywords: [
            ...keywords,
            {
              platform: upstreamPlatform,
              category: upstreamCategory.trim() || "推荐",
              keyword,
            },
          ],
          per_keyword_limit: value.per_keyword_limit,
          collection_interval_days: value.collection_interval_days,
        },
        `实时搜索后加入采集关键词「${keyword}」`,
      );
      setUpstreamNotice(`已把「${keyword}」加入采集关键词，下个采集周期生效。`);
      setControls(await fetchViralRuntimeControls());
      await loadOverview();
    } catch (cause) {
      setUpstreamError(
        adminActivationErrorMessage(cause, "加入采集关键词失败"),
      );
    } finally {
      setKeywordBusy(false);
    }
  }

  async function refreshStatistics() {
    if (readOnly || saving || loading) return;
    const videos = items.filter(
      (video) => video.platform === "wechat_channels",
    );
    const current = generation.current;
    setSaving(true);
    setError("");
    setNotice("");
    let complete = 0;
    let partial = 0;
    let failed = 0;
    try {
      for (const [index, video] of videos.entries()) {
        if (generation.current !== current) return;
        setStatisticsProgress(`正在核对互动 ${index + 1}/${videos.length}`);
        const key =
          statisticsKeys.current.get(video.video_id) ?? crypto.randomUUID();
        statisticsKeys.current.set(video.video_id, key);
        const result = await refreshCollectedVideoStatistics(video, key);
        statisticsKeys.current.delete(video.video_id);
        if (generation.current !== current) return;
        if (result.statistics_status === "complete") complete += 1;
        else if (result.statistics_status === "partial") partial += 1;
        else failed += 1;
        setItems((previous) =>
          previous.map((item) =>
            item.platform === video.platform && item.video_id === video.video_id
              ? { ...item, ...result }
              : item,
          ),
        );
      }
      setNotice(
        `互动核对完成：完整 ${complete} 条，接口部分提供 ${partial} 条，暂未获取 ${failed} 条。`,
      );
    } catch (cause) {
      setError(
        adminActivationErrorMessage(
          cause,
          "互动补采已暂停，已更新的数据保留。",
        ),
      );
    } finally {
      setStatisticsProgress("");
      setSaving(false);
    }
  }

  const detailVideo = detail
    ? (items.find((video) => rowKey(video) === detail.key) ?? detail.video)
    : null;
  const keywords = controls?.keywords ?? [];
  const collectionEnabled = overview?.collection_enabled;
  const copy = pending
    ? pendingCopy(pending.action, pending.items.length)
    : null;
  const hasWechat = items.some((video) => video.platform === "wechat_channels");

  return (
    <section
      aria-label="爆款视频库"
      className="admin-panel admin-viral-library"
    >
      <header className="admin-viral-heading">
        <div>
          <span className="admin-viral-eyebrow">客户运营</span>
          <h2>爆款视频库</h2>
          <p className="admin-hint">
            关键词定时采集 → 转存云存储 →
            上首页展示；搜索、预览、展示与删除都在本页完成。
          </p>
        </div>
        <div className="admin-viral-headacts">
          <button
            disabled={loading || saving}
            type="button"
            onClick={() => {
              void load();
              void loadOverview();
            }}
          >
            刷新数据
          </button>
          {!readOnly ? (
            <button
              aria-expanded={upstreamOpen}
              type="button"
              onClick={() => setUpstreamOpen((open) => !open)}
            >
              {upstreamOpen ? "收起实时搜索" : "实时搜索入库"}
            </button>
          ) : null}
          {!readOnly ? (
            <button
              disabled={saving || collectionEnabled === false}
              title={
                collectionEnabled === false
                  ? "采集已暂停，请先在系统设置 → 服务配置 → 运行控制中恢复采集。"
                  : undefined
              }
              type="button"
              onClick={() => choose("collect", [])}
            >
              立即采集
            </button>
          ) : null}
        </div>
      </header>
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {statisticsProgress ? (
        <PageBanner tone="notice">{statisticsProgress}</PageBanner>
      ) : null}

      <dl aria-label="爆款视频库概览" className="admin-viral-tiles">
        <Tile label="内容池总量" unit="条" value={overview?.content_total} />
        <Tile label="归档就绪" unit="条" value={overview?.archive_ready} />
        <Tile
          label="首页展示中"
          unit="条"
          value={overview?.homepage_featured}
        />
        <Tile
          label="待转存 / 失败"
          value={
            overview
              ? `${overview.pending_archive ?? 0} / ${overview.archive_failed ?? 0}`
              : undefined
          }
        />
        <Tile label="今日新增" unit="条" value={overview?.added_today} />
        <div className="admin-viral-tile admin-viral-tile--plan">
          <dt>采集计划</dt>
          <dd>
            <span>
              采集
              <StatusBadge tone={collectionEnabled ? "good" : "neutral"}>
                {collectionEnabled === undefined
                  ? "读取中"
                  : collectionEnabled
                    ? "已开启"
                    : "已暂停"}
              </StatusBadge>
            </span>
            <span>
              关键词 {keywords.length} 条（抖音{" "}
              {overview?.keyword_count?.douyin ?? 0} / 视频号{" "}
              {overview?.keyword_count?.wechat_channels ?? 0}）
            </span>
            <span>
              {overview?.collection_interval_days === 1 ? "每天" : "每周"}
              采集一次 · 下一次{" "}
              {overview?.next_collection_at
                ? displayDate(overview.next_collection_at, true)
                : "等待后台调度"}
            </span>
            <span>
              最后采集{" "}
              {overview?.last_fetched_at
                ? displayDate(overview.last_fetched_at, true)
                : "暂无"}
            </span>
          </dd>
        </div>
      </dl>

      <section aria-label="采集关键词" className="admin-viral-keywords">
        <span className="admin-viral-keywords__label">采集关键词</span>
        {controlsError ? (
          <span className="admin-viral-muted">{controlsError}</span>
        ) : keywords.length === 0 ? (
          <span className="admin-viral-muted">
            尚未配置关键词，未配置时后台不会采集。
          </span>
        ) : (
          keywords.map((item) => (
            <span
              className="admin-viral-kw"
              key={`${item.platform}:${item.category}:${item.keyword}`}
            >
              {item.keyword}
              <i>
                {platformLabel(item.platform)} · {item.category || "未分类"}
              </i>
            </span>
          ))
        )}
        <span className="admin-viral-keywords__hint">
          关键词在「系统设置 → 服务配置 →
          运行控制」维护，修改后下个采集周期生效。
        </span>
      </section>

      {!readOnly && upstreamOpen ? (
        <section aria-label="实时搜索上游" className="admin-viral-upstream">
          <h3>实时搜索入库</h3>
          <p className="admin-hint">
            外呼数据源按关键词检索，结果直接并入下方视频库；供应商成本记平台账，不扣客户积分。搜索后可转存到云端再展示到首页。
          </p>
          <form
            className="admin-toolbar admin-viral-toolbar"
            onSubmit={(event) => {
              event.preventDefault();
              void runUpstreamSearch();
            }}
          >
            <label>
              平台
              <select
                disabled={upstreamLoading}
                value={upstreamPlatform}
                onChange={(event) =>
                  setUpstreamPlatform(
                    event.target.value as "douyin" | "wechat_channels",
                  )
                }
              >
                <option value="douyin">抖音</option>
                <option value="wechat_channels">视频号</option>
              </select>
            </label>
            <label className="admin-viral-search">
              关键词
              <input
                disabled={upstreamLoading}
                maxLength={100}
                placeholder="输入要搜索的关键词"
                value={upstreamKeyword}
                onChange={(event) => setUpstreamKeyword(event.target.value)}
              />
            </label>
            <label>
              时间范围
              <select
                disabled={upstreamLoading}
                value={upstreamTimeRange}
                onChange={(event) =>
                  setUpstreamTimeRange(
                    event.target.value as AdminViralSearchTimeRange,
                  )
                }
              >
                <option value="day">最近 1 天</option>
                <option value="week">最近 7 天</option>
                <option value="half_year">最近半年</option>
                <option value="all">不限时间</option>
              </select>
            </label>
            <button
              disabled={upstreamLoading || !upstreamKeyword.trim()}
              type="submit"
            >
              {upstreamLoading ? "搜索中…" : "搜索"}
            </button>
          </form>
          {upstreamError ? (
            <PageBanner tone="error">{upstreamError}</PageBanner>
          ) : null}
          {upstreamNotice ? (
            <PageBanner tone="notice">{upstreamNotice}</PageBanner>
          ) : null}
          {upstreamLoading ? (
            <p role="status">正在搜索（需外呼数据源，请稍候）…</p>
          ) : null}
          {upstreamResults !== null && !upstreamLoading ? (
            <>
              {upstreamResults.length === 0 ? (
                <p>未搜索到相关视频，可换个关键词或放宽时间范围。</p>
              ) : (
                <ViralTable
                  ariaLabel="实时搜索结果"
                  readOnly={readOnly}
                  saving={saving}
                  selectAllLabel="选择搜索结果全部"
                  selected={selected}
                  videos={upstreamResults}
                  onAction={(action, video) => choose(action, [video])}
                  onDetail={(video) => setDetail({ key: rowKey(video), video })}
                  onSelect={(video) => toggleKey(rowKey(video))}
                  onSelectAll={() => toggleSelectAll(upstreamResults)}
                />
              )}
              {upstreamHasMore ? (
                <button
                  disabled={upstreamLoading}
                  type="button"
                  onClick={() => void runUpstreamSearch(upstreamCursor)}
                >
                  下一页（继续外呼数据源）
                </button>
              ) : null}
              {upstreamKeyword.trim() ? (
                <div className="admin-toolbar admin-viral-toolbar">
                  <label className="admin-viral-search">
                    采集分类
                    <input
                      disabled={keywordBusy}
                      maxLength={32}
                      placeholder="推荐"
                      value={upstreamCategory}
                      onChange={(event) =>
                        setUpstreamCategory(event.target.value)
                      }
                    />
                  </label>
                  <button
                    disabled={keywordBusy}
                    type="button"
                    onClick={() => void addUpstreamKeywordToCollection()}
                  >
                    将「{upstreamKeyword.trim()}」加入采集关键词
                  </button>
                </div>
              ) : null}
            </>
          ) : null}
        </section>
      ) : null}

      <div className="admin-viral-filters">
        <SegmentedGroup
          disabled={saving}
          label="平台筛选"
          options={[
            { value: "", label: "全部平台" },
            { value: "douyin", label: "抖音" },
            { value: "wechat_channels", label: "视频号" },
          ]}
          value={filters.platform}
          onChange={(value) => changeFilters({ platform: value, offset: 0 })}
        />
        <SegmentedGroup
          disabled={saving}
          label="状态筛选"
          options={[
            { value: "", label: "全部状态", count: overview?.content_total },
            {
              value: "ready",
              label: "归档就绪",
              count: overview?.archive_ready,
            },
            {
              value: "pending",
              label: "待转存",
              count: overview?.pending_archive,
            },
            {
              value: "failed",
              label: "转存失败",
              count: overview?.archive_failed,
            },
            {
              value: "featured",
              label: "已展示",
              count: overview?.homepage_featured,
            },
          ]}
          value={filters.status}
          onChange={(value) => changeFilters({ status: value, offset: 0 })}
        />
        <form
          className="admin-viral-searchbar"
          onSubmit={(event) => {
            event.preventDefault();
            changeFilters({ query: search.trim(), offset: 0 });
          }}
        >
          <label className="admin-viral-search">
            搜索视频
            <input
              disabled={saving}
              maxLength={100}
              placeholder="标题、作者或视频 ID"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <button disabled={saving} type="submit">
            搜索
          </button>
          {!readOnly && hasWechat ? (
            <button
              disabled={saving || loading}
              type="button"
              onClick={() => void refreshStatistics()}
            >
              补齐本页互动
            </button>
          ) : null}
        </form>
      </div>
      {hasWechat ? (
        <p className="admin-hint">
          视频号互动按需获取，24 小时内复用已获取数据；失败后冷却 10
          分钟。补采计入平台接口用量，不扣客户积分。
        </p>
      ) : null}

      {selectedItems.length > 0 ? (
        <section aria-label="批量操作" className="admin-viral-batch">
          <span>
            已选 <strong>{selectedItems.length}</strong> 项
          </span>
          <button
            disabled={saving}
            type="button"
            onClick={() => choose("feature", selectedItems)}
          >
            批量展示到首页
          </button>
          <button
            disabled={saving}
            type="button"
            onClick={() => choose("unfeature", selectedItems)}
          >
            批量取消展示
          </button>
          <button
            className="admin-viral-action-danger"
            disabled={saving}
            type="button"
            onClick={() => choose("delete", selectedItems)}
          >
            批量删除
          </button>
          <button
            disabled={saving}
            type="button"
            onClick={() => setSelected([])}
          >
            取消选择
          </button>
          <span className="admin-hint">
            批量操作整批同事务执行，任一条不满足条件则整批取消；删除后前台立即下架，已导入项目的素材保留。
          </span>
          {importedSelected > 0 ? (
            <span className="admin-hint">
              所选含 {importedSelected}{" "}
              条链接导入素材：它们不进首页，批量展示到首页会被整批拒绝，可先取消勾选。
            </span>
          ) : null}
        </section>
      ) : null}

      {loading ? (
        <p role="status">正在读取采集记录…</p>
      ) : items.length === 0 ? (
        <p>暂无符合条件的采集视频。</p>
      ) : (
        <ViralTable
          ariaLabel="爆款视频明细，可横向滚动"
          readOnly={readOnly}
          saving={saving}
          selectAllLabel="选择本页全部视频"
          selected={selected}
          videos={items}
          onAction={(action, video) => choose(action, [video])}
          onDetail={(video) => setDetail({ key: rowKey(video), video })}
          onSelect={(video) => toggleKey(rowKey(video))}
          onSelectAll={() => toggleSelectAll(items)}
        />
      )}
      <Pagination
        disabled={loading || saving}
        limit={25}
        offset={filters.offset}
        total={total}
        onPageChange={(offset) => changeFilters({ offset })}
      />

      {detailVideo ? (
        <div className="admin-viral-drawer-layer">
          <button
            aria-label="关闭视频详情"
            className="admin-viral-drawer__scrim"
            type="button"
            onClick={() => {
              setDetail(null);
              setPreview(null);
            }}
          />
          <aside
            aria-label="视频详情"
            aria-modal="true"
            className="admin-viral-drawer"
            ref={detailRef}
            role="dialog"
            tabIndex={-1}
          >
            <header className="admin-viral-drawer__head">
              <b>视频详情</b>
              <span className="admin-viral-muted">
                {platformLabel(detailVideo.platform)} ·{" "}
                {detailVideo.category || "未分类"}
              </span>
              <button
                type="button"
                onClick={() => {
                  setDetail(null);
                  setPreview(null);
                }}
              >
                关闭
              </button>
            </header>
            <div className="admin-viral-drawer__body">
              <div className="admin-viral-player">
                {preview && preview.key === rowKey(detailVideo) ? (
                  <video controls muted src={preview.url} />
                ) : detailVideo.cover_url ? (
                  <img alt="" src={detailVideo.cover_url} />
                ) : null}
                <div className="admin-viral-player__tags">
                  <StatusBadge tone={archiveTone(detailVideo)}>
                    {archiveLabel(detailVideo)}
                  </StatusBadge>
                  <span className="admin-viral-muted">
                    {detailVideo.duration_ms > 0
                      ? formatDuration(detailVideo.duration_ms)
                      : "时长未知"}
                  </span>
                </div>
              </div>
              <h3 className="admin-viral-drawer__title">
                {detailVideo.title || "未命名视频"}
              </h3>
              <AuthorBadge video={detailVideo} />
              <dl className="admin-viral-drawer__stats">
                {STAT_FIELDS.map(([statLabel, read]) => (
                  <div key={statLabel}>
                    <dt>{statLabel}</dt>
                    <dd>{statisticValue(detailVideo, read(detailVideo))}</dd>
                  </div>
                ))}
              </dl>
              {isLinkImported(detailVideo) ? (
                <p className="admin-hint">
                  字段待补全：链接导入只保证媒体本身，头像 / 封面 /
                  互动数据以解析上游 实际返回为准，缺的部分不会用默认值顶替。
                </p>
              ) : null}
              <h4 className="admin-viral-drawer__section">基础信息</h4>
              <dl className="admin-viral-drawer__meta">
                <div>
                  <dt>视频 ID</dt>
                  <dd>{detailVideo.video_id}</dd>
                </div>
                <div>
                  <dt>发布时间</dt>
                  <dd>{publishedLabel(detailVideo)}</dd>
                </div>
                <div>
                  <dt>采集时间</dt>
                  <dd>{displayDate(detailVideo.created_at, true)}</dd>
                </div>
                <div>
                  <dt>作者 / 分类</dt>
                  <dd>
                    {detailVideo.author || "未知作者"} /{" "}
                    {detailVideo.category || "未分类"}
                  </dd>
                </div>
                <div>
                  <dt>话题标签</dt>
                  <dd>
                    {(detailVideo.tags ?? []).length > 0
                      ? (detailVideo.tags ?? [])
                          .map((tag) => `#${tag}`)
                          .join(" ")
                      : "未提供"}
                  </dd>
                </div>
                <div>
                  <dt>互动刷新</dt>
                  <dd>
                    {detailVideo.statistics_retry_at
                      ? "上次获取失败，保留原值"
                      : detailVideo.statistics_checked_at
                        ? displayDate(detailVideo.statistics_checked_at, true)
                        : "尚未补齐"}
                  </dd>
                </div>
              </dl>
              <h4 className="admin-viral-drawer__section">云端素材</h4>
              <dl className="admin-viral-drawer__meta">
                <div>
                  <dt>归档状态</dt>
                  <dd>
                    {archiveLabel(detailVideo)}
                    {detailVideo.archive_error
                      ? `：${detailVideo.archive_error}`
                      : ""}
                  </dd>
                </div>
                <div>
                  <dt>前端展示</dt>
                  <dd>
                    {detailVideo.homepage_featured ? "首页展示中" : "未展示"}
                  </dd>
                </div>
                <div className="admin-viral-drawer__storage">
                  <dt>云存储地址</dt>
                  <dd>{detailVideo.storage_uri ?? "尚未生成"}</dd>
                </div>
              </dl>
            </div>
            <footer className="admin-viral-drawer__foot">
              {preview && preview.key === rowKey(detailVideo) ? (
                <button type="button" onClick={() => setPreview(null)}>
                  关闭预览
                </button>
              ) : (
                <button
                  disabled={!mediaReady(detailVideo)}
                  type="button"
                  onClick={() => void showPreview(detailVideo)}
                >
                  预览视频
                </button>
              )}
              {!readOnly && archiveLabel(detailVideo) !== "归档就绪" ? (
                <button
                  disabled={saving || archiveBusy(detailVideo)}
                  type="button"
                  onClick={() => choose("archive", [detailVideo])}
                >
                  {archiveBusy(detailVideo)
                    ? "后台转存中"
                    : detailVideo.archive_status === "FAILED" ||
                        detailVideo.media_status === "FAILED"
                      ? "重试转存"
                      : "转存到云端"}
                </button>
              ) : null}
              {!readOnly ? (
                <button
                  className="admin-viral-action-primary"
                  disabled={
                    saving ||
                    (!detailVideo.homepage_featured &&
                      (isLinkImported(detailVideo) || !mediaReady(detailVideo)))
                  }
                  title={
                    !detailVideo.homepage_featured &&
                    isLinkImported(detailVideo)
                      ? "链接导入的素材不进首页；请由后台按关键词采集同主题内容"
                      : undefined
                  }
                  type="button"
                  onClick={() =>
                    choose(
                      detailVideo.homepage_featured ? "unfeature" : "feature",
                      [detailVideo],
                    )
                  }
                >
                  {detailVideo.homepage_featured ? "取消展示" : "展示到首页"}
                </button>
              ) : null}
              {!readOnly ? (
                <button
                  className="admin-viral-action-danger"
                  disabled={saving}
                  type="button"
                  onClick={() => choose("delete", [detailVideo])}
                >
                  删除
                </button>
              ) : null}
            </footer>
          </aside>
        </div>
      ) : null}

      <ConfirmDialog
        busy={saving}
        confirmLabel="确认操作"
        description={copy?.description}
        error={error}
        level="reason"
        open={pending !== null}
        title={copy?.title ?? ""}
        onClose={() => setPending(null)}
        onConfirm={(reason) => void confirm(reason)}
      />
    </section>
  );
}
