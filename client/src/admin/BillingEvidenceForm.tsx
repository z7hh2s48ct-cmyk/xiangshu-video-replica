import { useRef, useState } from "react";
import { adminWrite } from "../api.admin";

export function BillingEvidenceForm({
  operationId,
  attemptId,
  onSaved,
}: {
  operationId: string;
  attemptId?: string;
  onSaved: () => void;
}) {
  const [value, setValue] = useState("");
  const [reference, setReference] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const saving = useRef(false);
  const retry = useRef<{ fingerprint: string; key: string } | undefined>(
    undefined,
  );
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (saving.current) return;
    if (!value || !reference.trim() || !reason.trim()) return;
    const payload = {
      operation_id: operationId,
      ...(attemptId
        ? { attempt_id: attemptId, cost_fen: value }
        : { units: value }),
      reference: reference.trim(),
    };
    const fingerprint = JSON.stringify([payload, reason]);
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    saving.current = true;
    setBusy(true);
    setError("");
    try {
      await adminWrite(
        "/api/control/billing/evidence",
        payload,
        reason.trim(),
        "保存核对依据失败",
        retry.current.key,
      );
      onSaved();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存失败");
    } finally {
      saving.current = false;
      setBusy(false);
    }
  }
  return (
    <form
      onSubmit={submit}
      aria-label={attemptId ? "核对调用成本" : "核对成功时长"}
    >
      <p>
        {attemptId
          ? "按供应商账单补录本次调用总成本，原始调用记录保留。"
          : "仅可为已成功交付且缺少时长的任务补录实际时长；按受理时售价结算，最多扣预留积分。"}
      </p>
      <label>
        {attemptId ? "本次调用总成本（分）" : "实际成功时长（秒）"}
        <input
          type="number"
          min={attemptId ? "0" : "0.000001"}
          step={attemptId ? "0.00000001" : "0.000001"}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          required
        />
      </label>
      <label>
        核对凭据
        <input
          value={reference}
          onChange={(e) => setReference(e.target.value)}
          placeholder="供应商账单号或媒体检测记录"
          maxLength={500}
          required
        />
      </label>
      <label>
        核对原因
        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          required
        />
      </label>
      <button type="submit" disabled={busy}>
        确认并保存核对记录
      </button>
      {error && <p role="alert">{error}</p>}
    </form>
  );
}
