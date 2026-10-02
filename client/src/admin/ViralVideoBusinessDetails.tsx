import { useEffect, useRef, useState } from "react";
import {
  adminActivationErrorMessage,
  type CollectedViralVideo,
  getViralVideoBusinessDetails,
  type ViralVideoBusinessDetails as ViralBusinessData,
} from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { formatDateTime, formatFen } from "./ui/vocabulary";

function mediaLabel(status: string, ready: boolean) {
  if (ready) return "已准备";
  return (
    (
      {
        PENDING: "等待准备",
        RUNNING: "正在准备",
        FAILED: "准备失败",
      } as Record<string, string>
    )[status] ?? "尚未准备"
  );
}

export function ViralVideoBusinessDetails({
  video,
  onRelated,
  onCustomer,
}: {
  video: CollectedViralVideo;
  onRelated: (video: CollectedViralVideo) => void;
  onCustomer?: (userId: string) => void;
}) {
  const [details, setDetails] = useState<ViralBusinessData | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const generation = useRef(0);
  const { platform, video_id } = video;
  useEffect(() => {
    const current = ++generation.current;
    setLoading(true);
    setError("");
    void getViralVideoBusinessDetails({ platform, video_id }, offset)
      .then((result) => {
        if (current === generation.current) setDetails(result);
      })
      .catch((cause) => {
        if (current === generation.current)
          setError(adminActivationErrorMessage(cause, "读取视频经营详情失败"));
      })
      .finally(() => {
        if (current === generation.current) setLoading(false);
      });
    return () => {
      generation.current += 1;
    };
  }, [platform, video_id, offset]);
  return (
    <section aria-label="视频经营详情">
      {error && <PageBanner tone="error">{error}</PageBanner>}
      {loading && <p role="status">正在读取经营与素材状态…</p>}
      {details && (
        <>
          <h4 className="admin-viral-drawer__section">素材准备</h4>
          <dl className="admin-viral-drawer__meta">
            <div>
              <dt>视频播放素材</dt>
              <dd>
                {mediaLabel(
                  details.media.video.status,
                  details.media.video.ready,
                )}
              </dd>
            </div>
            <div>
              <dt>音频提取素材</dt>
              <dd>
                {mediaLabel(
                  details.media.audio.status,
                  details.media.audio.ready,
                )}
              </dd>
            </div>
          </dl>
          {details.media.audio.ready && !details.media.video.ready && (
            <p className="admin-hint">
              音频已缓存，可以复用提取文案；视频尚未准备，暂时不能播放。播放时只需另行准备视频。
            </p>
          )}
          <h4 className="admin-viral-drawer__section">客户使用与收入</h4>
          <p className="admin-hint">
            {details.business.window}。{details.business.countingRule}
          </p>
          <dl className="admin-viral-drawer__stats">
            <div>
              <dt>详情授权账号</dt>
              <dd>{details.business.detailAccounts}</dd>
            </div>
            <div>
              <dt>文案授权账号</dt>
              <dd>{details.business.copyAccounts}</dd>
            </div>
            <div>
              <dt>当前收藏</dt>
              <dd>{details.business.favoriteAccounts}</dd>
            </div>
            <div>
              <dt>客户生成扣费</dt>
              <dd>{details.business.chargedCredits} 积分</dd>
            </div>
            <div>
              <dt>确认收入</dt>
              <dd>
                {details.business.revenueFen == null
                  ? "待核对"
                  : formatFen(details.business.revenueFen)}
              </dd>
            </div>
            <div>
              <dt>采集、准备与存储总成本</dt>
              <dd>
                {details.business.collectionCostFen == null
                  ? "待核对"
                  : formatFen(details.business.collectionCostFen)}
              </dd>
            </div>
          </dl>
          {details.business.unknownRevenueOperations > 0 && (
            <p className="admin-hint">
              已确认部分 {formatFen(details.business.knownRevenueFen)}；
              {details.business.unknownRevenueOperations}{" "}
              笔收入缺资金来源证据，未补零。
            </p>
          )}
          <p className="admin-hint">
            已归属接口请求 {details.business.attributedDataCalls ?? "未记录"}{" "}
            次； 已知接口成本{" "}
            {details.business.knownDataCostFen == null
              ? "待核对"
              : formatFen(details.business.knownDataCostFen)}
            ； 未知成本 {details.business.unknownDataCostCalls ?? "未记录"}{" "}
            次，进行中 {details.business.pendingDataCostCalls ?? "未记录"} 次。
          </p>
          <p className="admin-hint">{details.business.costNote}</p>
          <details>
            <summary>
              客户使用明细（{details.business.customerTotal} 个收费主体）
            </summary>
            {details.business.customers.length === 0 ? (
              <p>暂无已授权客户。</p>
            ) : (
              details.business.customers.map((customer) => (
                <article
                  key={customer.walletOwnerId}
                  className="admin-viral-customer-use"
                >
                  <strong>{customer.name}</strong>
                  {onCustomer ? (
                    <button
                      type="button"
                      onClick={() => onCustomer(customer.walletOwnerId)}
                    >
                      查看客户
                    </button>
                  ) : null}
                  <p>
                    {customer.detailAuthorized ? "已授权详情" : "未授权详情"} ·{" "}
                    {customer.copyAuthorized ? "已授权文案" : "未授权文案"}
                  </p>
                  <p>
                    {customer.chargedCredits} 积分 · 收入{" "}
                    {customer.revenueFen == null
                      ? "待核对"
                      : formatFen(customer.revenueFen)}
                  </p>
                  <p className="admin-hint">
                    最近使用 {formatDateTime(customer.lastUsedAt)} · 操作账号{" "}
                    {customer.actors.map((actor) => actor.name).join("、")}
                  </p>
                </article>
              ))
            )}
            <Pagination
              offset={offset}
              limit={20}
              total={details.business.customerTotal}
              disabled={loading}
              onPageChange={setOffset}
            />
          </details>
          <h4 className="admin-viral-drawer__section">来源与文案</h4>
          <p>来源关键词：{details.sourceKeywords.join("、") || "历史未记录"}</p>
          {details.originalUrl ? (
            <a href={details.originalUrl} target="_blank" rel="noreferrer">
              打开原始分享页
            </a>
          ) : (
            <p className="admin-hint">原始分享页：暂无已记录链接。</p>
          )}
          {details.sourceDescription && (
            <details>
              <summary>平台原始描述</summary>
              <p className="admin-viral-copy">{details.sourceDescription}</p>
            </details>
          )}
          {details.copy ? (
            <>
              <p className="admin-viral-copy">{details.copy.text}</p>
              <p className="admin-hint">
                文案缓存更新于 {formatDateTime(details.copy.updatedAt)}
              </p>
              <button
                type="button"
                onClick={() => {
                  void navigator.clipboard
                    .writeText(details.copy?.text ?? "")
                    .then(() => setCopied(true))
                    .catch(() => setError("复制失败，请选择文案手动复制。"));
                }}
              >
                {copied ? "已复制" : "复制文案"}
              </button>
            </>
          ) : (
            <p className="admin-hint">
              暂无共享文案；读取详情不会触发转写或下载视频。
            </p>
          )}
          <details>
            <summary>同作者其他视频（{details.relatedVideos.length}）</summary>
            {details.relatedVideos.map((related) => (
              <button
                type="button"
                key={related.video_id}
                onClick={() => onRelated(related)}
              >
                {related.title || "未命名视频"}
              </button>
            ))}
          </details>
          <details>
            <summary>技术详情</summary>
            <dl className="admin-viral-drawer__meta">
              <div>
                <dt>视频编号</dt>
                <dd>{video.video_id}</dd>
              </div>
              <div>
                <dt>视频存储地址</dt>
                <dd>{video.storage_uri || "尚未生成"}</dd>
              </div>
            </dl>
          </details>
        </>
      )}
    </section>
  );
}
