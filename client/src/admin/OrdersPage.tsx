import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  type ControlReconciliation,
  downloadControlRechargeOrdersCsv,
  getControlReconciliation,
  type RechargeOrderStatus,
  syncControlRechargeOrder,
} from "../api";
import { type AdminRechargeOrder, listAdminRechargeOrders } from "../api.admin";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { DataTable } from "./ui/DataTable";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { OrderStatusBadge } from "./ui/StatusBadge";
import { useAutoRefresh } from "./ui/useAutoRefresh";
import {
  formatDateTime,
  formatFen,
  ledgerExportMessage,
} from "./ui/vocabulary";

const PAGE_SIZE = 20;

const STATUS_FILTERS = [
  { value: "", label: "全部状态" },
  { value: "PENDING", label: "待支付" },
  { value: "PAID", label: "已支付" },
  { value: "FAILED", label: "失败" },
  { value: "CLOSED", label: "已关闭" },
] as const;

/**
 * 充值订单页（从 AdminApp 内联表格抽出）：补上状态筛选与分页（服务端
 * 均已支持）；手动查单改为说明性确认——它会向 ZPay 查单并可能入账。
 */
export function OrdersPage({ readOnly = false }: { readOnly?: boolean }) {
  const [orders, setOrders] = useState<AdminRechargeOrder[]>([]);
  const [orderTotal, setOrderTotal] = useState(0);
  const [orderOffset, setOrderOffset] = useState(0);
  const [statusDraft, setStatusDraft] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [usernameFilter, setUsernameFilter] = useState("");
  const [channelFilter, setChannelFilter] = useState("");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [reconciliation, setReconciliation] =
    useState<ControlReconciliation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [exporting, setExporting] = useState(false);
  const [pendingSyncOrderNo, setPendingSyncOrderNo] = useState<string | null>(
    null,
  );
  const [syncing, setSyncing] = useState(false);
  const { autoRefresh, toggleAutoRefresh } = useAutoRefresh(() => {
    void loadOrdersRef.current?.();
  });

  const loadOrders = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [orderPage, nextReconciliation] = await Promise.all([
        listAdminRechargeOrders({
          status: (statusFilter || undefined) as
            | RechargeOrderStatus
            | undefined,
          limit: PAGE_SIZE,
          offset: orderOffset,
          username: usernameFilter || undefined,
          channel: channelFilter || undefined,
          createdFrom: createdFrom || undefined,
          createdTo: createdTo || undefined,
        }),
        getControlReconciliation(),
      ]);
      setOrders(orderPage.items);
      setOrderTotal(orderPage.total);
      setReconciliation(nextReconciliation);
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? `加载失败：${cause.message}`
          : "加载失败：读取充值订单失败。",
      );
    } finally {
      setLoading(false);
    }
  }, [
    channelFilter,
    createdFrom,
    createdTo,
    orderOffset,
    statusFilter,
    usernameFilter,
  ]);

  const loadOrdersRef = useRef<(() => void) | null>(null);
  loadOrdersRef.current = () => void loadOrders();

  useEffect(() => {
    void loadOrders();
  }, [loadOrders]);

  function handleFilterSubmit(event: FormEvent) {
    event.preventDefault();
    setOrderOffset(0);
    setStatusFilter(statusDraft);
  }

  async function confirmSync(reason: string) {
    if (!pendingSyncOrderNo || syncing) {
      return;
    }
    setSyncing(true);
    setError("");
    setNotice("");
    try {
      await syncControlRechargeOrder(pendingSyncOrderNo, reason);
      setNotice(`订单 ${pendingSyncOrderNo} 状态已同步。`);
      setPendingSyncOrderNo(null);
      await loadOrders();
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "同步订单失败。",
      );
    } finally {
      setSyncing(false);
    }
  }

  async function exportRechargeOrders() {
    if (exporting) return;
    setExporting(true);
    setError("");
    setNotice("");
    try {
      const summary = await downloadControlRechargeOrdersCsv({
        status: (statusFilter || undefined) as RechargeOrderStatus | undefined,
        username: usernameFilter || undefined,
        channel: channelFilter || undefined,
        createdFrom: createdFrom || undefined,
        createdTo: createdTo || undefined,
      });
      setNotice(ledgerExportMessage(summary));
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : "导出充值订单失败。",
      );
    } finally {
      setExporting(false);
    }
  }

  return (
    <section aria-label="充值订单" className="admin-panel admin-orders-page">
      <div className="admin-actions">
        <button
          aria-pressed={autoRefresh}
          type="button"
          onClick={toggleAutoRefresh}
        >
          {autoRefresh ? "自动刷新：开（30 秒）" : "自动刷新：关"}
        </button>
        <button
          type="button"
          disabled={exporting || loading}
          onClick={() => void exportRechargeOrders()}
        >
          导出充值订单 CSV
        </button>
      </div>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {reconciliation ? (
        <div className="admin-metrics">
          {[
            ["钱包数", reconciliation.wallet_count],
            ["待支付订单", reconciliation.pending_order_count],
            ["钱包不一致", reconciliation.wallet_mismatch_count],
            ["已支付未入账", reconciliation.paid_order_without_charge_count],
            [
              "入账但订单未支付",
              reconciliation.charge_without_paid_order_count,
            ],
          ].map(([label, value]) => (
            <span key={label}>
              <small>{label} </small>
              <strong>{value}</strong>
            </span>
          ))}
        </div>
      ) : null}

      <form
        className="admin-form admin-filter-grid"
        onSubmit={handleFilterSubmit}
      >
        <label>
          订单状态
          <select
            value={statusDraft}
            onChange={(event) => setStatusDraft(event.target.value)}
          >
            {STATUS_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          账号
          <input
            aria-label="订单账号"
            value={usernameFilter}
            onChange={(event) => setUsernameFilter(event.target.value)}
          />
        </label>
        <label>
          支付渠道
          <select
            aria-label="支付渠道"
            value={channelFilter}
            onChange={(event) => setChannelFilter(event.target.value)}
          >
            <option value="">全部渠道</option>
            <option value="alipay">支付宝</option>
            <option value="wxpay">微信支付</option>
          </select>
        </label>
        <label>
          起始时间
          <input
            aria-label="订单起始时间"
            type="date"
            value={createdFrom}
            onChange={(event) => setCreatedFrom(event.target.value)}
          />
        </label>
        <label>
          截止时间
          <input
            aria-label="订单截止时间"
            type="date"
            value={createdTo}
            onChange={(event) => setCreatedTo(event.target.value)}
          />
        </label>
        <button disabled={loading} type="submit">
          筛选
        </button>
      </form>

      {!loading && orders.length === 0 && !error ? (
        <PageBanner tone="notice">暂无充值订单。</PageBanner>
      ) : (
        <DataTable
          ariaLabel="充值订单列表"
          headers={
            <>
              <th>订单号</th>
              <th>账号</th>
              <th>金额</th>
              <th>额度</th>
              <th>状态</th>
              <th>支付通道</th>
              <th>支付渠道</th>
              <th>第三方单号</th>
              <th>下单时间</th>
              <th>支付时间</th>
              <th>操作</th>
            </>
          }
        >
          {orders.map((order) => (
            <tr key={order.id}>
              <td>
                <code>{order.order_no}</code>
              </td>
              <td>{order.username}</td>
              <td>{formatFen(order.amount_fen)}</td>
              <td>+{order.credits} 积分</td>
              <td>
                <OrderStatusBadge status={order.status} />
              </td>
              <td>{providerLabel(order.provider)}</td>
              <td>{order.channel || "—"}</td>
              <td>
                {/* WeChat Native keeps provider_trade_no NULL and settles into
                    transaction_id, so both columns must be read to show a trade
                    reference for every provider. */}
                <code>
                  {order.transaction_id ?? order.provider_trade_no ?? "—"}
                </code>
              </td>
              <td>{formatDateTime(order.created_at)}</td>
              <td>{formatDateTime(order.paid_at)}</td>
              <td>
                {order.status !== "PENDING" || readOnly ? (
                  "—"
                ) : order.provider === "wechat_native" ? (
                  // 查单同步 only speaks to ZPay. A WeChat order is reconciled
                  // from 客户详情 → 充值核验, which picks the right gateway.
                  <span title="微信订单请在客户详情页的“充值核验”中核验">
                    客户详情核验
                  </span>
                ) : (
                  <button
                    type="button"
                    onClick={() => setPendingSyncOrderNo(order.order_no)}
                  >
                    查单同步
                  </button>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}

      <Pagination
        disabled={loading}
        limit={PAGE_SIZE}
        offset={orderOffset}
        total={orderTotal}
        onPageChange={setOrderOffset}
      />

      <ConfirmDialog
        busy={syncing}
        confirmLabel="确认查单"
        description="将立即向 ZPay 查询该订单的最新支付状态；若已支付，会当场完成入账。原因将写入审计日志。"
        level="reason"
        open={pendingSyncOrderNo !== null}
        title={`查单同步 ${pendingSyncOrderNo ?? ""}`}
        onClose={() => setPendingSyncOrderNo(null)}
        onConfirm={(reason: string) => void confirmSync(reason)}
      />
    </section>
  );
}

const PROVIDER_LABELS: Record<string, string> = {
  zpay: "ZPay",
  wechat_native: "微信官方",
  activation_code: "激活码",
  admin_adjustment: "管理员调整",
};

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}
