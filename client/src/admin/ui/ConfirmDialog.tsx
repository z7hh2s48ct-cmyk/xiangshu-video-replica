import { type FormEvent, useEffect, useRef, useState } from "react";

// 统一的高危操作确认对话框（2026-09-02 管理端评估 §交互规范）：
// - standard：说明性确认（低危、不可逆性弱，如查单）；
// - reason：原因必填（中危，如改价、暂停激活码）；
// - reasonAndAck：原因必填 + 勾选"我已知晓"（高危，如加款、吊销）。
// Esc 与遮罩点击关闭；打开时焦点落在原因输入框（或确认按钮）。
export type ConfirmLevel = "standard" | "reason" | "reasonAndAck";

export function ConfirmDialog({
  open,
  title,
  description,
  level = "reason",
  confirmLabel = "确认执行",
  busy = false,
  error = "",
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  description?: React.ReactNode;
  level?: ConfirmLevel;
  confirmLabel?: string;
  busy?: boolean;
  /** 提交失败时在对话框内展示；清空后回到表单态。 */
  error?: string;
  /** reason 仅在 reason/reasonAndAck 级别有值。 */
  onConfirm: (reason: string) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [localError, setLocalError] = useState("");
  const reasonInputRef = useRef<HTMLInputElement | null>(null);
  const confirmButtonRef = useRef<HTMLButtonElement | null>(null);
  const dialogRef = useRef<HTMLFormElement | null>(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    // 每次打开都是一次新的确认：清掉上一次的输入与错误。
    setReason("");
    setAcknowledged(false);
    setLocalError("");
    if (level === "standard") {
      confirmButtonRef.current?.focus();
    } else {
      reasonInputRef.current?.focus();
    }
  }, [level, open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        onClose();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [busy, onClose, open]);

  // 焦点圈闭：`aria-modal="true"` 承诺对话框外的内容不可交互，但只加这个属性
  // 并不会拦住 Tab —— 键盘用户仍能一路 Tab 到对话框背后的页面控件上，看不见
  // 焦点在哪。Esc 与遮罩点击此前已实现，缺的是这一层（2026-09-12 评审 P3 的
  // a11y 缺口）。圈闭在首尾两个可聚焦元素之间回绕。
  function trapFocus(event: React.KeyboardEvent<HTMLFormElement>) {
    if (event.key !== "Tab") return;
    const focusable = dialogRef.current?.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    if (!focusable || focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  if (!open) {
    return null;
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) {
      return;
    }
    const trimmed = reason.trim();
    if (level !== "standard" && !trimmed) {
      setLocalError("请填写操作原因");
      return;
    }
    if (level === "reasonAndAck" && !acknowledged) {
      setLocalError("请先勾选确认操作");
      return;
    }
    setLocalError("");
    onConfirm(trimmed);
  }

  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: 遮罩是 aria-modal dialog 模式的标准视觉层，键盘经 Esc 与对话框内控件交互
    <div
      className="admin-dialog-overlay"
      onClick={() => {
        if (!busy) {
          onClose();
        }
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape" && !busy) {
          onClose();
        }
      }}
      role="presentation"
    >
      <form
        aria-modal="true"
        className="admin-dialog"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          event.stopPropagation();
          trapFocus(event);
        }}
        ref={dialogRef}
        role="dialog"
        aria-label={title}
        onSubmit={submit}
      >
        <h2>{title}</h2>
        {description ? (
          <p className="admin-hint admin-dialog__description">{description}</p>
        ) : null}
        {level !== "standard" ? (
          <label>
            操作原因
            <input
              placeholder="请填写可审计的操作原因"
              ref={reasonInputRef}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </label>
        ) : null}
        {level === "reasonAndAck" ? (
          <label className="admin-dialog__ack">
            <input
              checked={acknowledged}
              type="checkbox"
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            我已知晓该操作的影响
          </label>
        ) : null}
        {error || localError ? (
          <p className="settings-error" role="alert">
            {localError || error}
          </p>
        ) : null}
        <div className="admin-actions">
          <button disabled={busy} ref={confirmButtonRef} type="submit">
            {confirmLabel}
          </button>
          <button disabled={busy} type="button" onClick={onClose}>
            取消
          </button>
        </div>
      </form>
    </div>
  );
}
