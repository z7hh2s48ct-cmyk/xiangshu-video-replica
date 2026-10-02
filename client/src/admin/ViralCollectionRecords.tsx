import { useEffect, useState } from "react";
import { adminRead } from "../api.admin";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { shanghaiDate } from "./ui/vocabulary";

type CollectionRecord = {
  id: string;
  platform: string;
  trigger_kind: "manual" | "scheduled" | "realtime" | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  run_status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | null;
  keywords: string[];
  videos: number | null;
  new_count: number | null;
  ready: number | null;
  succeeded: number | null;
  failed: number | null;
  failed_video_count: number | null;
  calls: number;
  known_cost_fen: number | string;
  cost_fen: number | string | null;
  unknown_cost_count: number;
  pending_cost_count: number;
  failure_reason: string | null;
  failure_category?: string | null;
  related_task_id?: string | null;
  advice: string | null;
};
type RecordPage = {
  items: CollectionRecord[];
  total: number;
  countingRule: string;
};
const money = (value: number | string | null) =>
  value == null ? "待核对" : `¥${(Number(value) / 100).toFixed(4)}`;
const stamp = (value: string | null) =>
  value
    ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })
    : "尚无记录";

type RecordTasks = {
  related_task_id: string | null;
  current_queue: {
    status: string;
    attempt: number;
    retry_count: number;
  } | null;
  keywords: {
    platform: string;
    keyword: string;
    status: string;
    video_count: number | null;
    attempt_count: number;
    failure_category: string | null;
    advice: string | null;
  }[];
  video_total?: number;
  videos?: {
    platform: string;
    video_id: string;
    title: string;
    status: string;
    failure_category: string | null;
    advice: string | null;
  }[];
  note: string;
};
function CollectionRecordTasks({ batchId }: { batchId: string }) {
  const [videoOffset, setVideoOffset] = useState(0);
  const [page, setPage] = useState<RecordTasks | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    setPage(null);
    setError("");
    void adminRead<RecordTasks>(
      `/api/control/viral/collection/records/${encodeURIComponent(batchId)}/tasks?video_limit=20&video_offset=${videoOffset}`,
      "读取本轮任务失败",
    )
      .then((next) => {
        if (active) setPage(next);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取本轮任务失败");
      });
    return () => {
      active = false;
    };
  }, [batchId, videoOffset]);
  if (error) return <PageBanner tone="error">{error}</PageBanner>;
  if (!page) return <p role="status">正在读取本轮任务…</p>;
  return (
    <section aria-label="本轮关联任务">
      <p>
        {page.related_task_id
          ? `关联任务 ${page.related_task_id}`
          : "本轮直接执行，没有队列任务"}
      </p>
      {page.current_queue ? (
        <p>
          队列状态{" "}
          {(
            {
              PENDING: "排队中",
              RUNNING: "执行中",
              SUCCEEDED: "成功",
              FAILED: "失败",
            } as Record<string, string>
          )[page.current_queue.status] ?? "未知"}
          ，执行 {page.current_queue.attempt} 次，重试{" "}
          {page.current_queue.retry_count} 次
        </p>
      ) : page.related_task_id ? (
        <p>队列已复用，本轮结果请以独立记录为准。</p>
      ) : null}
      <ul>
        {page.keywords.map((run) => (
          <li key={`${run.platform}:${run.keyword}`}>
            {run.keyword}：
            {(
              {
                RUNNING: "执行中",
                SUCCEEDED: "成功",
                FAILED: "失败",
              } as Record<string, string>
            )[run.status] ?? "未知"}
            ，执行 {run.attempt_count} 次，视频 {run.video_count ?? "未知"} 条
            {run.failure_category ? (
              <p>
                {run.failure_category}；{run.advice}
              </p>
            ) : null}
          </li>
        ))}
      </ul>
      <h4>逐视频准备结果</h4>
      {page.videos?.length ? (
        <ul>
          {page.videos.map((video) => (
            <li key={`${video.platform}:${video.video_id}`}>
              <strong>{video.title}</strong>：
              {video.status === "FAILED" ? "准备失败" : "已准备"}
              {video.failure_category ? (
                <p>
                  {video.failure_category}；{video.advice}
                </p>
              ) : null}
              <a
                href={`#admin/viralVideos?${new URLSearchParams({ platform: video.platform, videoId: video.video_id })}`}
              >
                查看视频与准备任务
              </a>
            </li>
          ))}
        </ul>
      ) : (
        <p>本轮未记录逐视频结果；旧失败原因无法还原，实时搜索只入库不准备。</p>
      )}
      {page.video_total != null ? (
        <Pagination
          offset={videoOffset}
          limit={20}
          total={page.video_total}
          onPageChange={setVideoOffset}
        />
      ) : null}
      <p className="admin-hint">{page.note}</p>
    </section>
  );
}

