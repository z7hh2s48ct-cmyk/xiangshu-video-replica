import { useCallback, useEffect, useState } from "react";

import {
  AdminActivationError,
  adminActivationErrorMessage,
  fetchQueueMode,
  updateQueueMode,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { StatusBadge } from "./ui/StatusBadge";

/**
 * 公平队列开关（M4/M5 review M2）：把此前只能进数据库改的
 * PATCH /api/control/settings/queue-mode 翻译成管理台上的一个开关。
 * 确认后自动记录操作说明，并沿用管理端幂等写契约。
 */
export function QueueModeSection({ readOnly = false }: { readOnly?: boolean }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setEnabled(await fetchQueueMode());
    } catch (cause) {
      setError(adminActivationErrorMessage(cause, "读取队列模式失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function toggle() {
    if (enabled === null || saving) {
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      setEnabled(
        await updateQueueMode(
          !enabled,
          enabled ? "关闭公平队列" : "开启公平队列",
        ),
      );
      setNotice(!enabled ? "公平队列已开启。" : "公平队列已关闭。");
      setConfirmOpen(false);
    } catch (cause) {
      if (cause instanceof AdminActivationError && cause.status === 401) {
        setError("会话已失效，请重新登录");
      } else {
        setError(adminActivationErrorMessage(cause, "切换队列模式失败"));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <section
      aria-label="公平队列开关"
      className="admin-panel queue-mode-section"
    >
      <h2>公平队列</h2>
      <p className="admin-hint">
        开启后，多客户同时生成时按公平队列调度，避免单一客户占满并发额度；
        切换立即生效并写入审计。当前状态：
        {loading ? (
          "读取中…"
        ) : (
          <StatusBadge tone={enabled ? "good" : "neutral"}>
            {enabled === null ? "未知" : enabled ? "已开启" : "已关闭"}
          </StatusBadge>
        )}
      </p>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {readOnly ? (
        <p className="admin-hint">审计员只读，不能切换队列模式。</p>
      ) : (
        <button
          disabled={loading || enabled === null || saving}
          type="button"
          onClick={() => setConfirmOpen(true)}
        >
          {enabled ? "关闭公平队列" : "开启公平队列"}
        </button>
      )}

      <ConfirmDialog
        busy={saving}
        confirmLabel={enabled ? "确认关闭" : "确认开启"}
        description={
          enabled
            ? "关闭后，生成任务的领取顺序回到默认策略。切换立即生效并写入审计。"
            : "开启后，多客户同时生成将进入公平队列调度。切换立即生效并写入审计。"
        }
        error={error}
        level="standard"
        open={confirmOpen}
        title={enabled ? "关闭公平队列" : "开启公平队列"}
        onClose={() => setConfirmOpen(false)}
        onConfirm={() => void toggle()}
      />
    </section>
  );
}
