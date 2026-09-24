import { useEffect, useRef, useState } from "react";

/** 会话冲突对话框（FE-03 / T30）。
 *
 * 用户在非当前在线设备上尝试登录时展示：切换必须显式确认，在服务端
 * 确认之前 UI 不得假设切换成功（“后登录自动踢人”红线）。
 *
 * 可访问性：打开即聚焦“取消”（安全操作）；Escape 等同取消（切换进行中
 * 除外）；Tab 在对话框内循环，不逃逸到底层页面。
 */
export function SessionConflictDialog({
  conflict,
  error,
  onCancel,
  onSwitch,
}: {
  conflict: {
    deviceNameMasked: string;
    leaseExpiresAt: string;
    slotNo: number;
  };
  /** 切换失败的原因（FE-03：失败必须可见，不得让按钮静默恢复可点）。
   * 由 RootApp 从会话 hook 的 error 状态传入。 */
  error?: string | null;
  onCancel: () => void;
  onSwitch: () => void;
}): React.JSX.Element {
  const [isProcessing, setIsProcessing] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    cancelRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isProcessing) {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab" || rootRef.current === null) {
        return;
      }
      const focusable = rootRef.current.querySelectorAll<HTMLElement>(
        "button:not([disabled]), [href], input:not([disabled])",
      );
      if (focusable.length === 0) {
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [isProcessing, onCancel]);

  const handleSwitch = async () => {
    setIsProcessing(true);
    try {
      await onSwitch();
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <div
      className="session-conflict-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="conflict-title"
      aria-describedby="conflict-description"
      ref={rootRef}
    >
      <header>
        <h1 id="conflict-title">检测到会话冲突</h1>
      </header>

      <div id="conflict-description">
        <p className="conflict-message">
          您的会话当前已在另一台设备上活跃：
          <strong>{conflict.deviceNameMasked}</strong>
        </p>

        <p className="lease-info">
          租约到期时间：{new Date(conflict.leaseExpiresAt).toLocaleString()}
        </p>

        <p className="slot-info">
          当前槽位：<strong>#{conflict.slotNo}</strong>
        </p>

        <p className="action-message">
          切换后另一台设备将立即下线。是否切换到本设备继续使用？
        </p>

        {error ? (
          <p className="form-error" role="alert">
            切换失败：{error}。可重试，或取消后稍后再试。
          </p>
        ) : null}
      </div>

      <footer className="dialog-actions">
        <button
          type="button"
          className="btn-secondary"
          ref={cancelRef}
          onClick={onCancel}
          disabled={isProcessing}
        >
          取消
        </button>

        <button
          type="button"
          className="btn-primary"
          onClick={handleSwitch}
          disabled={isProcessing}
        >
          {isProcessing ? "正在切换…" : "切换到本设备"}
        </button>
      </footer>
    </div>
  );
}
