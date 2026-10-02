import { useCallback, useEffect, useRef, useState } from "react";
import {
  addViralKeywords,
  adminActivationErrorMessage,
  archiveCollectedViralVideo,
  type CollectedViralVideo,
  curateViralVideo,
  estimateViralOperation,
  listViralDiscoveries,
  listViralDiscoveryDetails,
  type ViralDiscoveryAggregate,
  type ViralDiscoveryDetail,
  type ViralDiscoverySummary,
  type ViralOperationEstimate,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { ViralDemandCustomers } from "./ViralDemandCustomers";
import { ViralOperationCostSummary } from "./ViralOperationCostSummary";
import {
  archiveBusy,
  archiveLabel,
  contentStateLabel,
  contentStateOf,
} from "./ViralVideosPage";

type Action = "archive" | "feature" | "unfeature";

/** 与服务端一致按上海日历归属「今天」（en-CA 本地化即 YYYY-MM-DD）。 */
function todayShanghai(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
  }).format(new Date());
}

function displayTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function platformLabel(platform: string) {
  return platform === "douyin" ? "抖音" : "视频号";
}

const DETAIL_PAGE_SIZE = 20;

function SearchTrend({
  days,
}: {
  days: NonNullable<ViralDiscoverySummary["trend"]>;
}) {
  const max = Math.max(1, ...days.map((day) => day.searches ?? 0));
  return (
    <div
      role="img"
      aria-label="每日已记录搜索趋势，空白表示历史未知"
      style={{
        display: "flex",
        alignItems: "end",
        height: 36,
        gap: 2,
        minWidth: 110,
      }}
    >
      {days.map((day) => (
        <span
          key={day.date}
          title={`${day.date}：${day.searches == null ? "历史未知" : `${day.searches}页次${day.partial ? "，未覆盖全天" : ""}`}`}
          style={{
            flex: 1,
            height:
              day.searches == null ? 0 : Math.max(2, (day.searches / max) * 36),
            background: "var(--admin-primary, #32665b)",
            borderRadius: "2px 2px 0 0",
          }}
        />
      ))}
    </div>
  );
}