export function ViralCollectionRecords({
  initialBatchId = "",
}: {
  initialBatchId?: string;
}) {
  const [targetId, setTargetId] = useState(initialBatchId);
  const [locatedId, setLocatedId] = useState(initialBatchId);
  const [start, setStart] = useState(() => shanghaiDate(-29));
  const [end, setEnd] = useState(() => shanghaiDate());
  const [offset, setOffset] = useState(0);
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<RecordPage | null>(null);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  useEffect(() => {
    void revision;
    let active = true;
    setPage(null);
    setExpanded(null);
    setError("");
    void adminRead<RecordPage>(
      `/api/control/viral/collection/records?${new URLSearchParams({ start, end, limit: "20", offset: String(offset) })}`,
      "读取采集记录失败",
    )
      .then((next) => {
        if (active) setPage(next);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取采集记录失败");
      });
    return () => {
      active = false;
    };
  }, [start, end, offset, revision]);
  return (
    <section className="admin-panel" aria-label="采集执行记录">
      <h2>采集执行记录</h2>
      <p className="admin-hint">
        定时、手动和后台实时搜索逐次留档；正在执行与失败的费用均需核对。
      </p>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setLocatedId(targetId.trim());
        }}
      >
        <label>
          定位采集记录
          <input
            value={targetId}
            onChange={(event) => setTargetId(event.target.value)}
          />
        </label>
        <button type="submit" disabled={!targetId.trim()}>
          定位本轮任务
        </button>
      </form>
      {locatedId ? (
        <CollectionRecordTasks key={locatedId} batchId={locatedId} />
      ) : null}
      <div className="admin-form-grid admin-viral-plan-form">
        <label>
          记录开始日期
          <input
            type="date"
            value={start}
            onChange={(e) => {
              setStart(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <label>
          记录结束日期
          <input
            type="date"
            value={end}
            onChange={(e) => {
              setEnd(e.target.value);
              setOffset(0);
            }}
          />
        </label>
        <button type="button" onClick={() => setRevision((v) => v + 1)}>
          刷新采集记录
        </button>
      </div>
      {error ? (
        <PageBanner tone="error">{error}</PageBanner>
      ) : !page ? (
        <p role="status">正在读取采集记录…</p>
      ) : (
        <>
          <p className="admin-hint">{page.countingRule}</p>
          <div className="admin-table-scroll">
            <table className="admin-data-table">
              <thead>
                <tr>
                  <th>时间 / 触发方式</th>
                  <th>平台 / 关键词</th>
                  <th>执行结果</th>
                  <th>视频 / 新增 / 就绪</th>
                  <th>请求 / 费用</th>
                  <th>异常与处理</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((record) => (
                  <tr key={record.id}>
                    <td>
                      {stamp(record.created_at)}
                      <br />
                      {record.trigger_kind
                        ? {
                            manual: "手动采集",
                            scheduled: "定时采集",
                            realtime: "后台实时搜索",
                          }[record.trigger_kind]
                        : "历史触发方式未知"}
                      <br />
                      <small>
                        开始 {stamp(record.started_at)}
                        <br />
                        结束 {stamp(record.completed_at)}
                      </small>
                    </td>
                    <td>
                      {record.platform === "douyin" ? "抖音" : "视频号"}
                      <br />
                      {record.keywords.join("、") || "历史关键词未知"}
                      <br />
                      <small>记录编号 {record.id}</small>
                    </td>
                    <td>
                      {record.run_status
                        ? {
                            PENDING: "排队中",
                            RUNNING: "执行中",
                            SUCCEEDED: "执行成功",
                            FAILED: "执行失败",
                          }[record.run_status]
                        : "历史结果未知"}
                      <br />
                      成功关键词 {record.succeeded ?? "不适用 / 未知"}
                      ，失败关键词 {record.failed ?? "不适用 / 未知"}
                      <br />
                      失败视频 {record.failed_video_count ?? "未知"}
                    </td>
                    <td>
                      接收去重 {record.videos ?? "未知"}
                      <br />
                      实际新增 {record.new_count ?? "历史未知"}
                      <br />
                      素材就绪 {record.ready ?? "未知"}
                    </td>
                    <td>
                      数据接口 {record.calls} 次<br />
                      已知成本 {money(record.known_cost_fen)}
                      <br />
                      接口总成本 {money(record.cost_fen)}
                      <br />
                      未知 {record.unknown_cost_count} 次，进行中{" "}
                      {record.pending_cost_count} 次
                    </td>
                    <td>
                      {record.failure_category ? (
                        <strong>
                          {record.failure_category}
                          <br />
                        </strong>
                      ) : null}
                      {record.failure_reason || "—"}
                      {record.advice ? <p>{record.advice}</p> : null}
                      <button
                        type="button"
                        onClick={() =>
                          setExpanded(expanded === record.id ? null : record.id)
                        }
                      >
                        {expanded === record.id
                          ? "收起本轮任务"
                          : "查看本轮任务"}
                      </button>
                      {expanded === record.id ? (
                        <CollectionRecordTasks
                          key={record.id}
                          batchId={record.id}
                        />
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!page.items.length ? <p>该日期范围暂无采集执行记录。</p> : null}
          <Pagination
            offset={offset}
            limit={20}
            total={page.total}
            onPageChange={setOffset}
          />
        </>
      )}
    </section>
  );
}
