import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  AdminSessionError,
  type CustomerSessionListItem,
  listCustomerSessions,
  listLiveSessions,
  revokeCustomerSession,
} from "../api.admin";
import "./admin-sessions.css";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { platformLabel } from "./ui/vocabulary";

const PAGE_SIZE = 50;

// 平台名一律走共享词典（ui/vocabulary.ts 的 PLATFORM_LABELS）。
// 后台调账（含反向扣减）已迁到客户详情（方案 P0-2）：资金操作放在会话页既难找，
// 又要求手输客户 ID，本页只保留会话查看与强制下线。

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
    setItems([]);
    setTotal(0);
    setOffset(0);
    setError("");
    setNotice("");
    setPendingRevoke(null);
    setRevokeKey(null);
    setRevokeError("");
    setRevoking(false);
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
    setNotice("");
    void load(null, 0);
  }

  function selectCustomer(item: CustomerSessionListItem) {
    if (onCustomerChange) {
      onCustomerChange(item.user_id);
      return;
    }
    // 只看该客户的会话；调账已迁到客户详情，这里不再承担资金入口。
    contextIdRef.current += 1;
    setQueryUserId(item.user_id);
    setViewUserId(item.user_id);
    setNotice("");
    void load(item.user_id, 0);
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
    </section>
  );
}
