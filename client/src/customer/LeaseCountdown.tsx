import { useEffect, useState } from "react";

/** Lease countdown display with auto-refresh (FE-04 / T31).
 * Shows formatted expiry time, warning states, and provides refresh action.
 */
export function LeaseCountdown({
  expiresAt,
  onRefresh,
}: {
  expiresAt: string;
  onRefresh: () => void;
}): React.JSX.Element {
  // 审计 P2 清单 #15：最后一分钟要真的在跳。此前只在重渲染（心跳回来）时重算，
  // 于是「剩余 0 分钟」会停住不动——最需要预警的那一分钟里反而看不出变化。
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const now = new Date(nowMs);
  const expiry = new Date(expiresAt);
  const diffMs = expiry.getTime() - now.getTime();

  let status: "normal" | "warning" | "expired" = "normal";
  if (diffMs <= 0) {
    status = "expired";
  } else if (diffMs < 5 * 60_000) {
    // Less than 5 minutes
    status = "warning";
  }
  const secondsRemaining = Math.max(0, Math.ceil(diffMs / 1_000));

  const minutesRemaining = Math.floor(diffMs / 60_000);
  const formattedExpiry = expiry.toLocaleString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });

  return (
    <div className={`lease-countdown ${status}`}>
      <p className="label">
        本次会话有效至：
        <time dateTime={expiresAt}>{formattedExpiry}</time>
      </p>

      {status === "expired" && (
        <span className="status-text expired">
          ⚠️ 已过期——请刷新会话或重新登录
        </span>
      )}

      {status === "warning" && (
        <span className="status-text warning">
          {secondsRemaining <= 60
            ? `⏰ 约 ${secondsRemaining} 秒后失效——点「立即续约」可续期`
            : `⏰ 即将过期（剩余 ${minutesRemaining} 分钟）`}
        </span>
      )}

      {status === "normal" && (
        <span className="status-text normal">剩余 {minutesRemaining} 分钟</span>
      )}

      <button
        type="button"
        className="btn-renew"
        onClick={onRefresh}
        disabled={status === "expired"}
        aria-label="续约会话"
      >
        立即续约
      </button>
    </div>
  );
}
