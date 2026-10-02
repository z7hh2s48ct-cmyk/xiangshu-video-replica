import type { ViralOperationEstimate } from "../api.admin";

function money(fen: number | null) {
  return fen == null ? "未知" : `¥${(fen / 100).toFixed(4)}`;
}

export function ViralOperationCostSummary({
  estimate,
}: {
  estimate: ViralOperationEstimate;
}) {
  return (
    <section aria-label="操作前费用预估">
      <dl>
        <dt>当前接口成本单价</dt>
        <dd>{money(estimate.unitCostFen)} / 次</dd>
        <dt>逻辑数据请求</dt>
        <dd>0至{estimate.logicalCallsMax}次</dd>
        <dt>常规重试数据请求</dt>
        <dd>最多{estimate.normalRetryCallsMax}次</dd>
        <dt>常规重试接口费用</dt>
        <dd>{money(estimate.dataCostMaxFen)}</dd>
        <dt>可复用视频</dt>
        <dd>{estimate.reusedVideos}条</dd>
        <dt>待下载视频</dt>
        <dd>最多{estimate.mediaDownloadsMax}条</dd>
        <dt>下载、流量、存储及总费用</dt>
        <dd>未知</dd>
      </dl>
      <p>{estimate.note}</p>
    </section>
  );
}
