import { type FormEvent, useEffect, useRef, useState } from "react";
import {
  type AdjustmentWriteResult,
  AdminActivationError,
  createCustomerAdjustment,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import "./admin-customer-detail.css";

type PendingGrantIntent = {
  key: string;
  operatorId: string;
  userId: string;
  credits: number;
  sourceType: string;
  sourceRef: string;
  reason: string;
  uncertain: boolean;
  attemptId: string | null;
};

const PENDING_GRANT_STORAGE_PREFIX = "video-replica:admin-free-grant:v1:";

function pendingGrantStorageKey(operatorId: string, userId: string): string {
  return `${PENDING_GRANT_STORAGE_PREFIX}${encodeURIComponent(operatorId)}:${encodeURIComponent(userId)}`;
}

function readPendingGrant(
  operatorId: string,
  userId: string,
): PendingGrantIntent | null {
  const storageKey = pendingGrantStorageKey(operatorId, userId);
  if (typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(storageKey);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PendingGrantIntent>;
    if (
      value.operatorId !== operatorId ||
      value.userId !== userId ||
      value.uncertain !== true ||
      typeof value.key !== "string" ||
      !value.key ||
      !Number.isSafeInteger(value.credits) ||
      Number(value.credits) <= 0 ||
      Number(value.credits) > 2147483647 ||
      typeof value.sourceType !== "string" ||
      !value.sourceType ||
      typeof value.sourceRef !== "string" ||
      !value.sourceRef ||
      typeof value.reason !== "string" ||
      !value.reason ||
      typeof value.attemptId !== "string" ||
      !value.attemptId
    ) {
      window.sessionStorage.removeItem(storageKey);
      return null;
    }
    return value as PendingGrantIntent;
  } catch {
    return null;
  }
}

function persistPendingGrant(intent: PendingGrantIntent): boolean {
  const storageKey = pendingGrantStorageKey(intent.operatorId, intent.userId);
  if (typeof window === "undefined") return false;
  try {
    window.sessionStorage.setItem(storageKey, JSON.stringify(intent));
    return true;
  } catch {
    return false;
  }
}

function clearPendingGrant(operatorId: string, userId: string): boolean {
  const storageKey = pendingGrantStorageKey(operatorId, userId);
  if (typeof window === "undefined") return false;
  try {
    window.sessionStorage.removeItem(storageKey);
    return true;
  } catch {
    // Retaining a stale intent is safer than allowing a second idempotency key.
    return false;
  }
}

function clearPendingGrantForAttempt(
  operatorId: string,
  userId: string,
  key: string,
  attemptId: string,
): boolean {
  const current = readPendingGrant(operatorId, userId);
  if (current?.key !== key || current.attemptId !== attemptId) return false;
  return clearPendingGrant(operatorId, userId);
}

const REFERENCED_GRANT_SOURCES = [
  "CS_TICKET",
  "COMPENSATION_APPROVAL",
] as const;

function needsSourceRef(sourceType: string): boolean {
  return (REFERENCED_GRANT_SOURCES as readonly string[]).includes(sourceType);
}

export function FreeCreditsSection({
  userId,
  onGranted,
  operatorId,
  readOnly,
}: {
  userId: string;
  onGranted: (result: AdjustmentWriteResult) => void;
  operatorId: string;
  readOnly: boolean;
}) {
  const [credits, setCredits] = useState("");
  const [sourceType, setSourceType] = useState("FREE_GRANT");
  const [sourceRef, setSourceRef] = useState("");
  const [reason, setReason] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState("");
  const [notice, setNotice] = useState("");
  const [pendingGrant, setPendingGrant] = useState<PendingGrantIntent | null>(
    () => readPendingGrant(operatorId, userId),
  );
  const saving = useRef(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  function requestGrant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (readOnly || saving.current) return;
    if (pendingGrant) {
      setDialogError("");
      setDialogOpen(true);
      return;
    }
    const creditsNumber = Number(credits);
    if (
      !Number.isSafeInteger(creditsNumber) ||
      creditsNumber <= 0 ||
      creditsNumber > 2147483647
    ) {
      setDialogError("赠送积分必须是大于 0 的整数");
      return;
    }
    if (!reason.trim()) {
      setDialogError("请填写事由");
      return;
    }
    if (needsSourceRef(sourceType) && !sourceRef.trim()) {
      setDialogError(
        "请填写来源单号（工单号或审批单号），用于和客服与审批流程对齐",
      );
      return;
    }
    const key = crypto.randomUUID();
    setPendingGrant({
      key,
      operatorId,
      userId,
      credits: creditsNumber,
      sourceType,
      sourceRef: needsSourceRef(sourceType) ? sourceRef.trim() : `GRANT-${key}`,
      reason: reason.trim(),
      uncertain: false,
      attemptId: null,
    });
    setDialogError("");
    setDialogOpen(true);
  }

  async function submitGrant() {
    if (saving.current || readOnly || !pendingGrant) return;
    const intent = pendingGrant;
    const attemptId = crypto.randomUUID();
    const inFlightIntent: PendingGrantIntent = {
      ...intent,
      // A page reload cannot observe this request's eventual response, so the
      // durable copy must already be treated as uncertain before POST begins.
      uncertain: true,
      attemptId,
    };
    if (!persistPendingGrant(inFlightIntent)) {
      setDialogError(
        "无法安全保存待确认发放，本次请求尚未发送，请检查浏览器存储后重试",
      );
      return;
    }
    setPendingGrant(inFlightIntent);
    saving.current = true;
    setSubmitting(true);
    try {
      const result = await createCustomerAdjustment(
        userId,
        {
          sourceDocumentType: intent.sourceType,
          sourceDocumentRef: intent.sourceRef,
          credits: intent.credits,
        },
        intent.reason,
        intent.key,
      );
      clearPendingGrantForAttempt(operatorId, userId, intent.key, attemptId);
      if (!mounted.current) return;
      setNotice(
        `已发放 ${intent.credits} 赠送积分，余额 ${result.wallet_balance_after} 积分`,
      );
      onGranted(result);
      setCredits("");
      setSourceRef("");
      setReason("");
      setPendingGrant(null);
      setDialogOpen(false);
      setDialogError("");
    } catch (cause) {
      const definitivelyRejected =
        cause instanceof AdminActivationError &&
        cause.status !== undefined &&
        cause.status >= 400 &&
        cause.status < 500 &&
        ![408, 409, 429].includes(cause.status);
      if (definitivelyRejected && !intent.uncertain) {
        const cleared = clearPendingGrantForAttempt(
          operatorId,
          userId,
          intent.key,
          attemptId,
        );
        if (cleared && mounted.current) {
          setPendingGrant((current) =>
            current?.key === intent.key && current.attemptId === attemptId
              ? null
              : current,
          );
        }
      }
      if (mounted.current) {
        setDialogError(
          cause instanceof Error && cause.message.trim()
            ? cause.message
            : "发放赠送积分失败",
        );
      }
    } finally {
      saving.current = false;
      if (mounted.current) setSubmitting(false);
    }
  }

  if (readOnly) {
    return (
      <section aria-label="赠送积分" className="customer-detail-section">
        <h3>赠送积分</h3>
        <p className="admin-hint">审计员仅可查看，不能发放赠送积分。</p>
      </section>
    );
  }

  return (
    <section
      aria-label="赠送积分"
      className="customer-detail-section"
      id="customer-free-grant"
    >
      <h3>赠送积分</h3>
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {pendingGrant && !dialogOpen ? (
        <p className="admin-hint">
          上次发放结果尚未确认：{pendingGrant.credits} 积分，事由“
          {pendingGrant.reason}”。重试会使用同一来源单号与幂等键。
        </p>
      ) : null}
      {dialogError && !dialogOpen ? (
        <PageBanner tone="error">{dialogError}</PageBanner>
      ) : null}
      <form className="admin-form" onSubmit={requestGrant}>
        <label>
          积分来源
          <select
            disabled={dialogOpen || pendingGrant !== null}
            value={sourceType}
            onChange={(event) => setSourceType(event.target.value)}
          >
            <option value="FREE_GRANT">积分赠送</option>
            <option value="CREDIT_COMPENSATION">无收款补偿</option>
            <option value="CS_TICKET">客服工单补偿</option>
            <option value="COMPENSATION_APPROVAL">补偿审批</option>
          </select>
        </label>
        <label>
          发放积分
          <input
            disabled={dialogOpen || pendingGrant !== null}
            min={1}
            placeholder="例如：10"
            step={1}
            type="number"
            value={credits}
            onChange={(event) => setCredits(event.target.value)}
          />
        </label>
        {needsSourceRef(sourceType) ? (
          <label>
            来源单号
            <input
              disabled={dialogOpen || pendingGrant !== null}
              placeholder={
                sourceType === "CS_TICKET"
                  ? "例如：TICKET-20260928-001"
                  : "例如：COMP-20260928-001"
              }
              value={sourceRef}
              onChange={(event) => setSourceRef(event.target.value)}
            />
          </label>
        ) : null}
        <label>
          事由
          <input
            disabled={dialogOpen || pendingGrant !== null}
            placeholder="例如：新客赠送、活动奖励或售后补偿"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <button type="submit" disabled={dialogOpen}>
          {pendingGrant ? "重试确认上次发放" : "发放赠送积分"}
        </button>
      </form>

      <ConfirmDialog
        busy={submitting}
        confirmLabel="确认发放"
        description={
          <>
            即将发放 {pendingGrant?.credits ?? credits} 积分。
            <br />
            本次不产生收入；客户已付款的，请改用「开通套餐（已收款）」。
            <br />
            事由：{pendingGrant?.reason ?? reason.trim()}
            <br />
            来源单号：{pendingGrant?.sourceRef}
          </>
        }
        error={dialogError}
        level="standard"
        open={dialogOpen}
        title="发放赠送积分"
        onClose={() => {
          if (!pendingGrant?.uncertain) {
            clearPendingGrant(operatorId, userId);
            setPendingGrant(null);
          }
          setDialogOpen(false);
          setDialogError("");
        }}
        onConfirm={() => void submitGrant()}
      />
    </section>
  );
}
