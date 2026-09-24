import { useEffect, useRef, useState } from "react";
import {
  type AccountCreditSummary,
  getAccountCreditSummary,
  reconcileAccountRecharge,
} from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";

export function AccountCreditPanel({
  userId,
  onChanged,
  readOnly = false,
}: {
  userId: string;
  onChanged?: () => void;
  readOnly?: boolean;
}) {
  const [data, setData] = useState<AccountCreditSummary | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [order, setOrder] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const retry = useRef<{ fingerprint: string; key: string } | null>(null);
  useEffect(() => {
    void refresh;
    let active = true;
    setData(null);
    setError("");
    void getAccountCreditSummary(userId)
      .then((value) => {
        if (active) setData(value);
      })
      .catch((cause) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "账号查账加载失败");
      });
    return () => {
      active = false;
    };
  }, [userId, refresh]);
  async function reconcile(reason: string) {
    if (busy) return;
    const fingerprint = JSON.stringify({ userId, order: order.trim(), reason });
    if (retry.current?.fingerprint !== fingerprint)
      retry.current = { fingerprint, key: crypto.randomUUID() };
    setBusy(true);
    setError("");
    try {
      await reconcileAccountRecharge(
        userId,
        order.trim(),
        reason,
        retry.current.key,
      );
      onChanged?.();
      setNotice("原订单已核验到账。");
      setConfirm(false);
      retry.current = null;
      setRefresh((value) => value + 1);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "订单核验失败");
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="customer-detail-section" aria-label="账号积分查账">
      <details className="customer-account-tools">
        <summary>充值核验与接口用量</summary>
        {error && (
          <PageBanner tone="error">
            {error}
            <button
              type="button"
              onClick={() => setRefresh((value) => value + 1)}
            >
              重新加载
            </button>
          </PageBanner>
        )}
        {notice && <PageBanner tone="notice">{notice}</PageBanner>}
        {!data && !error && <p role="status">正在加载账号账目…</p>}
        {data && data.tokens.length > 0 && (
          <div className="admin-table-wrap">
            <table className="admin-data-table">
              <thead>
                <tr>
                  <th>Token 名称</th>
                  <th>凭据版本</th>
                  <th>状态</th>
                  <th>累计消费</th>
                </tr>
              </thead>
              <tbody>
                {data.tokens.map((token) => (
                  <tr key={token.id}>
                    <td>{token.label || "未命名 Token"}</td>
                    <td>V{token.credential_version}</td>
                    <td>{token.revoked_at ? "已撤销" : "有效"}</td>
                    <td>{token.total_consumed_credits} 积分</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {!readOnly && (
          <form
            className="admin-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (order.trim()) {
                setError("");
                setConfirm(true);
              }
            }}
          >
            <label>
              原商户订单号
              <input
                value={order}
                required
                disabled={busy}
                onChange={(event) => setOrder(event.target.value)}
              />
            </label>
            <button type="submit" disabled={busy}>
              核验原订单并补发
            </button>
          </form>
        )}
      </details>
      <ConfirmDialog
        open={confirm}
        busy={busy}
        title="核验原充值订单"
        description={`核验订单 ${order}，到账积分以原订单为准。`}
        confirmLabel="确认核验"
        level="reasonAndAck"
        error={error}
        onClose={() => {
          if (!busy) setConfirm(false);
        }}
        onConfirm={(reason) => void reconcile(reason)}
      />
    </section>
  );
}
