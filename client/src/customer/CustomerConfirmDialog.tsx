import {
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

// 客户泳道的高危操作确认框。
//
// 与管理端 ConfirmDialog 是同一份契约精神——不可逆的动作不能只剩一次点击——
// 但只保留客户真正需要的两级：
// - standard：说明性确认（影响可控或可恢复，如关闭待支付订单）；
// - acknowledge：说明 + 勾选「我已知晓该操作不可撤销」（不可逆，如退出所有设备、
//   撤销全部 Token、解绑设备、删除子账号）。
// 管理端还有一级 reason（原因必填），那是给运营留可审计理由用的；客户没有对账
// 义务，强制填原因只会产出垃圾数据，所以客户泳道刻意不设该级。
//
// 用原生 <dialog>：Esc 与焦点陷阱由浏览器给，jsdom 没有 showModal 时退回
// setAttribute("open")（与 CustomerCenterPage 既有弹窗同一处理）。
// 刻意不做遮罩点击关闭——危险动作需要一次明确的选择，误触不该让它消失。
export type CustomerConfirmLevel = "standard" | "acknowledge";

export type CustomerConfirmRequest = {
  title: string;
  description?: ReactNode;
  level?: CustomerConfirmLevel;
  confirmLabel?: string;
  onConfirm: () => void | Promise<void>;
};

export function CustomerConfirmDialog({
  request,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  request: CustomerConfirmRequest | null;
  busy: boolean;
  error: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const ackRef = useRef<HTMLInputElement | null>(null);
  const confirmRef = useRef<HTMLButtonElement | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [localError, setLocalError] = useState("");
  const level = request?.level ?? "standard";
  const needsAck = level === "acknowledge";

  useEffect(() => {
    const element = dialog.current;
    if (!request || !element) return;
    // 每次打开都是一次新的确认：清掉上一次的勾选与提示。
    setAcknowledged(false);
    setLocalError("");
    if (!element.open) {
      if (typeof element.showModal === "function") element.showModal();
      else element.setAttribute("open", "");
    }
    // 焦点落在用户下一步该动的地方：先勾选，再确认。
    (needsAck ? ackRef.current : confirmRef.current)?.focus();
    return () => {
      if (element.open) element.close?.();
    };
  }, [needsAck, request]);

  if (!request) {
    return null;
  }

  return (
    <dialog
      aria-label={request.title}
      className="uc-dialog uc-confirm"
      onCancel={(event) => {
        // Esc：执行中不放行，避免把一次不可逆动作关到一半。
        if (busy) event.preventDefault();
        else onClose();
      }}
      ref={dialog}
    >
      <h2>{request.title}</h2>
      {request.description ? <p>{request.description}</p> : null}
      {needsAck ? (
        <label className="uc-confirm__ack">
          <input
            checked={acknowledged}
            onChange={(event) => {
              setAcknowledged(event.target.checked);
              setLocalError("");
            }}
            ref={ackRef}
            type="checkbox"
          />
          我已知晓该操作不可撤销
        </label>
      ) : null}
      {localError || error ? (
        <p className="uc-error" role="alert">
          {localError || error}
        </p>
      ) : null}
      <div className="uc-dialog-actions">
        <button
          className="uc-primary"
          disabled={busy}
          onClick={() => {
            if (busy) return;
            if (needsAck && !acknowledged) {
              setLocalError("请先勾选确认操作");
              return;
            }
            onConfirm();
          }}
          ref={confirmRef}
          type="button"
        >
          {busy ? "正在执行…" : (request.confirmLabel ?? "确认执行")}
        </button>
        <button disabled={busy} onClick={onClose} type="button">
          取消
        </button>
      </div>
    </dialog>
  );
}

/**
 * 把「提问 → 等待 → 成功关闭 / 失败留在框内」的样板收敛成一处。
 *
 * 调用方只在真要执行危险动作时调用 ``confirm``；异步失败的信息显示在框内
 * （而不是页面上），用户能直接重试或取消。
 */
export function useCustomerConfirm() {
  const [request, setRequest] = useState<CustomerConfirmRequest | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const action = useRef<(() => void | Promise<void>) | null>(null);

  const confirm = useCallback((next: CustomerConfirmRequest) => {
    action.current = next.onConfirm;
    setError("");
    setBusy(false);
    setRequest(next);
  }, []);

  const close = useCallback(() => {
    action.current = null;
    setError("");
    setRequest(null);
  }, []);

  const submit = useCallback(() => {
    const run = action.current;
    if (!run) return;
    setBusy(true);
    setError("");
    void Promise.resolve()
      .then(run)
      .then(() => {
        setBusy(false);
        close();
      })
      .catch((cause: unknown) => {
        setBusy(false);
        setError(
          cause instanceof Error ? cause.message : "操作失败，请稍后重试。",
        );
      });
  }, [close]);

  return {
    confirm,
    dialog: (
      <CustomerConfirmDialog
        busy={busy}
        error={error}
        onClose={close}
        onConfirm={submit}
        request={request}
      />
    ),
  };
}
