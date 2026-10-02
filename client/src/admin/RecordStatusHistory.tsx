import { useEffect, useState } from "react";
import { getGenerationRecordHistory } from "../api.admin";
import { Pagination } from "./ui/Pagination";
import {
  formatDateTime,
  GENERATION_STATUS_LABELS,
  labelFrom,
} from "./ui/vocabulary";

export function RecordStatusHistory({
  recordId,
  recordType,
}: {
  recordId: string;
  recordType: string;
}) {
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Awaited<
    ReturnType<typeof getGenerationRecordHistory>
  > | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    void getGenerationRecordHistory(recordType, recordId, offset)
      .then((result) => {
        if (active) setData(result);
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取状态历史失败");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [recordType, recordId, offset]);
  return (
    <section aria-label="任务状态历史">
      <h5>状态历史</h5>
      {loading ? <p role="status">正在读取状态历史…</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      {data ? (
        <>
          <p>
            {data.historyRule} 启用时间：
            {formatDateTime(data.measurementStartedAt)}
          </p>
          {data.items.length === 0 ? (
            <p>没有已记录的状态转换，历史过程未知。</p>
          ) : (
            <ol>
              {data.items.map((event) => (
                <li key={event.id}>
                  {formatDateTime(event.at)} ·{" "}
                  {event.before == null
                    ? "创建"
                    : labelFrom(GENERATION_STATUS_LABELS, event.before)}{" "}
                  → {labelFrom(GENERATION_STATUS_LABELS, event.after)}
                </li>
              ))}
            </ol>
          )}
          <Pagination
            limit={20}
            offset={offset}
            total={data.total}
            disabled={loading}
            onPageChange={setOffset}
          />
        </>
      ) : null}
    </section>
  );
}
