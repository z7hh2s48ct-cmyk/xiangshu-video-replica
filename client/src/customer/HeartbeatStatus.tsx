import { useEffect, useState } from "react";

/** Heartbeat status indicator (FE-04 / T31).
 * Shows the last heartbeat time, refresh interval, and connection health.
 */
export function HeartbeatStatus({
  connectivity = "reachable",
  lastHeartbeatAt,
  intervalSeconds = 30,
  onRefresh,
}: {
  connectivity?: "reachable" | "unreachable";
  lastHeartbeatAt: string;
  intervalSeconds?: number;
  onRefresh: () => void;
}): React.JSX.Element {
  const [nowMs, setNowMs] = useState(Date.now);
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);
  const now = new Date(nowMs);
  const heartbeat = new Date(lastHeartbeatAt);
  const diffMs = now.getTime() - heartbeat.getTime();
  const secondsSinceLast = Math.floor(diffMs / 1_000);

  // Status thresholds
  const isHealthy = secondsSinceLast < intervalSeconds * 0.8; // < 80% of interval
  const isWarning = secondsSinceLast >= intervalSeconds; // overdue
  const isLongOverdue = secondsSinceLast > intervalSeconds * 2; // > 2x interval

  const formattedHeartbeat = heartbeat.toLocaleString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });

  return (
    <div
      className={`heartbeat-status ${connectivity === "unreachable" ? "warning" : isHealthy ? "healthy" : isWarning ? "warning" : "pending"}`}
      role="status"
      aria-live="polite"
    >
      <p className="label">
        上次心跳：<time dateTime={lastHeartbeatAt}>{formattedHeartbeat}</time>
      </p>

      <p className="interval-info">每 {intervalSeconds} 秒自动续约会话</p>

      {connectivity === "unreachable" ? (
        <span className="status-text warning">网络暂不可达</span>
      ) : null}

      {connectivity === "reachable" && isHealthy ? (
        <span className="status-text healthy">✓ 连接正常</span>
      ) : null}

      {connectivity === "reachable" && !isHealthy && !isWarning ? (
        <span className="status-text pending">等待下次自动心跳</span>
      ) : null}

      {connectivity === "reachable" && isWarning && !isLongOverdue && (
        <span className="status-text warning">⚠️ 心跳已逾期，会话即将失效</span>
      )}

      {connectivity === "reachable" && isLongOverdue && (
        <span className="status-text expired">
          ✕ 长时间未收到心跳结果，请尝试重新连接
        </span>
      )}

      <button
        type="button"
        className="btn-refresh"
        onClick={onRefresh}
        aria-label="立即发送心跳"
      >
        立即续约
      </button>

      <footer className="help-text">
        会话每 {intervalSeconds} 秒自动续约；长时间失联后需要重新登录。
      </footer>
    </div>
  );
}
