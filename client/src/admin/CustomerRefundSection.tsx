import { type FormEvent, useRef, useState } from "react";

import {
  type AdjustmentWriteResult,
  AdminActivationError,
  createCustomerAdjustment,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { ADJUSTMENT_SOURCE_LABELS, labelFrom } from "./ui/vocabulary";

/** 与服务端 REVERSAL_SOURCE_DOCUMENT_TYPES 同集合：只有这两类来源允许负数调账。 */
const REFUND_SOURCES = ["REFUND_APPROVAL", "LEDGER_CORRECTION"] as const;

type PendingRefund = {
  credits: number;
  sourceType: string;
  sourceRef: string;
  reason: string;
};

/**
 * 退款扣减（反向调账）：从客户可用积分里扣回，实际退款在线下办理。
 *
 * 原先只挂在「会话与设备」页、还要手输客户 ID，运营找不到入口；资金操作
 * 统一收进客户详情（方案 P0-2）。金额上限是当前可用余额——已消耗的积分
 * 不在账本内退回，服务端超额同样拒绝，前端先拦住免得白填原因。
 */
export function CustomerRefundSection({
  userId,
  availableCredits,
  readOnly,
  onRefunded,
}: {
  userId: string;
  availableCredits: number;
  readOnly: boolean;
  onRefunded: (result: AdjustmentWriteResult) => void;
}) {
  const [credits, setCredits] = useState("");
  const [sourceType, setSourceType] = useState<string>("REFUND_APPROVAL");
  const [sourceRef, setSourceRef] = useState("");
  const [reason, setReason] = useState("");
  const [formError, setFormError] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [notice, setNotice] = useState("");
  const [pending, setPending] = useState<PendingRefund | null>(null);
  const [busy, setBusy] = useState(false);
  // 结果不确定（超时 / 5xx / 409 占位）时必须复用同一幂等键重试，否则一笔
  // 退款可能被扣两次；内容一变就换新键。
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);

  if (readOnly) {
    return (
      <section aria-label="退款扣减" className="customer-detail-section">
        <h3>退款扣减</h3>
        <p className="admin-hint">审计员仅可查看，不能扣减积分。</p>
      </section>
    );
  }

  function requestRefund(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const amount = Number(credits);
    if (!Number.isSafeInteger(amount) || amount <= 0) {
      setFormError("扣减积分必须是大于 0 的整数");
      return;
    }
    if (amount > availableCredits) {
      setFormError(
        `超过当前可用余额，最多可扣减 ${availableCredits} 积分；已消耗的积分不在账本内退回`,
      );
      return;
    }
    if (!sourceRef.trim()) {
      setFormError("请填写审批单号，用于和线下退款记录对齐");
      return;
    }
    if (!reason.trim()) {
      setFormError("请填写原因");
      return;
    }
    setFormError("");
    setDialogError("");
    setPending({
      credits: amount,
      sourceType,
      sourceRef: sourceRef.trim(),
      reason: reason.trim(),
    });
  }

  async function submitRefund() {
    if (!pending || busy) return;
    const fingerprint = JSON.stringify({ userId, ...pending });
    if (retry.current?.fingerprint !== fingerprint) {
      retry.current = { fingerprint, key: crypto.randomUUID() };
    }
    setBusy(true);
    setDialogError("");
    try {
      const result = await createCustomerAdjustment(
        userId,
        {
          sourceDocumentType: pending.sourceType,
          sourceDocumentRef: pending.sourceRef,
          credits: -pending.credits,
        },
        pending.reason,
        retry.current.key,
      );
      retry.current = null;
      setNotice(
        `已扣减 ${pending.credits} 积分，余额 ${result.wallet_balance_after} 积分（request id: ${result.request_id}）。实际退款请线下办理，并用审批单号 ${pending.sourceRef} 对齐。`,
      );
      setCredits("");
      setSourceRef("");
      setReason("");
      setPending(null);
      onRefunded(result);
    } catch (cause) {
      const message =
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "退款扣减失败";
      const deterministic =
        cause instanceof AdminActivationError &&
        cause.status !== undefined &&
        cause.status >= 400 &&
        cause.status < 500 &&
        ![408, 409, 429].includes(cause.status);
      if (deterministic) {
        retry.current = null;
        setPending(null);
        setFormError(message);
      } else {
        setDialogError(
          `${message}。结果未确认：请先核对积分流水，再决定是否重试（重试不会重复扣减）。`,
        );
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <section
      aria-label="退款扣减"
      className="customer-detail-section"
      id="customer-refund"
    >
      <h3>退款扣减</h3>
      <p className="admin-hint">
        从可用积分中扣回（当前最多 {availableCredits}{" "}
        积分）。系统只记账本，实际退款请线下办理。
      </p>
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {formError ? <PageBanner tone="error">{formError}</PageBanner> : null}
      {/* noValidate：超额、缺单号都由下方中文提示说明原因，不交给浏览器气泡。 */}
      <form className="admin-form" noValidate onSubmit={requestRefund}>
        <label>
          依据
          <select
            disabled={busy}
            value={sourceType}
            onChange={(event) => setSourceType(event.target.value)}
          >
            {REFUND_SOURCES.map((value) => (
              <option key={value} value={value}>
                {labelFrom(ADJUSTMENT_SOURCE_LABELS, value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          扣减积分
          <input
            disabled={busy}
            max={availableCredits}
            min={1}
            step={1}
            type="number"
            value={credits}
            onChange={(event) => setCredits(event.target.value)}
          />
        </label>
        <label>
          审批单号
          <input
            disabled={busy}
            placeholder="例如：RF-0928-01"
            value={sourceRef}
            onChange={(event) => setSourceRef(event.target.value)}
          />
        </label>
        <label>
          原因
          <input
            disabled={busy}
            placeholder="例如：客户申请退还未用积分"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <button disabled={busy} type="submit">
          提交退款扣减
        </button>
      </form>

      <ConfirmDialog
        busy={busy}
        confirmLabel="确认扣减"
        description={
          pending ? (
            <>
              即将从该客户可用积分中扣减 {pending.credits} 积分。
              <br />
              依据：{labelFrom(ADJUSTMENT_SOURCE_LABELS, pending.sourceType)}（
              {pending.sourceRef}）
              <br />
              原因：{pending.reason}
            </>
          ) : null
        }
        error={dialogError}
        level="standard"
        open={pending !== null}
        title="确认退款扣减"
        onClose={() => {
          if (busy) return;
          setPending(null);
          setDialogError("");
        }}
        onConfirm={() => void submitRefund()}
      />
    </section>
  );
}
