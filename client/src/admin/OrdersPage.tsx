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
  type ReconciliationAnomaly,
  syncControlRechargeOrder,
} from "../api";
import { type AdminRechargeOrder, listAdminRechargeOrders } from "../api.admin";
import { CustomerLink } from "./CustomerLink";
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
export function OrdersPage({
  readOnly = false,
  userId = "",
  orderNo = "",
  onOpenReconciliation,
  onCustomer,
}: {
  readOnly?: boolean;
  userId?: string;
  orderNo?: string;
  onOpenReconciliation?: (anomaly: ReconciliationAnomaly) => void;
  onCustomer?: (id: string) => void;
}) {
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
          orderNo: orderNo || undefined,
          status: (statusFilter || undefined) as
            | RechargeOrderStatus
            | undefined,
          limit: PAGE_SIZE,
          offset: orderOffset,
          userId: userId || undefined,
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
    userId,
    orderNo,
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
    if (exporting || orderNo) return;
    setExporting(true);
    setError("");
    setNotice("");
    try {
      const summary = await downloadControlRechargeOrdersCsv({
        status: (statusFilter || undefined) as RechargeOrderStatus | undefined,
        userId: userId || undefined,
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
        {/* 整表导出是写级动作（服务端 ControlWriter）：auditor 不渲染入口，
            而不是渲染出来点了才 403。 */}
        {readOnly ? null : (
          <button
            type="button"
            disabled={exporting || loading || Boolean(orderNo)}
            onClick={() => void exportRechargeOrders()}
          >
            导出充值订单 CSV
          </button>
        )}
      </div>

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}

      {reconciliation && !userId && !orderNo ? (
        <div className="admin-metrics">
          <button
            type="button"
            onClick={() => {
              setStatusDraft("PENDING");
              setStatusFilter("PENDING");
              setOrderOffset(0);
              setUsernameFilter("");
              setChannelFilter("");
              setCreatedFrom("");
              setCreatedTo("");
            }}
          >
            <small>待支付订单 </small>
            <strong>{reconciliation.pending_order_count}</strong>
          </button>
          {(
            [
              [
                "钱包不一致",
                reconciliation.wallet_mismatch_count,
                "wallet_mismatch",
              ],
              [
                "已支付未入账",
                reconciliation.paid_order_without_charge_count,
                "paid_without_charge",
              ],
              [
                "入账但订单未支付",
                reconciliation.charge_without_paid_order_count,
                "charge_without_paid_order",
              ],
            ] as const
          ).map(([label, value, anomaly]) => (
            <button
              key={anomaly}
              type="button"
              onClick={() => {
                if (onOpenReconciliation) onOpenReconciliation(anomaly);
                else
                  window.location.hash = `admin/funds?intent=recon&anomaly=${anomaly}`;
              }}
            >
              <small>{label} </small>
              <strong>{value}</strong>
            </button>
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
          客户
          <input
            aria-label="订单客户"
            value={usernameFilter}
            onChange={(event) => setUsernameFilter(event.target.value)}
          />
        </label>
        <label>
          支付方式
          <select
            aria-label="支付方式"
            value={channelFilter}
            onChange={(event) => setChannelFilter(event.target.value)}
          >
            <option value="">全部方式</option>
            <option value="alipay">支付宝</option>
            <option value="wxpay">微信</option>
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
              <th>客户</th>
              <th>金额</th>
              <th>到账积分</th>
              <th>状态</th>
              <th>支付方式</th>
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
              <td>
                <CustomerLink
                  userId={order.user_id}
                  company={order.display_name}
                  username={order.username}
                  onCustomer={onCustomer}
                />
              </td>
              <td>{formatFen(order.amount_fen)}</td>
              <td>+{order.credits} 积分</td>
              <td>
                <OrderStatusBadge status={order.status} />
              </td>
              <td>{paymentMethodLabel(order)}</td>
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
                ) : (
                  // 查单补单统一入口（方案 P1）：后端按订单渠道分派网关，
                  // 微信订单不再需要绕道客户详情核验。
                  <button
                    type="button"
                    onClick={() => setPendingSyncOrderNo(order.order_no)}
                  >
                    查单补单
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
        description="将立即向支付通道查询该订单的最新支付状态；若已支付，会当场完成入账。原因将写入审计日志。"
        level="reason"
        open={pendingSyncOrderNo !== null}
        title={`查单补单 ${pendingSyncOrderNo ?? ""}`}
        onClose={() => setPendingSyncOrderNo(null)}
        onConfirm={(reason: string) => void confirmSync(reason)}
      />
    </section>
  );
}

/**
 * 支付方式只回答“客户怎么付的钱”（P2-1）：渠道值优先（alipay / wxpay），
 * 无渠道的历史单按 provider 换算；两个技术列（支付通道 / 支付渠道）合并为一列，
 * 运营不再需要理解 provider 与 channel 的区别。
 */
function paymentMethodLabel(order: AdminRechargeOrder): string {
  const byChannel: Record<string, string> = {
    alipay: "支付宝",
    wxpay: "微信",
  };
  const byProvider: Record<string, string> = {
    zpay: "支付宝",
    wechat_native: "微信",
    activation_code: "激活码",
    admin_adjustment: "线下转账",
  };
  return (
    byChannel[order.channel ?? ""] ??
    byProvider[order.provider] ??
    order.channel ??
    order.provider ??
    "—"
  );
}
