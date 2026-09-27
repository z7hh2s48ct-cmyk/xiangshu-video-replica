import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type AdjustmentWriteInput,
  AdminSessionError,
  type CustomerSessionListItem,
  createCustomerAdjustment,
  listCustomerSessions,
  listLiveSessions,
  revokeCustomerSession,
} from "../api.admin";
import "./admin-sessions.css";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import {
  ADJUSTMENT_SOURCE_LABELS,
  labelFrom,
  platformLabel,
} from "./ui/vocabulary";

const PAGE_SIZE = 50;
const SOURCE_DOCUMENT_OPTIONS = [
  "CS_TICKET",
  "FREE_GRANT",
  "COMPENSATION_APPROVAL",
  "REFUND_APPROVAL",
  "LEDGER_CORRECTION",
  "OFFLINE_PAYMENT",
] as const;

// 平台名一律走共享词典（ui/vocabulary.ts 的 PLATFORM_LABELS）。这里曾私刻一份
// 且只列了 windows/macos/linux，ios/android 落到 `?? platform` 兜底 —— 运营
// 看到的是英文码 "ios"/"android" 而非 "iOS"/"Android"（2026-09-12 评审 P3
// 记载的 platformLabel 与词典分叉）。合并 origin/main 的 B1（#216）时该副本
// 一并删除：共享词典是它的严格超集，留着会与顶部 import 撞名。
/**
 * B1（SOP §10 第 2 步）：这几个来源单类型可以做**反向调账**（负数）。
 *
 * 与服务端 `REVERSAL_SOURCE_DOCUMENT_TYPES` 同集合——服务端仍是权威，这里只决定
 * 输入框要不要挡住负号，以及给运营看什么提示。其余来源类型维持正向（>= 1）。
 */
const REVERSAL_SOURCE_TYPES = new Set(["REFUND_APPROVAL", "LEDGER_CORRECTION"]);

function secondsBetween(later: string | number, earlier: string | number) {
  return Math.max(
    0,
    Math.ceil((new Date(later).getTime() - new Date(earlier).getTime()) / 1000),
  );
}

function leaseState(item: CustomerSessionListItem, now: number) {
  const remaining = secondsBetween(item.lease_until, now);
  const duration = Math.max(
    1,
    secondsBetween(item.lease_until, item.last_heartbeat_at),
  );
  const percent = Math.round(Math.min(1, remaining / duration) * 100);
  const heartbeatAgo = secondsBetween(now, item.last_heartbeat_at);
  return { heartbeatAgo, percent, remaining };
}

