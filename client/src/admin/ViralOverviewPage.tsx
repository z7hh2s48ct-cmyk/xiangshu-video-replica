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

export function ViralOverviewPage() {
  const [data, setData] = useState<ViralLibraryOverview | null>(null);
  const [error, setError] = useState("");
  const [business, setBusiness] = useState<ViralContentOverview | null>(null);
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
    void getViralContentOverview()
      .then((value) => {
        if (active) setBusiness(value);
      })
      .catch((cause) => {
        if (active)
          setError(adminActivationErrorMessage(cause, "读取内容经营数据失败"));
      });
    return () => {
      active = false;
    };
  }, []);
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
      {business && (
        <>
          <h3>
            本周采集转化（仅起计以来） · {business.from} 至 {business.to}
          </h3>
          <p className="admin-hint">{business.cohortRule}</p>
          <dl className="admin-content-funnel">
            {business.funnel.map((step) => (
              <div key={step.name}>
                <dt>{step.name}</dt>
                <dd>{step.count} 条</dd>
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
          <h3>爆款业务收支 · 同一区间</h3>
          <dl className="admin-viral-tiles">
            {[
              ["确认收入", business.finance.revenueFen],
              ["平台调用成本", business.finance.costFen],
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
                    {v.title || "历史视频"} · {v.requests} 次成功读取
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
                        {k.keyword} · 采集 {k.collected} 条 / 客户详情使用{" "}
                        {k.used} 条
                      </li>
                    ),
                  )}
                </ol>
              </section>
            ))}
          </div>
          <p className="admin-hint">{business.keywordRule}</p>
        </>
      )}
      {data ? (
        <>
          <dl className="admin-viral-tiles">
            {[
              ["内容池", data.content_total],
              ["素材已准备", data.archive_ready],
              ["首页当前展示", data.homepage_featured],
              ["待准备", data.pending_archive],
              ["准备失败", data.archive_failed],
            ].map(([label, value]) => (
              <div className="admin-viral-tile" key={label}>
                <dt>{label}</dt>
                <dd>{value} 条</dd>
              </div>
            ))}
          </dl>
          <div className="admin-content-next">
            <h3>运营待办</h3>
            <a href="#admin/viralVideos">
              处理 {data.archive_failed} 条准备失败与 {data.pending_archive}{" "}
              条待准备内容
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
