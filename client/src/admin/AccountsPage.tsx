import { useCallback, useEffect, useRef, useState } from "react";
import { downloadControlWalletTransactionsCsv } from "../api";

import {
  type AdminWalletTransaction,
  listAdminWalletTransactions,
} from "../api.admin";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { StatusBadge } from "./ui/StatusBadge";
import { useAutoRefresh } from "./ui/useAutoRefresh";
import {
  formatDateTime,
  ledgerExportMessage,
  transactionTypeLabel,
} from "./ui/vocabulary";

const PAGE_SIZE = 20;

/**
 * 账号与钱包页（从 AdminApp 内联表格抽出，2026-09-02 评估 §0.5）：
 * 补上此前被忽略的分页——服务端一直返回 total 并支持 limit/offset。
 */
export function AccountsPage() {
  const [transactions, setTransactions] = useState<AdminWalletTransaction[]>(
    [],
  );
  const [transactionTotal, setTransactionTotal] = useState(0);
  const [transactionOffset, setTransactionOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [exporting, setExporting] = useState(false);
  const [username, setUsername] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const { autoRefresh, toggleAutoRefresh } = useAutoRefresh(() => {
    void loadRef.current?.();
  });

  const loadAccounts = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const transactionPage = await listAdminWalletTransactions({
        limit: PAGE_SIZE,
        offset: transactionOffset,
        username: username || undefined,
        type: typeFilter || undefined,
        createdFrom: createdFrom || undefined,
        createdTo: createdTo || undefined,
      });
      setTransactions(transactionPage.items);
      setTransactionTotal(transactionPage.total);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：读取账号与钱包失败。",
      );
    } finally {
      setLoading(false);
    }
  }, [createdFrom, createdTo, transactionOffset, typeFilter, username]);

  const loadRef = useRef<(() => void) | null>(null);
  loadRef.current = () => void loadAccounts();

  useEffect(() => {
    void loadAccounts();
  }, [loadAccounts]);

  async function exportTransactions() {
    if (exporting) return;
    setExporting(true);
    setError("");
    setNotice("");
    try {
      const summary = await downloadControlWalletTransactionsCsv({
        username: username || undefined,
        type: typeFilter || undefined,
        createdFrom: createdFrom || undefined,
        createdTo: createdTo || undefined,
      });
      setNotice(ledgerExportMessage(summary));
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "导出账务流水失败。",
      );
    } finally {
      setExporting(false);
    }
  }

  return (
    <section aria-label="账号与钱包" className="admin-panel">
      <div className="admin-actions">
        <button
          type="button"
          disabled={exporting || loading}
          onClick={() => void exportTransactions()}
        >
          导出账务流水 CSV
        </button>
        <button
          aria-pressed={autoRefresh}
          type="button"
          onClick={toggleAutoRefresh}
        >
          {autoRefresh ? "自动刷新：开（30 秒）" : "自动刷新：关"}
        </button>
      </div>
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {loading ? <div className="loading">加载中...</div> : null}
      <form
        className="admin-toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setTransactionOffset(0);
          void loadAccounts();
        }}
      >
        <label>
          账号
          <input
            aria-label="流水账号"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          类型
          <select
            aria-label="流水类型"
            value={typeFilter}
            onChange={(event) => setTypeFilter(event.target.value)}
          >
            <option value="">全部类型</option>
            <option value="CHARGE">充值</option>
            <option value="RESERVE">预留</option>
            <option value="SETTLE">结算</option>
            <option value="RELEASE">释放</option>
          </select>
        </label>
        <label>
          起始时间
          <input
            aria-label="流水起始时间"
            type="date"
            value={createdFrom}
            onChange={(event) => setCreatedFrom(event.target.value)}
          />
        </label>
        <label>
          截止时间
          <input
            aria-label="流水截止时间"
            type="date"
            value={createdTo}
            onChange={(event) => setCreatedTo(event.target.value)}
          />
        </label>
        <button type="submit">查询</button>
      </form>
      {!loading && transactions.length > 0 ? (
        <DataTable
          ariaLabel="账务流水列表"
          headers={
            <>
              <th>时间</th>
              <th>账号</th>
              <th>类型</th>
              <th>可用变动</th>
              <th>冻结变动</th>
              <th>变动后余额</th>
              <th>关联订单 / 任务</th>
            </>
          }
        >
          {transactions.map((tx) => (
            <tr key={tx.id}>
              <td>{formatDateTime(tx.created_at)}</td>
              <td>{tx.username}</td>
              <td>
                <StatusBadge
                  tone={
                    tx.type === "CHARGE"
                      ? "good"
                      : tx.type === "RESERVE"
                        ? "warn"
                        : "info"
                  }
                >
                  {transactionTypeLabel(tx.type)}
                </StatusBadge>
              </td>
              <td
                className={
                  tx.available_delta < 0
                    ? "ledger-delta-negative"
                    : "ledger-delta-positive"
                }
              >
                {tx.available_delta > 0 ? "+" : ""}
                {tx.available_delta} 积分
              </td>
              <td>
                {tx.reserved_delta > 0 ? "+" : ""}
                {tx.reserved_delta} 积分
              </td>
              <td>
                {tx.available_balance_after === null ||
                tx.reserved_balance_after === null ? (
                  "历史未记录"
                ) : (
                  <>
                    <strong>{tx.available_balance_after} 积分</strong> / 冻结{" "}
                    {tx.reserved_balance_after} 积分
                  </>
                )}
              </td>
              <td>
                {tx.service_name && <div>{tx.service_name}</div>}
                <code>
                  {tx.recharge_order_id ??
                    tx.task_id ??
                    tx.oral_task_id ??
                    tx.source_id ??
                    tx.billing_operation_id ??
                    "—"}
                </code>
              </td>
            </tr>
          ))}
        </DataTable>
      ) : null}
      {!loading && transactions.length === 0 && !error ? (
        <PageBanner tone="notice">暂无账务流水。</PageBanner>
      ) : null}
      <Pagination
        disabled={loading}
        limit={PAGE_SIZE}
        offset={transactionOffset}
        total={transactionTotal}
        onPageChange={setTransactionOffset}
      />
      <p className="admin-hint">
        余额与变动单位均为积分；新流水按实际记账顺序计算余额，历史缺少可靠顺序的记录不推算余额。
      </p>
    </section>
  );
}
