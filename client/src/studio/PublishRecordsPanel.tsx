import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelPublishRecord,
  deletePublishRecord,
  listPublishRecords,
  type PublishRecordItem,
  type PublishRecordStatus,
  retryPublishRecord,
  syncPublishRecord,
} from "../api";
import { PlatformLogo, type PublishPlatform } from "./PlatformLogo";
import { Button, Empty, formatTaskTime } from "./ui";

export const PUBLISH_RECORDS_POLL_MS = 15_000;

export const publishStatusNames: Record<PublishRecordStatus, string> = {
  queued: "排队中",
  publishing: "发布中",
  published: "已发布",
  failed: "发布失败",
  cancelled: "已取消",
};

const statusClass: Record<PublishRecordStatus, string> = {
  queued: "queued",
  publishing: "running",
  published: "completed",
  failed: "failed",
  cancelled: "cancelled",
};

const statNames: Array<[string, string]> = [
  ["play_count", "播放"],
  ["like_count", "点赞"],
  ["comment_count", "评论"],
  ["share_count", "分享"],
];

function scheduleLabel(record: PublishRecordItem): string {
  if (record.status === "published" && record.published_at)
    return `发布于 ${formatTaskTime(record.published_at)}`;
  if (record.scheduled_at) {
    const when = formatTaskTime(record.scheduled_at);
    return record.status === "queued" && record.attempt_count > 0
      ? `将于 ${when} 自动重试`
      : `定时 ${when}`;
  }
  return `提交于 ${formatTaskTime(record.created_at)}`;
}

export function isActivePublishRecord(record: PublishRecordItem): boolean {
  return (
    record.status === "queued" ||
    record.status === "publishing" ||
    record.sync_requested
  );
}

export function PublishRecordsPanel({
  refreshToken = 0,
  disabled = false,
  onError,
}: {
  refreshToken?: number;
  disabled?: boolean;
  onError?(message: string): void;
}) {
  const [records, setRecords] = useState<PublishRecordItem[] | null>(null);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const active = useRef(true);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const items = await listPublishRecords({ limit: 50 });
      if (!active.current) return;
      setRecords(items);
      setError("");
      if (timer.current) clearTimeout(timer.current);
      timer.current = items.some(isActivePublishRecord)
        ? setTimeout(() => void load(), PUBLISH_RECORDS_POLL_MS)
        : null;
    } catch (cause) {
      if (!active.current) return;
      setError(cause instanceof Error ? cause.message : "读取发布记录失败");
    }
  }, []);

  useEffect(() => {
    void refreshToken;
    active.current = true;
    void load();
    return () => {
      active.current = false;
      if (timer.current) clearTimeout(timer.current);
      timer.current = null;
    };
  }, [load, refreshToken]);

  async function act(
    record: PublishRecordItem,
    action: (id: string) => Promise<PublishRecordItem | undefined>,
  ) {
    if (busyId) return;
    setBusyId(record.id);
    try {
      const updated = await action(record.id);
      if (!active.current) return;
      setRecords((previous) =>
        previous
          ? updated
            ? previous.map((item) => (item.id === record.id ? updated : item))
            : previous.filter((item) => item.id !== record.id)
          : previous,
      );
      if (updated && isActivePublishRecord(updated) && !timer.current)
        timer.current = setTimeout(() => void load(), PUBLISH_RECORDS_POLL_MS);
    } catch (cause) {
      if (!active.current) return;
      const message =
        cause instanceof Error ? cause.message : "操作失败，请重试。";
      setError(message);
      onError?.(message);
    } finally {
      if (active.current) setBusyId(null);
    }
  }

  if (records === null && !error) return <p role="status">正在读取发布记录…</p>;
  return (
    <section className="content-publish-records" aria-label="发布记录">
      {error && (
        <p role="alert">
          {error} <Button onClick={() => void load()}>重新加载</Button>
        </p>
      )}
      {records && records.length === 0 && (
        <Empty
          title="暂无发布记录"
          description="选择成片与账号后点击「立即发布」或「定时发布」，记录会显示在这里。"
        />
      )}
      {records?.map((record) => {
        const stats = record.stats ?? {};
        const numbers = statNames.filter(
          ([key]) => typeof stats[key] === "number",
        );
        const busy = disabled || busyId === record.id;
        return (
          <article
            className="content-publish-record"
            key={record.id}
            data-status={record.status}
          >
            <PlatformLogo
              platform={record.platform as PublishPlatform}
              size={28}
            />
            <div className="content-publish-record-body">
              <strong>{record.title || "未命名发布"}</strong>
              <small>
                {record.account_username ?? "账号已解绑"} ·{" "}
                {scheduleLabel(record)}
                {record.delivery_mode === "browser" && " · 浏览器兜底"}
              </small>
              {record.platform_short_url && (
                <a
                  href={record.platform_short_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  查看作品
                </a>
              )}
              {!record.platform_short_url && record.platform_item_id && (
                <small>作品 ID：{record.platform_item_id}</small>
              )}
              {record.platform_status && (
                <small>平台状态：{record.platform_status}</small>
              )}
              {numbers.length > 0 && (
                <small className="content-publish-record-stats">
                  {numbers
                    .map(([key, label]) => `${label} ${String(stats[key])}`)
                    .join(" · ")}
                  {record.stats_synced_at &&
                    ` · 同步于 ${formatTaskTime(record.stats_synced_at)}`}
                </small>
              )}
              {record.error_message && (
                <p role="alert">{record.error_message}</p>
              )}
            </div>
            <span
              className={`studio-status studio-status--${statusClass[record.status]}`}
            >
              {publishStatusNames[record.status]}
              {record.sync_requested && " · 同步中"}
            </span>
            <div className="content-publish-record-actions">
              {record.status === "queued" && (
                <Button
                  disabled={busy}
                  onClick={() => void act(record, cancelPublishRecord)}
                >
                  取消
                </Button>
              )}
              {record.status === "failed" && (
                <Button
                  disabled={busy}
                  onClick={() => void act(record, retryPublishRecord)}
                >
                  重试
                </Button>
              )}
              {record.status === "published" && (
                <Button
                  disabled={busy || record.sync_requested}
                  onClick={() => void act(record, syncPublishRecord)}
                >
                  同步数据
                </Button>
              )}
              {(record.status === "published" ||
                record.status === "failed" ||
                record.status === "cancelled") && (
                <Button
                  disabled={busy}
                  variant="quiet"
                  onClick={() =>
                    void act(record, async (id) => {
                      await deletePublishRecord(id);
                      return undefined;
                    })
                  }
                >
                  删除记录
                </Button>
              )}
            </div>
          </article>
        );
      })}
    </section>
  );
}
