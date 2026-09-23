import {
  Fragment,
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import {
  adminActivationErrorMessage,
  type CollectedViralVideo,
  curateViralVideo,
  listCollectedViralVideos,
  previewCollectedViralVideo,
  refreshCollectedVideoStatistics,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";

type Action = "feature" | "unfeature" | "delete";

function coverLabel(video: CollectedViralVideo) {
  return video.cover_key ? "封面已就绪" : "封面待补齐";
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

export function ViralVideosPage({ readOnly = false }: { readOnly?: boolean }) {
  const [items, setItems] = useState<CollectedViralVideo[]>([]);
  const [total, setTotal] = useState(0);
  const [filters, setFilters] = useState({
    platform: "",
    query: "",
    offset: 0,
  });
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [statisticsProgress, setStatisticsProgress] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const detailPrefix = useId();
  const [preview, setPreview] = useState<{ title: string; url: string } | null>(
    null,
  );
  const [pending, setPending] = useState<{
    video: CollectedViralVideo;
    action: Action;
    key: string;
  } | null>(null);
  const generation = useRef(0);
  const previewGeneration = useRef(0);
  const statisticsKeys = useRef(new Map<string, string>());
  const load = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true);
    setError("");
    try {
      const result = await listCollectedViralVideos(filters);
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
  useEffect(() => {
    void load();
    return () => {
      generation.current += 1;
      previewGeneration.current += 1;
    };
  }, [load]);

  async function showPreview(video: CollectedViralVideo) {
    const current = ++previewGeneration.current;
    setError("");
    try {
      const result = await previewCollectedViralVideo(video);
      if (current === previewGeneration.current)
        setPreview({ title: video.title, url: result.url });
    } catch (cause) {
      if (current === previewGeneration.current)
        setError(adminActivationErrorMessage(cause, "读取预览失败"));
    }
  }
  async function confirm(reason: string) {
    if (!pending) return;
    setSaving(true);
    setError("");
    try {
      await curateViralVideo(
        pending.video,
        pending.action,
        reason,
        pending.key,
      );
      setNotice(
        pending.action === "delete"
          ? "视频已删除，前台不再展示。"
          : "首页展示设置已更新。",
      );
      setPending(null);
      setPreview(null);
      previewGeneration.current += 1;
      await load();
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "更新视频失败"));
    } finally {
      setSaving(false);
    }
  }
  function choose(video: CollectedViralVideo, action: Action) {
    setError("");
    setPending({ video, action, key: crypto.randomUUID() });
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

  return (
    <section
      className="admin-panel admin-viral-library"
      aria-label="爆款视频库"
    >
      <header className="admin-viral-heading">
        <div>
          <h2>
            内容池 <span>{total.toLocaleString("zh-CN")}</span>
          </h2>
          <p className="admin-hint">
            客户搜索到的内容会持续沉淀在这里，可人工筛选首页精选。
          </p>
        </div>
        <section className="admin-viral-summary" aria-label="当前页概况">
          <span>
            本页 <strong>{items.length}</strong>
          </span>
          <span>
            稳定封面 <strong>{items.filter((v) => v.cover_key).length}</strong>
          </span>
          <span>
            首页展示{" "}
            <strong>{items.filter((v) => v.homepage_featured).length}</strong>
          </span>
        </section>
      </header>
      <p className="admin-hint">
        精选只要求稳定封面，不要求服务器保存视频文件。视频文件仅保留旧数据只读预览。
      </p>
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <PageBanner tone="notice">{notice}</PageBanner>}
      {statisticsProgress && (
        <PageBanner tone="notice">{statisticsProgress}</PageBanner>
      )}
      <form
        className="admin-toolbar admin-viral-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setFilters({ ...filters, query: search.trim(), offset: 0 });
        }}
      >
        <label>
          平台筛选
          <select
            disabled={saving}
            value={filters.platform}
            onChange={(event) =>
              setFilters({
                ...filters,
                platform: event.target.value,
                offset: 0,
              })
            }
          >
            <option value="">全部平台</option>
            <option value="douyin">抖音</option>
            <option value="wechat_channels">视频号</option>
          </select>
        </label>
        <label className="admin-viral-search">
          搜索视频
          <input
            disabled={saving}
            value={search}
            maxLength={100}
            placeholder="标题、作者或视频 ID"
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <button type="submit" disabled={saving}>
          搜索
        </button>
        <button
          type="button"
          disabled={loading || saving}
          onClick={() => void load()}
        >
          刷新数据
        </button>
        {!readOnly && items.some((v) => v.platform === "wechat_channels") && (
          <button
            type="button"
            disabled={saving || loading}
            onClick={() => void refreshStatistics()}
          >
            补齐本页互动
          </button>
        )}
      </form>
      {items.some((v) => v.platform === "wechat_channels") && (
        <p className="admin-hint">
          视频号互动按需获取，24 小时内复用已获取数据；失败后冷却 10
          分钟。补采计入平台接口用量，不扣客户积分。
        </p>
      )}
      {preview && (
        <section className="admin-viral-preview" aria-label="云端视频预览">
          <h3>{preview.title}</h3>
          <video
            controls
            muted
            src={preview.url}
            style={{ width: "100%", maxHeight: 480 }}
          />
          <button
            type="button"
            onClick={() => {
              setPreview(null);
              previewGeneration.current += 1;
            }}
          >
            关闭预览
          </button>
        </section>
      )}
      {loading ? (
        <p role="status">正在读取内容池…</p>
      ) : items.length === 0 ? (
        <p>暂无符合条件的内容。</p>
      ) : (
        <section
          className="admin-table-scroll admin-viral-table-scroll"
          // biome-ignore lint/a11y/noNoninteractiveTabindex: 键盘用户需要聚焦滚动区域，以方向键查看窄窗口中的完整表格。
          tabIndex={0}
          aria-label="爆款视频明细，可横向滚动"
        >
          <table className="admin-data-table admin-viral-table">
            <colgroup>
              <col className="admin-viral-col-video" />
              <col className="admin-viral-col-metrics" />
              <col className="admin-viral-col-status" />
              <col className="admin-viral-col-actions" />
            </colgroup>
            <thead>
              <tr>
                <th scope="col">视频信息</th>
                <th scope="col">互动数据</th>
                <th scope="col">封面 / 精选</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {items.map((video) => {
                const key = `${video.platform}:${video.video_id}`;
                const open = expanded === key;
                const detailId = `${detailPrefix}-${encodeURIComponent(key)}`;
                return (
                  <Fragment key={key}>
                    <tr className={open ? "is-expanded" : undefined}>
                      <td>
                        <div className="admin-viral-meta">
                          <span className="admin-viral-platform">
                            {video.platform === "douyin" ? "抖音" : "视频号"}
                          </span>
                          <span>{video.category || "未分类"}</span>
                          <span>
                            {video.duration_ms > 0
                              ? `${(video.duration_ms / 1000).toFixed(1)} 秒`
                              : "时长未知"}
                          </span>
                        </div>
                        <strong
                          className="admin-viral-title"
                          title={video.title}
                        >
                          {video.title || "未命名视频"}
                        </strong>
                        <div className="admin-viral-byline">
                          <span title={video.author}>
                            {video.author || "未知作者"}
                          </span>
                          <time dateTime={video.created_at}>
                            {displayDate(video.created_at)} 入池
                          </time>
                        </div>
                      </td>
                      <td>
                        <dl className="admin-viral-metrics">
                          {[
                            ["点赞", video.likes],
                            ["评论", video.comments],
                            ["分享", video.shares],
                            ["收藏", video.collects],
                          ].map(([label, value]) => (
                            <div key={label}>
                              <dt>{label}</dt>
                              <dd>
                                {typeof value === "number"
                                  ? value.toLocaleString("zh-CN")
                                  : video.statistics_checked_at
                                    ? "未提供"
                                    : "—"}
                              </dd>
                            </div>
                          ))}
                        </dl>
                        {video.platform === "wechat_channels" && (
                          <p className="admin-hint admin-viral-statistics-time">
                            {video.statistics_retry_at
                              ? "获取失败，保留原值"
                              : video.statistics_checked_at
                                ? `更新于 ${displayDate(video.statistics_checked_at, true)}`
                                : "互动待补齐"}
                          </p>
                        )}
                      </td>
                      <td>
                        <div className="admin-viral-status">
                          <StatusBadge tone={video.cover_key ? "good" : "warn"}>
                            {coverLabel(video)}
                          </StatusBadge>
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
                      <td>
                        <div className="admin-viral-actions">
                          <button
                            type="button"
                            disabled={
                              saving ||
                              video.media_status !== "SUCCEEDED" ||
                              !video.storage_uri
                            }
                            onClick={() => void showPreview(video)}
                          >
                            预览
                          </button>
                          <button
                            type="button"
                            aria-expanded={open}
                            aria-controls={open ? detailId : undefined}
                            onClick={() => setExpanded(open ? null : key)}
                          >
                            {open ? "收起详情" : "查看详情"}
                          </button>
                          {!readOnly && (
                            <button
                              type="button"
                              disabled={
                                saving ||
                                (!video.homepage_featured &&
                                  !video.cover_key &&
                                  !video.cover_required)
                              }
                              onClick={() =>
                                choose(
                                  video,
                                  video.homepage_featured
                                    ? "unfeature"
                                    : "feature",
                                )
                              }
                            >
                              {video.homepage_featured
                                ? "取消首页展示"
                                : "展示到首页"}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                    {open && (
                      <tr className="admin-detail-row">
                        <td colSpan={4}>
                          <section
                            id={detailId}
                            className="admin-viral-detail"
                            aria-label="视频完整详情"
                          >
                            <p className="admin-viral-full-title">
                              {video.title || "未命名视频"}
                            </p>
                            <dl>
                              <div>
                                <dt>视频 ID</dt>
                                <dd>{video.video_id}</dd>
                              </div>
                              <div>
                                <dt>作者 / 分类</dt>
                                <dd>
                                  {video.author || "未知作者"} /{" "}
                                  {video.category || "未分类"}
                                </dd>
                              </div>
                              <div>
                                <dt>发布时间（北京时间）</dt>
                                <dd>
                                  {video.published_at
                                    ? displayDate(
                                        video.published_at * 1000,
                                        true,
                                      )
                                    : "未知"}
                                </dd>
                              </div>
                              <div>
                                <dt>入池时间（北京时间）</dt>
                                <dd>{displayDate(video.created_at, true)}</dd>
                              </div>
                              <div className="admin-viral-storage">
                                <dt>存量云视频（只读）</dt>
                                <dd>
                                  {video.storage_uri ??
                                    "无（客户端从源站缓存）"}
                                </dd>
                              </div>
                              {video.archive_error && (
                                <div>
                                  <dt>转存说明</dt>
                                  <dd>{video.archive_error}</dd>
                                </div>
                              )}
                            </dl>
                            {!readOnly && (
                              <div className="admin-viral-danger-zone">
                                <span>
                                  删除后前台不可用，已导入项目的素材保留。
                                </span>
                                <button
                                  type="button"
                                  disabled={saving}
                                  onClick={() => choose(video, "delete")}
                                >
                                  删除
                                </button>
                              </div>
                            )}
                          </section>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </section>
      )}
      <Pagination
        offset={filters.offset}
        limit={25}
        total={total}
        disabled={loading || saving}
        onPageChange={(offset) => setFilters({ ...filters, offset })}
      />
      <ConfirmDialog
        open={pending !== null}
        busy={saving}
        error={error}
        level="reason"
        title={pending?.action === "delete" ? "删除爆款视频" : "更新首页展示"}
        description={
          pending?.action === "delete"
            ? "该视频会从前台移除，后续搜索更新也不会重新展示。已导入项目的素材保留。"
            : pending?.action === "unfeature"
              ? "取消后首页不再展示，视频仍保留在爆款列表。请填写操作原因。"
              : "内容会在稳定封面就绪后展示到首页，不需要云端视频。请填写操作原因。"
        }
        confirmLabel="确认操作"
        onClose={() => setPending(null)}
        onConfirm={(reason) => void confirm(reason)}
      />
    </section>
  );
}
