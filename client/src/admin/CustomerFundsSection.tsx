import { type ReactNode, useEffect, useState } from "react";
import {
  type AdminRechargeOrder,
  type AdminWalletTransaction,
  type CustomerListItem,
  listAdminRechargeOrders,
  listAdminWalletTransactions,
  resumeCustomer,
  suspendCustomer,
} from "../api.admin";
import { AccountsPage } from "./AccountsPage";
import { AdjustmentsPage } from "./AdjustmentsPage";
import { OrdersPage } from "./OrdersPage";
import { RecordCallsPanel } from "./RecordCallsPanel";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { PageBanner } from "./ui/PageBanner";
import { OrderStatusBadge } from "./ui/StatusBadge";
import {
  formatDateTime,
  formatFen,
  transactionTypeLabel,
} from "./ui/vocabulary";
import "./admin-customer-detail.css";

import { companyNameOf } from "./CustomerIdentity";

type Customer360Snapshot = {
  orders: AdminRechargeOrder[];
  transactions: AdminWalletTransaction[];
};

// 充值订单不进生成记录列表（它不是生成任务），但第三方调用同源落库：
// #9 要求用订单号也能反查这张单出网调了什么，支付回调报文就在原始响应里。
const RECHARGE_ORDER_RECORD_TYPE = "RECHARGE_ORDER";

