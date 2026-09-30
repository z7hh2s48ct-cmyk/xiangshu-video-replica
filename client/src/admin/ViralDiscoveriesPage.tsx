import { useCallback, useEffect, useState } from "react";
import {
  adminActivationErrorMessage,
  archiveCollectedViralVideo,
  type CollectedViralVideo,
  curateViralVideo,
  listViralDiscoveries,
  listViralDiscoveryDetails,
  type ViralDiscoveryAggregate,
  type ViralDiscoveryDetail,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { archiveBusy, archiveLabel } from "./ViralVideosPage";

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

export function ViralDiscoveriesPage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [date, setDate] = useState(todayShanghai);
  // 方案 P1 客户需求洞察：单日之外支持近 7/30 天与自定义区间。
  const [rangePreset, setRangePreset] = useState<
    "day" | "7d" | "30d" | "custom"
  >("day");
  const [rangeFrom, setRangeFrom] = useState(todayShanghai());
  const [rangeTo, setRangeTo] = useState(todayShanghai());
  const [aggregate, setAggregate] = useState<ViralDiscoveryAggregate | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [saving, setSaving] = useState(false);
  // 下钻明细：keyword+platform 定位一个分组；offset 供翻页。
  const [detail, setDetail] = useState<{
    keyword: string;
    platform: string;
    items: ViralDiscoveryDetail[];
    total: number;
    offset: number;
  } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [pending, setPending] = useState<{
    video: CollectedViralVideo;
    action: Action;
    key: string;
  } | null>(null);

  const loadAggregate = useCallback(
    async (targetDate: string, preset: typeof rangePreset = "day") => {
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
                    new Date(Date.now() - (shiftDays - 1) * 86_400_000),
                  ),
                  to: targetDate,
                };
        const result = await listViralDiscoveries(targetDate, range);
        setAggregate(result);
      } catch (cause) {
        setError(adminActivationErrorMessage(cause, "读取搜索发现失败"));
      } finally {
        setLoading(false);
      }
    },
    [],
  );
  useEffect(() => {
    void loadAggregate(date);
  }, [date, loadAggregate]);

  const loadDetail = useCallback(
    async (keyword: string, platform: string, offset: number) => {
      setDetailLoading(true);
      setError("");
      try {
        const result = await listViralDiscoveryDetails({
          date,
          keyword,
          platform,
          offset,
          limit: DETAIL_PAGE_SIZE,
        });
        setDetail({
          keyword,
          platform,
          items: result.items,
          total: result.total,
          offset,
        });
      } catch (cause) {
        setError(adminActivationErrorMessage(cause, "读取搜索发现明细失败"));
      } finally {
        setDetailLoading(false);
      }
    },
    [date],
  );

  async function confirm(reason: string) {
    if (!pending) return;
    setSaving(true);
    setError("");
    try {
      if (pending.action === "archive")
        await archiveCollectedViralVideo(pending.video, reason, pending.key);
      else
        await curateViralVideo(
          pending.video,
          pending.action,
          reason,
          pending.key,
        );
      setNotice(
        pending.action === "archive"
          ? "已提交后台转存，可刷新明细查看进度。"
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
                if (pending.action === "archive")
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

  return (
    <section
      className="admin-panel admin-viral-discoveries"
      aria-label="用户搜索发现"
    >
      <header className="admin-viral-heading">
        <div>
          <h2>用户搜索发现</h2>
          <p className="admin-hint">
            客户在前台搜索爆款时用过的关键词与命中的视频会记录在这里；按词下钻后可直接转存、展示到首页。
          </p>
        </div>
      </header>
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {notice && <PageBanner tone="notice">{notice}</PageBanner>}
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
                <th scope="col">搜索次数</th>
                <th scope="col">用户数</th>
                <th scope="col">命中视频</th>
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
                    <td>{row.discoveries.toLocaleString("zh-CN")}</td>
                    <td>{row.users.toLocaleString("zh-CN")}</td>
                    <td>{row.videos.toLocaleString("zh-CN")}</td>
                    <td>
                      <button
                        type="button"
                        onClick={() =>
                          void loadDetail(row.keyword, row.platform, 0)
                        }
                      >
                        查看明细
                      </button>
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
                          <div className="admin-viral-meta">
                            <span className="admin-viral-platform">
                              {platformLabel(item.platform)}
                            </span>
                            <span>覆盖 {item.users} 个用户</span>
                            <span>
                              搜索 {item.discoveries.toLocaleString("zh-CN")} 次
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
                                  archiveLabel(video) === "归档就绪"
                                    ? "good"
                                    : video.media_status === "FAILED"
                                      ? "danger"
                                      : "warn"
                                }
                              >
                                {archiveLabel(video)}
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
                          ) : (
                            <span className="admin-viral-muted">
                              已被删除，仅保留记录
                            </span>
                          )}
                        </td>
                        <td>
                          {video && !readOnly ? (
                            <div className="admin-viral-actions">
                              {archiveLabel(video) !== "归档就绪" && (
                                <button
                                  type="button"
                                  disabled={saving || archiveBusy(video)}
                                  onClick={() =>
                                    setPending({
                                      video,
                                      action: "archive",
                                      key: crypto.randomUUID(),
                                    })
                                  }
                                >
                                  {archiveBusy(video)
                                    ? "后台转存中"
                                    : archiveLabel(video) === "转存失败"
                                      ? "重试转存"
                                      : "转存到云端"}
                                </button>
                              )}
                              <button
                                type="button"
                                disabled={
                                  saving ||
                                  (!video.homepage_featured &&
                                    (video.media_status !== "SUCCEEDED" ||
                                      !video.storage_uri))
                                }
                                onClick={() =>
                                  setPending({
                                    video,
                                    action: video.homepage_featured
                                      ? "unfeature"
                                      : "feature",
                                    key: crypto.randomUUID(),
                                  })
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
                void loadDetail(detail.keyword, detail.platform, offset)
              }
            />
          )}
        </section>
      )}
      <ConfirmDialog
        open={pending !== null}
        busy={saving}
        error={error}
        level="reason"
        title={pending?.action === "archive" ? "转存单条视频" : "更新首页展示"}
        description={
          pending?.action === "archive"
            ? "后台将获取此视频并转存到已配置的云存储，可能产生供应商调用费用。不会重新搜索或修改首页展示。请填写操作原因。"
            : pending?.action === "unfeature"
              ? "取消后首页不再展示，视频仍保留在爆款列表。请填写操作原因。"
              : "已归档视频会自动补齐封面后展示到首页。请填写操作原因。"
        }
        confirmLabel="确认操作"
        onClose={() => setPending(null)}
        onConfirm={(reason) => void confirm(reason)}
      />
    </section>
  );
}
