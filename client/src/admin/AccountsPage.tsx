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
  HELD_CREDITS_LABEL,
  ledgerExportMessage,
  TRANSACTION_TYPE_LABELS,
  transactionTypeLabel,
} from "./ui/vocabulary";

const PAGE_SIZE = 20;

/**
 * 积分流水页（从 AdminApp 内联表格抽出，2026-09-02 评估 §0.5）：
 * 补上此前被忽略的分页——服务端一直返回 total 并支持 limit/offset。
 */
export function AccountsPage({
  readOnly = false,
  userId = "",
}: {
  readOnly?: boolean;
  userId?: string;
} = {}) {
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
        userId: userId || undefined,
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
          : "加载失败：读取积分流水失败。",
      );
    } finally {
      setLoading(false);
    }
  }, [createdFrom, createdTo, transactionOffset, typeFilter, username, userId]);

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
        userId: userId || undefined,
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
    <section aria-label="积分流水" className="admin-panel">
      <div className="admin-actions">
        {/* 整表导出是写级动作（服务端 ControlWriter）：只读角色不渲染入口。 */}
        {readOnly ? null : (
          <button
            type="button"
            disabled={exporting || loading}
            onClick={() => void exportTransactions()}
          >
            导出账务流水 CSV
          </button>
        )}
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
          客户
          <input
            aria-label="流水客户"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label>
          业务类型
          <select
            aria-label="流水业务类型"
            value={typeFilter}
            onChange={(event) => setTypeFilter(event.target.value)}
          >
            <option value="">全部业务类型</option>
            {/* P2-1：选项从词典生成——此前手写四项漏了退款扣减/历史转换，
                运营在流水中能看到的类型却筛不出来。 */}
            {Object.entries(TRANSACTION_TYPE_LABELS).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
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
              <th>客户</th>
              <th>业务类型</th>
              <th>可用变动</th>
              <th>{HELD_CREDITS_LABEL}变动</th>
              <th>变动后余额</th>
              <th>关联业务</th>
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
                    <strong>{tx.available_balance_after} 积分</strong> /{" "}
                    {HELD_CREDITS_LABEL} {tx.reserved_balance_after} 积分
                  </>
                )}
              </td>
              <td>
                <strong>
                  {tx.business_label ?? tx.service_name ?? "历史业务名称未记录"}
                </strong>
                <div>
                  <a
                    href={`#admin/customersMgmt?userId=${encodeURIComponent(tx.user_id)}`}
                  >
                    查看客户
                  </a>
                  {tx.order_no ? (
                    <a
                      href={`#admin/funds?intent=order&orderNo=${encodeURIComponent(tx.order_no)}&userId=${encodeURIComponent(tx.user_id)}`}
                    >
                      查看订单
                    </a>
                  ) : null}
                  {tx.task_id || tx.oral_task_id || tx.source_id ? (
                    <a
                      href={`#admin/generationRecords?taskId=${encodeURIComponent(tx.task_id ?? tx.oral_task_id ?? tx.source_id ?? "")}&userId=${encodeURIComponent(tx.user_id)}`}
                    >
                      查看关联任务
                    </a>
                  ) : null}
                </div>
                <details>
                  <summary>查看关联编号</summary>
                  <code>
                    {tx.recharge_order_id ??
                      tx.task_id ??
                      tx.oral_task_id ??
                      tx.source_id ??
                      tx.billing_operation_id ??
                      "—"}
                  </code>
                  {tx.billing_round != null ? (
                    <div>计费轮次 {tx.billing_round}</div>
                  ) : null}
                </details>
              </td>
            </tr>
          ))}
        </DataTable>
      ) : null}
      {!loading && transactions.length === 0 && !error ? (
        <PageBanner tone="notice">暂无账务流水。</PageBanner>
      ) : null}
      <p className="admin-hint">
        生成冻结是在任务提交时保留预计积分；生成扣费是任务结束后确认的消耗；未使用的生成冻结积分会退回可用余额。
        退款扣减是人工退款对应的积分减少。关联编号保留用于追溯，不代表额外扣费。
      </p>
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