export function Customer360Data({
  userId,
  readOnly,
}: {
  userId: string;
  readOnly: boolean;
}) {
  const [snapshot, setSnapshot] = useState<Customer360Snapshot | null>(null);
  const [error, setError] = useState("");
  // 一次只展开一单的查单日志；再点一次收起。
  const [openedOrderNo, setOpenedOrderNo] = useState<string | null>(null);
  const [fullFunds, setFullFunds] = useState<
    "orders" | "ledger" | "adjustments" | null
  >(null);

  useEffect(() => {
    let cancelled = false;
    setError("");
    void Promise.all([
      listAdminRechargeOrders({ userId, limit: 3, offset: 0 }),
      listAdminWalletTransactions({ userId, limit: 3, offset: 0 }),
    ])
      .then(([orders, transactions]) => {
        if (!cancelled) {
          setSnapshot({
            orders: orders.items,
            transactions: transactions.items,
          });
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(
            cause instanceof Error && cause.message
              ? `客户运营数据加载失败：${cause.message}`
              : "客户运营数据加载失败",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  if (error) {
    return <PageBanner tone="error">{error}</PageBanner>;
  }
  if (!snapshot) {
    return <p className="admin-hint">正在加载…</p>;
  }

  return (
    <section aria-label="客户 360 度运营数据" className="customer-360-grid">
      <nav aria-label="客户完整资金记录">
        <button type="button" onClick={() => setFullFunds("orders")}>
          查看全部充值订单
        </button>
        <button type="button" onClick={() => setFullFunds("ledger")}>
          查看全部积分流水
        </button>
        <button type="button" onClick={() => setFullFunds("adjustments")}>
          查看全部人工调整
        </button>
        {fullFunds ? (
          <button type="button" onClick={() => setFullFunds(null)}>
            收起完整资金记录
          </button>
        ) : null}
      </nav>
      {fullFunds === "ledger" ? (
        <AccountsPage key={userId} userId={userId} readOnly={readOnly} />
      ) : null}
      {fullFunds === "orders" ? (
        <OrdersPage key={userId} userId={userId} readOnly={readOnly} />
      ) : null}
      {fullFunds === "adjustments" ? (
        <AdjustmentsPage key={userId} userId={userId} readOnly={readOnly} />
      ) : null}
      <Customer360Panel
        className={openedOrderNo ? "customer-360-panel--wide" : undefined}
        title="最近充值订单"
      >
        {snapshot.orders.length ? (
          snapshot.orders.map((order) => (
            <div className="customer-360-order" key={order.id}>
              <div className="customer-360-row">
                <code>{order.order_no}</code>
                <span>{formatFen(order.amount_fen)}</span>
                <OrderStatusBadge status={order.status} />
                <small>
                  {formatDateTime(order.paid_at ?? order.created_at)}
                </small>
              </div>
              <div className="customer-360-order-tools">
                <button
                  aria-expanded={openedOrderNo === order.order_no}
                  type="button"
                  onClick={() =>
                    setOpenedOrderNo((current) =>
                      current === order.order_no ? null : order.order_no,
                    )
                  }
                >
                  {openedOrderNo === order.order_no
                    ? "收起查单日志"
                    : "查单日志"}
                </button>
              </div>
              {openedOrderNo === order.order_no ? (
                <RecordCallsPanel
                  active
                  readOnly={readOnly}
                  recordId={order.order_no}
                  recordType={RECHARGE_ORDER_RECORD_TYPE}
                />
              ) : null}
            </div>
          ))
        ) : (
          <Customer360Empty />
        )}
      </Customer360Panel>

      <Customer360Panel title="最近积分流水">
        {snapshot.transactions.length ? (
          snapshot.transactions.map((transaction) => (
            <div className="customer-360-row" key={transaction.id}>
              <span>{transactionTypeLabel(transaction.type)}</span>
              <strong>{transaction.available_delta} 积分</strong>
              <span>
                {transaction.available_balance_after === null
                  ? "历史未记录"
                  : `余额 ${transaction.available_balance_after} 积分`}
              </span>
              <small>{formatDateTime(transaction.created_at)}</small>
            </div>
          ))
        ) : (
          <Customer360Empty />
        )}
      </Customer360Panel>
    </section>
  );
}

function Customer360Panel({
  title,
  children,
  className,
}: {
  title: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={
        className ? `customer-360-panel ${className}` : "customer-360-panel"
      }
    >
      <h3>{title}</h3>
      <div>{children}</div>
    </section>
  );
}

function Customer360Empty() {
  return <p className="admin-hint">暂无记录</p>;
}

/** 暂停 / 恢复账号（方案 P1 客户管理第 4 主操作）。
 *  口径：暂停只禁止新登录与新任务并吊销当前会话，余额不动；恢复后客户自行
 *  重新登录。与调账同写契约（原因必填 + 幂等键）。 */
export function CustomerSuspendButton({
  customer,
  onChanged,
}: {
  customer: CustomerListItem;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const suspended =
    customer.account_active === undefined
      ? customer.status.toUpperCase() === "SUSPENDED"
      : !customer.account_active;
  const action = suspended ? "恢复账号" : "暂停账号";

  async function submit(reason: string) {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const key = crypto.randomUUID();
      if (suspended) {
        await resumeCustomer(customer.user_id, reason, key);
      } else {
        await suspendCustomer(customer.user_id, reason, key);
      }
      setOpen(false);
      // 状态列与核心指标由列表刷新承载；这里给出可感知的结果提示。
      setNotice(
        suspended
          ? `已恢复 ${companyNameOf(customer)}，客户可重新登录。`
          : `已暂停 ${companyNameOf(customer)}，当前会话已下线，余额保持不变。`,
      );
      onChanged();
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : `${action}失败，请重试。`,
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      <button type="button" onClick={() => setOpen(true)}>
        {action}
      </button>
      <ConfirmDialog
        busy={busy}
        confirmLabel={`确认${action}`}
        description={
          suspended
            ? "恢复后客户可重新登录，钱包余额与进行中的任务不受影响。原因将写入审计日志。"
            : "暂停后该客户无法登录或发起新任务；当前在线会话立即下线，钱包余额保持不变，进行中的任务跑完。原因将写入审计日志。"
        }
        error={error}
        level="reason"
        open={open}
        title={`${action} ${companyNameOf(customer)}`}
        onClose={() => setOpen(false)}
        onConfirm={(reason: string) => void submit(reason)}
      />
    </>
  );
}