export function ViralDiscoveriesPage({
  readOnly = false,
  onSearch,
  onCustomer,
}: {
  readOnly?: boolean;
  onSearch?: (keyword: string, platform: "douyin" | "wechat_channels") => void;
  onCustomer?: (userId: string) => void;
}) {
  const [date, setDate] = useState(todayShanghai);
  // 方案 P1 客户需求洞察：单日之外支持近 7/30 天与自定义区间。
  const [rangePreset, setRangePreset] = useState<
    "day" | "7d" | "30d" | "custom"
  >("7d");
  const [customerTarget, setCustomerTarget] = useState<{
    from: string;
    to: string;
    keyword: string;
    platform: string;
  } | null>(null);
  const [rangeFrom, setRangeFrom] = useState(todayShanghai());
  const [rangeTo, setRangeTo] = useState(todayShanghai());
  const [aggregate, setAggregate] = useState<ViralDiscoveryAggregate | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  const [estimating, setEstimating] = useState(false);
  const [keywordPending, setKeywordPending] = useState<{
    row: ViralDiscoverySummary;
    key: string;
  } | null>(null);
  const [keywordCategory, setKeywordCategory] = useState("");
  const aggregateGeneration = useRef(0);
  const detailGeneration = useRef(0);
  // 下钻明细：keyword+platform 定位一个分组；offset 供翻页。
  const [detail, setDetail] = useState<{
    keyword: string;
    platform: string;
    items: ViralDiscoveryDetail[];
    total: number;
    offset: number;
    window: { date: string; from?: string; to?: string };
  } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pending, setPending] = useState<{
    video: CollectedViralVideo;
    action: Action;
    key: string;
    estimate: ViralOperationEstimate | null;
  } | null>(null);

  const loadAggregate = useCallback(
    async (targetDate: string, preset: typeof rangePreset = "day") => {
      const generation = ++aggregateGeneration.current;
      detailGeneration.current += 1;
      setDetail(null);
      setCustomerTarget(null);
      setDetailLoading(false);
      setLoading(true);
      setError("");
      try {
        const shiftDays = preset === "7d" ? 7 : preset === "30d" ? 30 : 0;
        const range =
          preset === "custom"
            ? { from: rangeFrom, to: rangeTo }
            : preset === "day"
              ? undefined
              : {
                  from: new Intl.DateTimeFormat("en-CA", {
                    timeZone: "Asia/Shanghai",
                  }).format(
                    new Date(
                      new Date(`${targetDate}T12:00:00+08:00`).getTime() -
                        (shiftDays - 1) * 86_400_000,
                    ),
                  ),
                  to: targetDate,
                };
        const result = await listViralDiscoveries(targetDate, range);
        if (generation === aggregateGeneration.current) setAggregate(result);
      } catch (cause) {
        if (generation === aggregateGeneration.current)
          setError(adminActivationErrorMessage(cause, "读取搜索发现失败"));
      } finally {
        if (generation === aggregateGeneration.current) setLoading(false);
      }
    },
    // 自定义区间输入变化时自动重查：与单日 date 输入的既有行为一致。
    [rangeFrom, rangeTo],
  );
  useEffect(() => {
    setDetail(null);
    void loadAggregate(date, rangePreset);
    return () => {
      aggregateGeneration.current += 1;
      detailGeneration.current += 1;
    };
  }, [date, rangePreset, loadAggregate]);

  const loadDetail = useCallback(
    async (
      keyword: string,
      platform: string,
      offset: number,
      window: { date: string; from?: string; to?: string } = {
        date,
        from: aggregate?.from,
        to: aggregate?.to,
      },
    ) => {
      const generation = ++detailGeneration.current;
      setDetailLoading(true);
      setError("");
      try {
        const result = await listViralDiscoveryDetails({
          ...window,
          keyword,
          platform,
          offset,
          limit: DETAIL_PAGE_SIZE,
        });
        if (generation !== detailGeneration.current) return;
        setDetail({
          keyword,
          platform,
          items: result.items,
          total: result.total,
          offset,
          window,
        });
      } catch (cause) {
        if (generation === detailGeneration.current)
          setError(adminActivationErrorMessage(cause, "读取搜索发现明细失败"));
      } finally {
        if (generation === detailGeneration.current) setDetailLoading(false);
      }
    },
    [date, aggregate],
  );

  async function requestAction(video: CollectedViralVideo, action: Action) {
    if (readOnly || saving || estimating) return;
    setEstimating(true);
    setError("");
    try {
      const estimate =
        action === "unfeature"
          ? null
          : await estimateViralOperation({
              action,
              platform: video.platform,
              items: [video],
            });
      setPending({ video, action, key: crypto.randomUUID(), estimate });
    } catch (cause) {
      setError(
        adminActivationErrorMessage(cause, "费用预估失败，未执行操作。"),
      );
    } finally {
      setEstimating(false);
    }
  }

  async function confirm(reason: string) {
    if (!pending) return;
    setSaving(true);
    setError("");
    try {
      let queued = false;
      if (pending.action === "archive")
        await archiveCollectedViralVideo(
          pending.video,
          reason,
          pending.key,
          pending.estimate?.snapshot,
        );
      else {
        const result = await curateViralVideo(
          pending.video,
          pending.action,
          reason,
          pending.key,
          pending.estimate?.snapshot,
        );
        queued = result.queued_for_preparation === true;
      }
      setNotice(
        queued
          ? "已排队准备素材，尚未上首页。"
          : pending.action === "archive"
            ? "已提交后台准备，可刷新明细查看进度。"
            : "首页展示设置已更新。",
      );
      const target = pending.video;
      setPending(null);
      // 明细行的归档/首页状态就地更新，避免为一次操作整页重拉。
      setDetail((previous) =>
        previous === null
          ? previous
          : {
              ...previous,
              items: previous.items.map((item) => {
                if (
                  !item.video ||
                  item.video.platform !== target.platform ||
                  item.video.video_id !== target.video_id
                )
                  return item;
                const video = { ...item.video };
                if (pending.action === "archive" || queued)
                  video.archive_status = "PENDING";
                else if (pending.action === "feature")
                  video.homepage_featured = true;
                else video.homepage_featured = false;
                return { ...item, video };
              }),
            },
      );
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "更新视频失败"));
    } finally {
      setSaving(false);
    }
  }

  async function addDemandKeyword(reason: string) {
    if (!keywordPending) return;
    setSaving(true);
    setError("");
    try {
      if (!keywordCategory) throw new Error("请选择客户端分类目录中的分类。");
      const result = await addViralKeywords(
        [
          {
            platform: keywordPending.row.platform,
            keyword: keywordPending.row.keyword,
            category: keywordCategory,
            enabled: true,
            limit: null,
          },
        ],
        reason,
        keywordPending.key,
      );
      setNotice(
        result.added
          ? "关键词已加入定时采集，下一轮生效。"
          : "该关键词已经配置，无需重复添加。",
      );
      setKeywordPending(null);
      await loadAggregate(date, rangePreset);
    } catch (cause) {
      setError(
        adminActivationErrorMessage(cause, "加入采集失败，所选分类已保留"),
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <section
        className="admin-panel admin-viral-discoveries"
        aria-label="客户需求洞察"
      >
        <header className="admin-viral-heading">
          <div>
            <h2>客户需求洞察</h2>
            <p className="admin-hint">
              客户在前台搜索爆款时用过的关键词与命中的视频会记录在这里；按词下钻后可直接准备、展示到首页。
            </p>
          </div>
        </header>
        {error && <PageBanner tone="error">{error}</PageBanner>}
        {notice && <PageBanner tone="notice">{notice}</PageBanner>}
        {aggregate && (
          <p className="admin-hint">
            统计区间：{aggregate.from ?? date} 至 {aggregate.to ?? date}
            （上海时间）。
            {aggregate.countingRule ?? "历史命中记录不能还原真实搜索次数。"}
            {aggregate.measurementStartedAt &&
              `真实搜索从 ${displayTime(aggregate.measurementStartedAt)} 开始记录；更早的数据未知。`}
          </p>
        )}
        {aggregate?.inventoryRule ? (
          <p className="admin-hint">库存口径：{aggregate.inventoryRule}</p>
        ) : null}
        {aggregate?.priorityRule ? (
          <p className="admin-hint">备货排序：{aggregate.priorityRule}</p>
        ) : null}
        <form
          className="admin-toolbar admin-viral-toolbar"
          onSubmit={(event) => {
            event.preventDefault();
            void loadAggregate(date, rangePreset);
          }}
        >
          <label>
            时间范围
            <select
              value={rangePreset}
              onChange={(event) =>
                setRangePreset(event.target.value as typeof rangePreset)
              }
            >
              <option value="day">单日</option>
              <option value="7d">近 7 天</option>
              <option value="30d">近 30 天</option>
              <option value="custom">自定义区间</option>
            </select>
          </label>
          {rangePreset === "custom" ? (
            <>
              <label>
                开始日期
                <input
                  type="date"
                  value={rangeFrom}
                  onChange={(event) => {
                    if (event.target.value) setRangeFrom(event.target.value);
                  }}
                />
              </label>
              <label>
                结束日期
                <input
                  type="date"
                  value={rangeTo}
                  onChange={(event) => {
                    if (event.target.value) setRangeTo(event.target.value);
                  }}
                />
              </label>
            </>
          ) : (
            <label>
              日期
              <input
                type="date"
                value={date}
                onChange={(event) => {
                  if (event.target.value) setDate(event.target.value);
                }}
              />
            </label>
          )}
          <button type="submit" disabled={loading}>
            查看
          </button>
        </form>
        {loading ? (
          <p role="status">正在读取搜索发现…</p>
        ) : aggregate === null ? (
          <p>暂无数据。</p>
        ) : aggregate.keywords.length === 0 ? (
          <p>该日期暂无用户搜索记录。</p>
        ) : (
          <section
            className="admin-table-scroll admin-viral-table-scroll"
            // biome-ignore lint/a11y/noNoninteractiveTabindex: 键盘用户需要聚焦滚动区域，以方向键查看窄窗口中的完整表格。
            tabIndex={0}
            aria-label="搜索发现聚合，可横向滚动"
          >
            <table className="admin-data-table admin-viral-table">
              <thead>
                <tr>
                  <th scope="col">关键词</th>
                  <th scope="col">平台</th>
                  <th scope="col">成功搜索页次</th>
                  <th scope="col">零结果页次</th>
                  <th scope="col">搜索账号 / 历史覆盖</th>
                  <th scope="col">命中视频</th>
                  <th scope="col">可用库存 / 采集配置</th>
                  <th scope="col">搜索趋势</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {aggregate.keywords.map((row) => {
                  const key = `${row.keyword}:${row.platform}`;
                  return (
                    <tr key={key}>
                      <td>{row.keyword}</td>
                      <td>{platformLabel(row.platform)}</td>
                      <td>
                        {row.searches == null
                          ? "历史未知"
                          : row.searches.toLocaleString("zh-CN")}
                      </td>
                      <td>
                        {row.searches == null
                          ? "历史未知"
                          : (row.zero_results ?? 0).toLocaleString("zh-CN")}
                      </td>
                      <td>
                        {row.searches == null ? "历史覆盖 " : "已记录搜索 "}
                        {row.users.toLocaleString("zh-CN")}
                        <button
                          type="button"
                          onClick={() =>
                            setCustomerTarget({
                              from: aggregate.from || date,
                              to: aggregate.to || date,
                              keyword: row.keyword,
                              platform: row.platform,
                            })
                          }
                        >
                          查看搜索客户
                        </button>
                      </td>
                      <td>{row.videos.toLocaleString("zh-CN")}</td>
                      <td>
                        <strong>
                          {row.inventory == null
                            ? "库存未知"
                            : `${row.inventory} 条可用`}
                        </strong>
                        {row.inventory === 0 && (row.searches ?? 0) > 0 ? (
                          <StatusBadge tone="warn">
                            有搜索 · 无可用库存
                          </StatusBadge>
                        ) : null}
                        <div className="admin-hint">
                          {row.configured == null
                            ? "配置未知"
                            : row.configured
                              ? row.collectionEnabled
                                ? "已配置 · 采集中"
                                : "已配置 · 暂停中"
                              : "尚未配置采集"}
                        </div>
                      </td>
                      <td>
                        {row.trend?.length ? (
                          <>
                            <SearchTrend days={row.trend} />
                            <details>
                              <summary>查看每日趋势</summary>
                              <ol>
                                {row.trend.map((day) => (
                                  <li key={day.date}>
                                    {day.date}：
                                    {day.searches == null
                                      ? "历史未知"
                                      : `${day.searches} 页次${day.partial ? "（开始记录当天，未覆盖全天）" : ""}`}
                                  </li>
                                ))}
                              </ol>
                            </details>
                          </>
                        ) : (
                          "趋势未知"
                        )}
                      </td>
                      <td>
                        <button
                          type="button"
                          onClick={() =>
                            void loadDetail(row.keyword, row.platform, 0)
                          }
                        >
                          查看明细
                        </button>
                        {!readOnly &&
                        row.configured === false &&
                        aggregate.categories?.length ? (
                          <button
                            type="button"
                            disabled={saving}
                            onClick={() => {
                              setKeywordCategory(
                                aggregate.categories?.[0] ?? "",
                              );
                              setKeywordPending({
                                row,
                                key: crypto.randomUUID(),
                              });
                              setError("");
                            }}
                          >
                            加入采集
                          </button>
                        ) : null}
                        {!readOnly && onSearch ? (
                          <button
                            type="button"
                            onClick={() => onSearch(row.keyword, row.platform)}
                          >
                            实时搜索补货
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        )}
        {detail && (
          <section aria-label="搜索发现明细">
            <h3>
              「{detail.keyword}」· {platformLabel(detail.platform)}
            </h3>
            {detailLoading ? (
              <p role="status">正在读取明细…</p>
            ) : detail.items.length === 0 ? (
              <p>该关键词暂无明细。</p>
            ) : (
              <section
                className="admin-table-scroll admin-viral-table-scroll"
                // biome-ignore lint/a11y/noNoninteractiveTabindex: 同聚合表格。
                tabIndex={0}
                aria-label="搜索发现明细，可横向滚动"
              >
                <table className="admin-data-table admin-viral-table">
                  <thead>
                    <tr>
                      <th scope="col">视频信息</th>
                      <th scope="col">最后搜索</th>
                      <th scope="col">归档 / 首页</th>
                      <th scope="col">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.items.map((item) => {
                      const video = item.video;
                      const key = `${item.platform}:${item.videoId}`;
                      return (
                        <tr key={key}>
                          <td>
                            {video?.cover_url ? (
                              <img
                                className="admin-viral-cover"
                                src={video.cover_url}
                                alt={`${video.title}封面`}
                                loading="lazy"
                              />
                            ) : (
                              <span className="admin-viral-muted">
                                暂无封面
                              </span>
                            )}
                            <div className="admin-viral-meta">
                              <span className="admin-viral-platform">
                                {platformLabel(item.platform)}
                              </span>
                              <span>覆盖 {item.users} 个用户</span>
                              <span>
                                覆盖命中记录{" "}
                                {item.discoveries.toLocaleString("zh-CN")}{" "}
                                条（用户×日期去重）
                              </span>
                            </div>
                            <strong
                              className="admin-viral-title"
                              title={video?.title ?? item.videoId}
                            >
                              {video?.title || "视频已不在内容池"}
                            </strong>
                            <div className="admin-viral-byline">
                              <span title={video?.author}>
                                {video?.author || "未知作者"}
                              </span>
                            </div>
                          </td>
                          <td>
                            <time dateTime={item.lastSearchedAt}>
                              {displayTime(item.lastSearchedAt)}
                            </time>
                          </td>
                          <td>
                            {video ? (
                              <div className="admin-viral-status">
                                <StatusBadge
                                  tone={
                                    archiveLabel(video) === "素材已准备"
                                      ? "good"
                                      : video.media_status === "FAILED"
                                        ? "danger"
                                        : "warn"
                                  }
                                >
                                  {contentStateLabel(video)}
                                </StatusBadge>
                                <span
                                  className={
                                    video.homepage_featured
                                      ? "admin-viral-featured"
                                      : "admin-viral-muted"
                                  }
                                >
                                  {video.homepage_featured
                                    ? "展示中"
                                    : "未展示"}
                                </span>
                              </div>
                            ) : (
                              <span className="admin-viral-muted">
                                已被删除，仅保留记录
                              </span>
                            )}
                          </td>
                          <td>
                            {video &&
                            !readOnly &&
                            !["removed", "blocked"].includes(
                              contentStateOf(video),
                            ) ? (
                              <div className="admin-viral-actions">
                                {archiveLabel(video) !== "素材已准备" && (
                                  <button
                                    type="button"
                                    disabled={
                                      saving || estimating || archiveBusy(video)
                                    }
                                    onClick={() =>
                                      void requestAction(video, "archive")
                                    }
                                  >
                                    {archiveBusy(video)
                                      ? "后台准备中"
                                      : archiveLabel(video) === "准备失败"
                                        ? "重试准备"
                                        : "准备到云端"}
                                  </button>
                                )}
                                <button
                                  type="button"
                                  disabled={
                                    saving ||
                                    estimating ||
                                    (!video.homepage_featured &&
                                      (video.media_status !== "SUCCEEDED" ||
                                        !video.storage_uri))
                                  }
                                  onClick={() =>
                                    void requestAction(
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
                              </div>
                            ) : (
                              <span className="admin-viral-muted">
                                {readOnly ? "只读账号" : "—"}
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </section>
            )}
            {detail.total > DETAIL_PAGE_SIZE && (
              <Pagination
                offset={detail.offset}
                limit={DETAIL_PAGE_SIZE}
                total={detail.total}
                disabled={detailLoading || saving}
                onPageChange={(offset) =>
                  void loadDetail(
                    detail.keyword,
                    detail.platform,
                    offset,
                    detail.window,
                  )
                }
              />
            )}
          </section>
        )}
      </section>
      {customerTarget ? (
        <ViralDemandCustomers
          key={`${customerTarget.from}:${customerTarget.to}:${customerTarget.keyword}:${customerTarget.platform}`}
          {...customerTarget}
          onCustomer={onCustomer}
          onClose={() => setCustomerTarget(null)}
        />
      ) : null}
      <ConfirmDialog
        open={pending !== null}
        busy={saving}
        error={error}
        level="reason"
        title={pending?.action === "archive" ? "准备单条视频" : "更新首页展示"}
        description={
          pending?.action === "archive"
            ? "后台将获取此视频并准备到已配置的云存储，可能产生供应商调用费用。不会重新搜索或修改首页展示。请填写操作原因。"
            : pending?.action === "unfeature"
              ? "取消后首页不再展示，视频仍保留在爆款列表。请填写操作原因。"
              : "已归档视频会自动补齐封面后展示到首页。请填写操作原因。"
        }
        confirmLabel="确认操作"
        onClose={() => setPending(null)}
        onConfirm={(reason) => void confirm(reason)}
      >
        {pending?.estimate ? (
          <ViralOperationCostSummary estimate={pending.estimate} />
        ) : null}
      </ConfirmDialog>
      <ConfirmDialog
        open={keywordPending !== null}
        title="需求词加入采集"
        description={keywordPending?.row.keyword}
        busy={saving}
        error={error}
        onConfirm={(reason) => void addDemandKeyword(reason)}
        onClose={() => !saving && setKeywordPending(null)}
      >
        <label>
          采集分类
          <select
            value={keywordCategory}
            onChange={(event) => setKeywordCategory(event.target.value)}
          >
            {aggregate?.categories?.map((category) => (
              <option key={category} value={category}>
                {category}
              </option>
            ))}
          </select>
        </label>
      </ConfirmDialog>
    </>
  );
}