export function SessionsPage({
  userId,
  readOnly = false,
  onCustomerChange,
}: {
  userId?: string;
  readOnly?: boolean;
  onCustomerChange?: (userId: string | undefined) => void;
}) {
  const [queryUserId, setQueryUserId] = useState(userId ?? "");
  const [viewUserId, setViewUserId] = useState<string | null>(userId ?? null);
  const [activeUserId, setActiveUserId] = useState<string | null>(
    userId ?? null,
  );
  const [items, setItems] = useState<CustomerSessionListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [now, setNow] = useState(Date.now());

  const [pendingRevoke, setPendingRevoke] =
    useState<CustomerSessionListItem | null>(null);
  const [revokeKey, setRevokeKey] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState("");
  const [revoking, setRevoking] = useState(false);

  const [adjustOpen, setAdjustOpen] = useState(false);
  const [adjustKey, setAdjustKey] = useState<string | null>(null);
  const [credits, setCredits] = useState("");
  const [sourceType, setSourceType] = useState<string>("CS_TICKET");
  const [adjustReason, setAdjustReason] = useState("");
  const [adjustConfirmOpen, setAdjustConfirmOpen] = useState(false);
  const [writeError, setWriteError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const requestIdRef = useRef(0);
  const contextIdRef = useRef(0);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const load = useCallback(
    async (targetUserId: string | null, nextOffset = 0) => {
      const requestId = requestIdRef.current + 1;
      requestIdRef.current = requestId;
      setLoading(true);
      setError("");
      try {
        const response = targetUserId
          ? await listCustomerSessions(targetUserId, { limit: PAGE_SIZE })
          : await listLiveSessions({ limit: PAGE_SIZE, offset: nextOffset });
        if (requestId !== requestIdRef.current) {
          return;
        }
        setItems(response.items);
        setTotal(response.total);
        setOffset(nextOffset);
      } catch (cause) {
        if (requestId !== requestIdRef.current) {
          return;
        }
        setError(
          cause instanceof Error && cause.message
            ? `加载失败：${cause.message}`
            : "加载失败：未知错误",
        );
      } finally {
        if (requestId === requestIdRef.current) {
          setLoading(false);
        }
      }
    },
    [],
  );

  useEffect(() => {
    contextIdRef.current += 1;
    requestIdRef.current += 1;
    setQueryUserId(userId ?? "");
    setViewUserId(userId ?? null);
    setActiveUserId(userId ?? null);
    setItems([]);
    setTotal(0);
    setOffset(0);
    setError("");
    setNotice("");
    setPendingRevoke(null);
    setRevokeKey(null);
    setRevokeError("");
    setRevoking(false);
    setAdjustOpen(false);
    setAdjustKey(null);
    setCredits("");
    setSourceType("CS_TICKET");
    setAdjustReason("");
    setAdjustConfirmOpen(false);
    setWriteError("");
    setSubmitting(false);
    void load(userId ?? null, 0);
  }, [load, userId]);

  function handleQuery(event: FormEvent) {
    event.preventDefault();
    const target = queryUserId.trim();
    if (!target) return;
    if (onCustomerChange) {
      onCustomerChange(target);
      return;
    }
    contextIdRef.current += 1;
    setViewUserId(target);
    setActiveUserId(target);
    setAdjustOpen(false);
    setNotice("");
    void load(target, 0);
  }

  function showAllLive() {
    if (onCustomerChange) {
      onCustomerChange(undefined);
      return;
    }
    contextIdRef.current += 1;
    setViewUserId(null);
    setActiveUserId(null);
    setAdjustOpen(false);
    setNotice("");
    void load(null, 0);
  }

  function selectCustomer(item: CustomerSessionListItem) {
    if (onCustomerChange) {
      onCustomerChange(item.user_id);
      return;
    }
    contextIdRef.current += 1;
    setActiveUserId(item.user_id);
    setQueryUserId(item.user_id);
    setAdjustOpen(false);
  }

  function beginRevoke(item: CustomerSessionListItem) {
    setPendingRevoke(item);
    setRevokeKey(null);
    setRevokeError("");
  }

  async function submitRevoke(reason: string) {
    if (!pendingRevoke || revoking) return;
    const key = revokeKey ?? crypto.randomUUID();
    setRevokeKey(key);
    setRevoking(true);
    const actionContextId = contextIdRef.current;
    try {
      await revokeCustomerSession(
        pendingRevoke.session_id,
        pendingRevoke.session_epoch,
        reason,
        key,
      );
      if (contextIdRef.current !== actionContextId) return;
      setNotice(`已强制下线 ${pendingRevoke.username}，会话状态已刷新。`);
      setPendingRevoke(null);
      setRevokeKey(null);
      await load(viewUserId, offset);
    } catch (cause) {
      if (contextIdRef.current !== actionContextId) return;
      setRevokeError(
        cause instanceof Error ? cause.message : "结束会话失败：未知错误",
      );
      if (cause instanceof AdminSessionError && cause.status !== undefined) {
        setRevokeKey(null);
      }
    } finally {
      if (contextIdRef.current === actionContextId) {
        setRevoking(false);
      }
    }
  }

  function handleAdjustSubmit(event: FormEvent) {
    event.preventDefault();
    setWriteError("");
    setError("");
    const seconds = Number(credits);
    const isReversal = REVERSAL_SOURCE_TYPES.has(sourceType);
    if (!Number.isSafeInteger(seconds) || seconds === 0) {
      setError(
        isReversal ? "请输入非 0 的积分整数" : "请输入大于 0 的积分整数",
      );
      return;
    }
    if (seconds < 0 && !isReversal) {
      setError(
        "该来源单类型只能正向加积分；反向调账请选择「退款审批」或「账本修正」",
      );
      return;
    }
    if (!adjustReason.trim()) {
      setError("请填写事由");
      return;
    }
    setAdjustKey((key) => key ?? crypto.randomUUID());
    setAdjustConfirmOpen(true);
  }

  async function submitAdjustment() {
    if (!activeUserId || submitting || readOnly || !adjustKey) return;
    const seconds = Number(credits);
    const input: AdjustmentWriteInput = {
      sourceDocumentType: sourceType,
      sourceDocumentRef: `GRANT-${adjustKey}`,
      credits: seconds,
    };
    const key = adjustKey;
    setAdjustKey(key);
    setSubmitting(true);
    const actionContextId = contextIdRef.current;
    try {
      const result = await createCustomerAdjustment(
        activeUserId,
        input,
        adjustReason.trim(),
        key,
      );
      if (contextIdRef.current !== actionContextId) return;
      setNotice(
        seconds < 0
          ? `反向调账成功（request id: ${result.request_id}），余额 ${result.wallet_balance_after} 积分；实际退付请在 ZPay 后台办理并以来源单号对齐`
          : `增加积分成功（request id: ${result.request_id}），余额 ${result.wallet_balance_after} 积分`,
      );
      setCredits("");
      setAdjustReason("");
      setAdjustKey(null);
      setAdjustConfirmOpen(false);
      await load(viewUserId, offset);
    } catch (cause) {
      if (contextIdRef.current !== actionContextId) return;
      setWriteError(
        cause instanceof Error
          ? cause.message
          : seconds < 0
            ? "反向调账失败：未知错误"
            : "增加积分失败：未知错误",
      );
    } finally {
      if (contextIdRef.current === actionContextId) {
        setSubmitting(false);
      }
    }
  }

  // 来源类型只决定「允许不允许输入负数」；钱往哪个方向走由输入符号决定——
  // 退款审批来源 + 正数 = 服务端走正向加款通道，弹窗必须说「增加积分」。
  // 方向口径按符号判定与 submitAdjustment 成功通知（seconds < 0）同源，
  // 避免运营确认的方向与账本实际方向相反（上线前检查 P1-2）。
  const isReversalSource = REVERSAL_SOURCE_TYPES.has(sourceType);
  const isReversalInput = Number(credits) < 0;

  return (
    <section aria-label="客户会话" className="admin-sessions admin-panel">
      <header className="admin-sessions__header">
        <div>
          <h2>在线会话</h2>
          <p>实时查看客户租约，并在必要时强制结束当前会话。</p>
        </div>
        <span className="admin-sessions__count">{total} 个在线</span>
      </header>

      {!userId ? (
        <form className="admin-sessions__toolbar" onSubmit={handleQuery}>
          <label>
            <span>客户 ID</span>
            <input
              placeholder="输入客户 ID"
              value={queryUserId}
              onChange={(event) => setQueryUserId(event.target.value)}
            />
          </label>
          <button disabled={loading} type="submit">
            查看客户
          </button>
          <button disabled={loading} type="button" onClick={showAllLive}>
            全部在线
          </button>
        </form>
      ) : null}

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {loading ? <div className="loading">加载中...</div> : null}
      {!loading && !error && items.length === 0 ? (
        <div className="empty-state">当前没有活动会话</div>
      ) : null}

      {!loading && items.length > 0 ? (
        <ul aria-label="在线会话列表" className="admin-sessions__list">
          {items.map((item) => {
            const lease = leaseState(item, now);
            return (
              <li className="admin-sessions__card" key={item.session_id}>
                <div className="admin-sessions__identity">
                  <span aria-hidden="true" className="admin-sessions__avatar">
                    {item.username.slice(0, 1).toUpperCase()}
                  </span>
                  <div>
                    <strong>{item.username}</strong>
                    <span>{platformLabel(item.platform)}</span>
                  </div>
                </div>
                <div className="admin-sessions__lease-meta">
                  <span>租约剩余 {lease.remaining} 秒</span>
                  <span>心跳 {lease.heartbeatAgo} 秒前</span>
                  <span>Epoch {item.session_epoch}</span>
                </div>
                <div
                  aria-label={`${item.username} 租约剩余`}
                  aria-valuemax={100}
                  aria-valuemin={0}
                  aria-valuenow={lease.percent}
                  className="admin-sessions__progress"
                  role="progressbar"
                >
                  <span style={{ width: `${lease.percent}%` }} />
                </div>
                <div className="admin-sessions__actions">
                  {!userId ? (
                    <button type="button" onClick={() => selectCustomer(item)}>
                      选择客户
                    </button>
                  ) : null}
                  {!readOnly ? (
                    <button
                      aria-label={`强制下线 ${item.username}`}
                      className="admin-sessions__revoke"
                      type="button"
                      onClick={() => beginRevoke(item)}
                    >
                      强制下线
                    </button>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}

      {!viewUserId && total > PAGE_SIZE ? (
        <Pagination
          disabled={loading}
          limit={PAGE_SIZE}
          offset={offset}
          total={total}
          onPageChange={(next) => void load(null, next)}
        />
      ) : null}

      {activeUserId && !readOnly ? (
        <section className="admin-sessions__adjustment">
          <button
            aria-expanded={adjustOpen}
            className="admin-sessions__adjust-toggle"
            type="button"
            onClick={() => setAdjustOpen((open) => !open)}
          >
            {adjustOpen ? "收起后台调账" : "展开后台调账"}
          </button>
          {adjustOpen ? (
            <form className="admin-form" onSubmit={handleAdjustSubmit}>
              <h3>
                {isReversalInput
                  ? `为 ${activeUserId} 反向调账（扣减积分）`
                  : `为 ${activeUserId} 后台增加积分`}
              </h3>
              <label>
                积分整数
                <input
                  disabled={adjustConfirmOpen}
                  min={isReversalSource ? undefined : 1}
                  step={1}
                  type="number"
                  value={credits}
                  onChange={(event) => {
                    setCredits(event.target.value);
                    setAdjustKey(null);
                  }}
                />
              </label>
              <label>
                来源单类型
                <select
                  disabled={adjustConfirmOpen}
                  value={sourceType}
                  onChange={(event) => {
                    setSourceType(event.target.value);
                    setAdjustKey(null);
                  }}
                >
                  {SOURCE_DOCUMENT_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {labelFrom(ADJUSTMENT_SOURCE_LABELS, option)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                事由
                <input
                  disabled={adjustConfirmOpen}
                  value={adjustReason}
                  onChange={(event) => {
                    setAdjustReason(event.target.value);
                    setAdjustKey(null);
                  }}
                />
              </label>
              {isReversalSource ? (
                <p className="admin-hint">
                  此为账本反向记账，实际退付在 ZPay
                  后台办理（两笔事实以来源单号对齐）。
                  退款额度以客户当前可用余额为限：已消耗的部分不在账本内退回，
                  超额提交会被拒绝。
                </p>
              ) : (
                <p className="admin-hint">来源单号自动生成，事由留痕审计。</p>
              )}
              <button type="submit" disabled={adjustConfirmOpen}>
                {isReversalInput ? "执行反向调账" : "执行后台增加积分"}
              </button>
            </form>
          ) : null}
        </section>
      ) : null}

      <ConfirmDialog
        busy={revoking}
        confirmLabel="确认强制下线"
        description={
          pendingRevoke
            ? `将结束 ${pendingRevoke.username} 的当前登录会话。`
            : null
        }
        error={revokeError}
        level="reason"
        open={pendingRevoke !== null && !readOnly}
        title="确认强制下线"
        onClose={() => {
          setPendingRevoke(null);
          setRevokeError("");
          setRevokeKey(null);
        }}
        onConfirm={(reason) => void submitRevoke(reason)}
      />
      <ConfirmDialog
        busy={submitting}
        confirmLabel={isReversalInput ? "确认反向调账" : "确认增加积分"}
        description={
          <>
            {isReversalInput
              ? `即将为 ${activeUserId} 反向调账 ${Math.abs(Number(credits))} 积分（账本扣减）。`
              : `即将为 ${activeUserId} 增加 ${credits} 积分。`}
            <br />
            事由：{adjustReason.trim()}
            <br />
            来源单号：GRANT-{adjustKey}
            {isReversalInput ? (
              <>
                <br />
                实际退付在 ZPay 后台办理，以该来源单号对齐留档。
              </>
            ) : null}
          </>
        }
        error={writeError}
        level="standard"
        open={adjustConfirmOpen}
        title={isReversalInput ? "确认反向调账" : "确认后台增加积分"}
        onClose={() => {
          setAdjustConfirmOpen(false);
          setWriteError("");
        }}
        onConfirm={() => void submitAdjustment()}
      />
    </section>
  );
}
