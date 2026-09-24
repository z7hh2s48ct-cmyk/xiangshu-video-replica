import { useCallback, useEffect, useRef, useState } from "react";

// C2（2026-09-02 评估）：资金相关页的可配置自动刷新。默认关闭——
// 打开后每 intervalMs 触发一次回调（回调自行守卫过期响应）。
// 页面隐藏（document.hidden）时暂停，避免后台标签页空转打点。
export function useAutoRefresh(
  refresh: () => void,
  intervalMs = 30_000,
): { autoRefresh: boolean; toggleAutoRefresh: () => void } {
  const [autoRefresh, setAutoRefresh] = useState(false);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  useEffect(() => {
    if (!autoRefresh) {
      return;
    }
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) {
        return;
      }
      refreshRef.current();
    }, intervalMs);
    return () => {
      window.clearInterval(timer);
    };
  }, [autoRefresh, intervalMs]);

  const toggleAutoRefresh = useCallback(() => {
    setAutoRefresh((current) => !current);
  }, []);

  return { autoRefresh, toggleAutoRefresh };
}
