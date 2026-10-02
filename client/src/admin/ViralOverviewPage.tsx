import { useEffect, useState } from "react";
import {
  adminActivationErrorMessage,
  getViralContentOverview,
  type ViralContentOverview,
  type ViralLibraryOverview,
  viralLibraryOverview,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { formatDateTime, formatFen } from "./ui/vocabulary";
import { ViralResourceCosts } from "./ViralResourceCosts";

function videoListHref(filters: Record<string, string>) {
  return `#admin/viralVideos?${new URLSearchParams({ listQuery: new URLSearchParams(filters).toString() })}`;
}

export function ViralOverviewPage({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [data, setData] = useState<ViralLibraryOverview | null>(null);
  const [error, setError] = useState("");
  const [business, setBusiness] = useState<ViralContentOverview | null>(null);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [platform, setPlatform] = useState("");
  const [scope, setScope] = useState({ from: "", to: "", platform: "" });
  useEffect(() => {
    let active = true;
    void viralLibraryOverview()
      .then((value) => {
        if (active) setData(value);
      })
      .catch((cause) => {
        if (active)
          setError(adminActivationErrorMessage(cause, "读取内容概览失败"));
      });
    void getViralContentOverview(
      scope.from || undefined,
      scope.to || undefined,
      scope.platform || undefined,
    )
      .then((value) => {
        if (active) {
          setBusiness(value);
          setFrom((previous) => previous || value.from);
          setTo((previous) => previous || value.to);
        }
      })
      .catch((cause) => {
        if (active)
          setError(adminActivationErrorMessage(cause, "读取内容经营数据失败"));
      });
    return () => {
      active = false;
    };
  }, [scope]);
  return (
    <section
      className="admin-panel admin-content-overview"
      aria-label="内容概览"
    >
      <header>
        <span className="admin-viral-eyebrow">内容运营</span>
        <h2>内容概览</h2>
        <p className="admin-hint">
          先看当前内容与待办，再进入视频库挑选内容、安排首页和采集。
        </p>
      </header>
      {error && <PageBanner tone="error">{error}</PageBanner>}
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setError("");
          setScope({ from, to, platform });
        }}
      >
        <label>
          采集起始日期
          <input
            type="date"
            value={from}
            onChange={(event) => setFrom(event.target.value)}
            required
          />
        </label>
        <label>
          采集结束日期
          <input
            type="date"
            value={to}
            min={from || undefined}
            onChange={(event) => setTo(event.target.value)}
            required
          />
        </label>
        <label>
          采集漏斗平台
          <select
            value={platform}
            onChange={(event) => setPlatform(event.target.value)}
          >
            <option value="">全部平台</option>
            <option value="douyin">抖音</option>
            <option value="wechat_channels">视频号</option>
          </select>
        </label>
        <button type="submit">查看区间</button>
      </form>
      {business && (
        <>
          <h3>
            采集转化（仅起计以来） · {business.from} 至 {business.to}
          </h3>
          <p className="admin-hint">{business.cohortRule}</p>
          <dl className="admin-content-funnel">
            {business.funnel.map((step) => (
              <div key={step.name}>
                <dt>{step.name}</dt>
                <dd>
                  <a
                    href={videoListHref({
                      cohortStage: step.stage,
                      collectedFrom: business.from,
                      collectedTo: business.to,
                      ...(business.platform
                        ? { platform: business.platform }
                        : {}),
                    })}
                  >
                    {step.count} 条
                  </a>
                </dd>
                <small>
                  {step.conversion == null
                    ? "无可比较基数"
                    : `转化 ${(step.conversion * 100).toFixed(1)}%`}
                </small>
              </div>
            ))}
          </dl>
          <p className="admin-hint">
            {business.historyNote} 起计：
            {formatDateTime(business.measurementStartedAt)}
            。直接获取文案但未记录详情读取：{business.directCopyWithoutDetail}{" "}
            条。
          </p>
          <h3>爆款业务收支 · 同一区间全部平台</h3>
          <dl className="admin-viral-tiles">
            {[
              ["确认收入", business.finance.revenueFen],
              ["全部业务成本", business.finance.costFen],
              ["业务毛利", business.finance.grossFen],
            ].map(([label, value]) => (
              <div className="admin-viral-tile" key={String(label)}>
                <dt>{label}</dt>
                <dd>{value == null ? "待核对" : formatFen(Number(value))}</dd>
              </div>
            ))}
            <div className="admin-viral-tile">
              <dt>毛利率</dt>
              <dd>
                {business.finance.grossRate == null
                  ? "待核对 / 无收入基数"
                  : `${(business.finance.grossRate * 100).toFixed(1)}%`}
              </dd>
            </div>
          </dl>
          <p className="admin-hint">
            已确认收入 {formatFen(business.finance.knownRevenueFen)}，
            {business.finance.unknownRevenueCount} 项收入待核对；已确认成本{" "}
            {formatFen(business.finance.knownCostFen)}，
            {business.finance.unknownCostCount} 项成本待核对。
          </p>
          <details>
            <summary>收支口径</summary>
            <p>{business.finance.countingRule}</p>
          </details>
          <div className="admin-content-rankings">
            <section>
              <h3>客户常用视频 · 前10</h3>
              <ol>
                {business.topVideos.map((v) => (
                  <li key={`${v.platform}:${v.video_id}`}>
                    <a
                      href={`#admin/viralVideos?${new URLSearchParams({ videoId: v.video_id, platform: v.platform })}`}
                    >
                      {v.title || "历史视频"}
                    </a>{" "}
                    · {v.requests} 次成功读取
                  </li>
                ))}
              </ol>
              {business.topVideos.length === 0 && (
                <p>本期暂无起计以来的客户读取。</p>
              )}
            </section>
            {[
              ["表现较好的关键词", business.bestKeywords],
              ["需要补充的关键词", business.worstKeywords],
            ].map(([title, keywords]) => (
              <section key={String(title)}>
                <h3>{String(title)} · 前5</h3>
                <ol>
                  {(keywords as ViralContentOverview["bestKeywords"]).map(
                    (k) => (
                      <li key={`${k.platform}:${k.keyword}`}>
                        <a
                          href={videoListHref({
                            cohortStage: "collected",
                            collectedFrom: business.from,
                            collectedTo: business.to,
                            platform: k.platform,
                            sourceKeyword: k.keyword,
                          })}
                        >
                          {k.keyword}
                        </a>{" "}
                        · 采集 {k.collected} 条 / 客户详情使用 {k.used} 条
                      </li>
                    ),
                  )}
                </ol>
              </section>
            ))}
          </div>
          <p className="admin-hint">{business.keywordRule}</p>
          <ViralResourceCosts
            key={`${business.from}:${business.to}`}
            from={business.from}
            to={business.to}
            readOnly={readOnly}
          />
        </>
      )}
      {data ? (
        <>
          <dl className="admin-viral-tiles">
            {(
              [
                ["内容池", data.content_total, {}],
                ["素材已准备", data.archive_ready, { libraryStatus: "ready" }],
                [
                  "首页当前展示",
                  data.homepage_featured,
                  { libraryStatus: "featured" },
                ],
                ["待准备", data.pending_archive, { libraryStatus: "pending" }],
                ["准备失败", data.archive_failed, { libraryStatus: "failed" }],
                [
                  "缺少归档封面",
                  data.missing_cover ?? 0,
                  { attention: "missing_cover" },
                ],
              ] as Array<[string, number, Record<string, string>]>
            ).map(([label, value, filters]) => (
              <div className="admin-viral-tile" key={label}>
                <dt>{label}</dt>
                <dd>
                  <a href={videoListHref(filters as Record<string, string>)}>
                    {value} 条
                  </a>
                </dd>
              </div>
            ))}
          </dl>
          <div className="admin-content-next">
            <h3>运营待办</h3>
            <a href={videoListHref({ libraryStatus: "failed" })}>
              处理 {data.archive_failed} 条准备失败
            </a>
            <a href={videoListHref({ libraryStatus: "pending" })}>
              准备 {data.pending_archive} 条内容
            </a>
            <a href={videoListHref({ attention: "missing_cover" })}>
              补齐 {data.missing_cover ?? 0} 条封面
            </a>
            <a href="#admin/viralHomepage">检查首页封面、素材状态与排期</a>
            <a href="#admin/viralDiscoveries">查看客户搜索需求与补货方向</a>
            <a href="#admin/viralCollection">
              {data.collection_enabled
                ? "检查采集计划和关键词"
                : "采集已暂停，查看采集设置"}
            </a>
          </div>
          <p className="admin-hint">
            库存用于当前待办；上面的转化仅跟踪同一采集批次，两者数量不必相等。
          </p>
        </>
      ) : (
        !error && <p role="status">正在读取内容概览…</p>
      )}
    </section>
  );
}
