import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { downloadCustomersCsv } from "../api";
import {
  type AdjustmentWriteResult,
  AdminActivationError,
  type AdminRechargeOrder,
  type AdminWalletTransaction,
  type CustomerListItem,
  type CustomerUnitPrice,
  createCustomerAdjustment,
  fetchCustomerUnitPrice,
  listAdminRechargeOrders,
  listAdminWalletTransactions,
  listCustomers,
  updateCustomerUnitPrice,
} from "../api.admin";
import { yuanInputToFen } from "../rechargePackageDisplay";
import { AccountCreditPanel } from "./AccountCreditPanel";
import { CustomerDeviceSection } from "./CustomerDeviceSection";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { CopyCustomerId } from "./ui/CopyCustomerId";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { CustomerStatusBadge, OrderStatusBadge } from "./ui/StatusBadge";
import {
  formatDateTime,
  formatFen,
  formatYuanFromFen,
  transactionTypeLabel,
} from "./ui/vocabulary";
import "./admin-customer-detail.css";

interface CustomersPageProps {
  embedded?: boolean;
  operatorId?: string;
  readOnly?: boolean;
  /**
   * 导航意图（AdminApp 从 hash `?intent=` 解析后透传）。总览快捷入口跳转到
   * 客户管理后必须"有下文"，否则管理员只看到一个与上下文无关的客户列表：
   * - customerAdjustments（后台加款 / 发放赠送积分）：引导到客户详情内的
   *   「赠送积分」表单，并在展开客户时自动定位到该区块；
   * - 其余 intent 与空值：维持原有客户列表行为。
   *
   * （issueCodes / codes 两个 intent 已随本批删除：它们自 #102 下线「快速发码」
   *   后就没有任何产出方，保留只会留下无来源的死常量。）
   */
  initialIntent?: string;
}

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

// 页大小是常量，不是状态：原写法把它放进 useState 却从不改它，等于把常量
// 伪装成状态（2026-09-12 评审 P3 的「伪状态反模式」）。
const PAGE_SIZE = 20;

export function CustomersPage({
  embedded = false,
  operatorId = "standalone-admin",
  readOnly = false,
  initialIntent = "",
}: CustomersPageProps = {}) {
  const [customers, setCustomers] = useState<CustomerListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [usernameDraft, setUsernameDraft] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [createdFrom, setCreatedFrom] = useState("");
  const [createdTo, setCreatedTo] = useState("");
  const [balanceMin, setBalanceMin] = useState("");
  const [balanceMax, setBalanceMax] = useState("");
  const [expandedUserId, setExpandedUserId] = useState<string | null>(null);
  const [filters, setFilters] = useState({
    username: "",
    status: "all",
    createdFrom: "",
    createdTo: "",
    balanceMin: "",
    balanceMax: "",
  });
  const requestId = useRef(0);
  const usernameFilterRef = useRef<HTMLInputElement>(null);
  // 加款意图只在可写角色下引导：auditor 的详情页没有发放表单。
  const adjustmentsIntent =
    initialIntent === "customerAdjustments" && !readOnly;

  // 总览「后台加款 / 发放赠送积分」跳进来后直接落在用户名筛选上，
  // 管理员可以立刻输入客户名，而不是先自己找筛选框。
  useEffect(() => {
    if (adjustmentsIntent) {
      usernameFilterRef.current?.focus();
    }
  }, [adjustmentsIntent]);

  const loadCustomers = useCallback(async () => {
    const sequence = ++requestId.current;
    try {
      setLoading(true);
      setError("");
      const response = await listCustomers({
        limit: PAGE_SIZE,
        offset,
        username_filter: filters.username || undefined,
        status: filters.status === "all" ? undefined : filters.status,
        createdFrom: filters.createdFrom || undefined,
        createdTo: filters.createdTo || undefined,
        balanceMin: filters.balanceMin ? Number(filters.balanceMin) : undefined,
        balanceMax: filters.balanceMax ? Number(filters.balanceMax) : undefined,
      });
      if (sequence !== requestId.current) return;
      setCustomers(response.items);
      setTotal(response.total);
    } catch (err) {
      if (sequence !== requestId.current) return;
      setError(
        err instanceof Error && err.message
          ? `加载失败：${err.message}`
          : "加载失败：未知错误",
      );
    } finally {
      if (sequence === requestId.current) setLoading(false);
    }
  }, [filters, offset]);

  useEffect(() => {
    loadCustomers();
    return () => {
      requestId.current += 1;
    };
  }, [loadCustomers]);

  const handleFilterSubmit = (e: FormEvent) => {
    e.preventDefault();
    setOffset(0);
    setExpandedUserId(null);
    setFilters({
      username: usernameDraft.trim(),
      status: statusFilter,
      createdFrom,
      createdTo,
      balanceMin,
      balanceMax,
    });
  };

  const failedGenerations = customers.reduce(
    (sum, customer) => sum + (customer.generation_failed ?? 0),
    0,
  );
  const inProgressGenerations = customers.reduce(
    (sum, customer) => sum + (customer.generation_in_progress ?? 0),
    0,
  );

  const exportList = () => {
    void downloadCustomersCsv({
      status: filters.status === "all" ? undefined : filters.status,
      username: filters.username || undefined,
      createdFrom: filters.createdFrom || undefined,
      createdTo: filters.createdTo || undefined,
      balanceMin: filters.balanceMin ? Number(filters.balanceMin) : undefined,
      balanceMax: filters.balanceMax ? Number(filters.balanceMax) : undefined,
    }).catch((cause: unknown) =>
      setError(cause instanceof Error ? cause.message : "导出失败"),
    );
  };

  const focusedCustomer =
    expandedUserId === null
      ? null
      : (customers.find((customer) => customer.user_id === expandedUserId) ??
        null);
  if (focusedCustomer) {
    return (
      <CustomerDetailView
        customer={focusedCustomer}
        focusGrantSection={adjustmentsIntent}
        operatorId={operatorId}
        onChanged={() => void loadCustomers()}
        onGranted={(result) => {
          setCustomers((current) =>
            current.map((customer) =>
              customer.user_id === focusedCustomer.user_id
                ? {
                    ...customer,
                    available_credits: result.wallet_balance_after,
                  }
                : customer,
            ),
          );
          void loadCustomers();
        }}
        readOnly={readOnly}
        refreshError={error}
        onBack={() => setExpandedUserId(null)}
      />
    );
  }

  return (
    <div className="customers-page">
      {!embedded ? (
        <header className="admin-page-header">
          <h1>客户管理</h1>
          <p>
            聚焦客户状态、生成表现和消耗情况，支持在同一页快速查看运营详情。
          </p>
        </header>
      ) : null}

      {adjustmentsIntent ? (
        <PageBanner tone="notice">
          后台加款与赠送积分在客户详情内完成：先筛选并展开目标客户，页面会自动定位到「赠送积分」区块。
        </PageBanner>
      ) : null}

      <form
        className="admin-toolbar customer-list-filters"
        onSubmit={handleFilterSubmit}
      >
        <label className="admin-toolbar__field">
          <span>用户名筛选</span>
          <input
            placeholder="按用户名筛选"
            ref={usernameFilterRef}
            type="text"
            value={usernameDraft}
            onChange={(e) => setUsernameDraft(e.target.value)}
          />
        </label>
        <label className="admin-toolbar__field">
          <span>注册起始</span>
          <input
            aria-label="注册起始"
            type="date"
            value={createdFrom}
            onChange={(event) => setCreatedFrom(event.target.value)}
          />
        </label>
        <label className="admin-toolbar__field">
          <span>注册截止</span>
          <input
            aria-label="注册截止"
            type="date"
            min={createdFrom || undefined}
            value={createdTo}
            onChange={(event) => setCreatedTo(event.target.value)}
          />
        </label>
        <label className="admin-toolbar__field">
          <span>最低余额（积分）</span>
          <input
            aria-label="最低余额"
            min="0"
            type="number"
            value={balanceMin}
            onChange={(event) => setBalanceMin(event.target.value)}
          />
        </label>
        <label className="admin-toolbar__field">
          <span>最高余额（积分）</span>
          <input
            aria-label="最高余额"
            min="0"
            type="number"
            value={balanceMax}
            onChange={(event) => setBalanceMax(event.target.value)}
          />
        </label>
        <label className="admin-toolbar__field admin-toolbar__field--select">
          <span>客户状态</span>
          <select
            aria-label="客户状态"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value)}
          >
            <option value="all">全部状态</option>
            <option value="active">活跃</option>
            <option value="suspended">已暂停</option>
            <option value="revoked">已撤销</option>
          </select>
        </label>
        <div className="customer-list-filters__actions">
          <button className="admin-toolbar__secondary" type="submit">
            筛选
          </button>
          <button type="button" onClick={exportList}>
            导出列表 CSV
          </button>
        </div>
      </form>

      <section aria-label="需要处理" className="admin-attention-strip">
        <strong>需要处理</strong>
        <span>
          失败生成 <b>{failedGenerations}</b>
        </span>
        <span>
          处理中 <b>{inProgressGenerations}</b>
        </span>
        <small>数据范围：当前筛选页</small>
      </section>

      {loading && <div className="loading">加载中...</div>}

      {error ? <PageBanner tone="error">{error}</PageBanner> : null}

      {!loading && !error && customers.length === 0 && (
        <div className="empty-state">暂无客户数据</div>
      )}

      {!loading && !error && customers.length > 0 && (
        <>
          <div className="table-scroll admin-table-card">
            <table
              aria-label="客户列表"
              className="customers-table admin-data-table"
            >
              <thead>
                <tr>
                  <th>用户名</th>
                  <th>公司名称</th>
                  <th>客户 ID</th>
                  <th>注册时间</th>
                  <th>状态</th>
                  <th>可用额度</th>
                  <th>累计消耗</th>
                  <th>生成情况</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {customers.map((customer) => (
                  <tr key={customer.user_id}>
                    <td data-label="用户名">
                      <span
                        className="customer-cell-ellipsis"
                        title={customer.username}
                      >
                        {customer.username}
                      </span>
                    </td>
                    <td data-label="公司名称">
                      <span
                        className="customer-cell-ellipsis"
                        title={customer.display_name}
                      >
                        {customer.display_name}
                      </span>
                    </td>
                    <td data-label="客户 ID">
                      <CopyCustomerId value={customer.user_id} />
                    </td>
                    <td data-label="注册时间">
                      <time
                        className="customer-cell-date"
                        dateTime={customer.created_at}
                      >
                        {formatDateTime(customer.created_at).split(" ")[0]}{" "}
                        <small>
                          {formatDateTime(customer.created_at).split(" ")[1]}
                        </small>
                      </time>
                    </td>
                    <td data-label="状态">
                      <CustomerStatusBadge status={customer.status} />
                    </td>
                    <td data-label="可用额度">
                      <strong>{customer.available_credits ?? 0} 积分</strong>
                    </td>
                    <td
                      aria-label={`${customer.username} 已结算消耗`}
                      data-label="累计消耗"
                    >
                      {customer.credits_spent ?? 0} 积分
                    </td>
                    <td data-label="生成情况">
                      <div className="customer-cell-generation">
                        <span>
                          成功 {customer.generation_succeeded ?? 0} /{" "}
                          {customer.generation_total ?? 0}
                        </span>{" "}
                        <small>
                          失败 {customer.generation_failed ?? 0} · 进行中{" "}
                          {customer.generation_in_progress ?? 0}
                        </small>
                      </div>
                    </td>
                    <td data-label="操作">
                      <button
                        aria-controls={`customer-detail-${customer.user_id}`}
                        aria-expanded="false"
                        type="button"
                        onClick={() => setExpandedUserId(customer.user_id)}
                      >
                        展开详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <Pagination
            limit={PAGE_SIZE}
            noun="位"
            offset={offset}
            total={total}
            onPageChange={setOffset}
          />
        </>
      )}
    </div>
  );
}

type Customer360Snapshot = {
  orders: AdminRechargeOrder[];
  transactions: AdminWalletTransaction[];
};

function Customer360Data({ userId }: { userId: string }) {
  const [snapshot, setSnapshot] = useState<Customer360Snapshot | null>(null);
  const [error, setError] = useState("");

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
      <Customer360Panel title="最近充值订单">
        {snapshot.orders.length ? (
          snapshot.orders.map((order) => (
            <div className="customer-360-row" key={order.id}>
              <code>{order.order_no}</code>
              <span>{formatFen(order.amount_fen)}</span>
              <OrderStatusBadge status={order.status} />
              <small>{formatDateTime(order.paid_at ?? order.created_at)}</small>
            </div>
          ))
        ) : (
          <Customer360Empty />
        )}
      </Customer360Panel>

      <Customer360Panel title="最近额度流水">
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
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="customer-360-panel">
      <h3>{title}</h3>
      <div>{children}</div>
    </section>
  );
}

function Customer360Empty() {
  return <p className="admin-hint">暂无记录</p>;
}

function CustomerDetailView({
  customer,
  focusGrantSection,
  operatorId,
  onChanged,
  onGranted,
  readOnly,
  refreshError,
  onBack,
}: {
  customer: CustomerListItem;
  focusGrantSection: boolean;
  operatorId: string;
  onChanged: () => void;
  onGranted: (result: AdjustmentWriteResult) => void;
  readOnly: boolean;
  refreshError: string;
  onBack: () => void;
}) {
  // 带加款意图进入时，展开客户即直达「赠送积分」表单，省掉再点一次
  // 「后台加款」滚动按钮；jsdom 没有 scrollIntoView，必须走可选调用。
  useEffect(() => {
    if (!focusGrantSection) return;
    document
      .getElementById("customer-free-grant")
      ?.scrollIntoView?.({ behavior: "smooth", block: "start" });
  }, [focusGrantSection]);

  return (
    <div
      className="customers-page customer-focused-detail"
      id={`customer-detail-${customer.user_id}`}
    >
      {refreshError ? (
        <PageBanner tone="error">{refreshError}</PageBanner>
      ) : null}
      <button
        className="customer-detail-back btn-secondary"
        type="button"
        onClick={onBack}
      >
        ← 返回客户列表
      </button>
      <section className="customer-detail-hero">
        <div className="customer-detail-identity">
          <div aria-hidden="true" className="customer-detail-avatar">
            {customer.username.slice(0, 1)}
          </div>
          <div>
            <div className="customer-detail-title">
              <h1>{customer.username}</h1>
              <CustomerStatusBadge status={customer.status} />
            </div>
            <p>
              客户 ID <CopyCustomerId value={customer.user_id} />
            </p>
            <p>
              公司名称{" "}
              <strong>{customer.display_name ?? customer.username}</strong>
            </p>
            <p>注册时间 {formatDateTime(customer.created_at)}</p>
          </div>
        </div>
        <div className="customer-detail-operations">
          {!readOnly ? (
            <button
              type="button"
              onClick={() =>
                document
                  .getElementById("customer-free-grant")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" })
              }
            >
              后台加款
            </button>
          ) : null}
        </div>
      </section>

      <section aria-label="客户核心指标" className="customer-detail-kpis">
        <article>
          <span>可用额度</span>
          <strong>{customer.available_credits ?? 0}</strong>
          <small>积分</small>
        </article>
        <article>
          <span>累计消耗</span>
          <strong>{customer.credits_spent ?? 0}</strong>
          <small>积分</small>
        </article>
        <article>
          <span>累计生成</span>
          <strong>{customer.generation_total ?? 0}</strong>
          <small>条</small>
        </article>
      </section>

      <AccountCreditPanel
        key={`account:${customer.user_id}:${customer.available_credits}`}
        onChanged={onChanged}
        userId={customer.user_id}
        readOnly={readOnly}
      />
      <Customer360Data
        key={`ledger:${customer.user_id}:${customer.available_credits}`}
        userId={customer.user_id}
      />
      {/* 任务书 C：设备视图（BOUND 设备 + 解绑/吊销凭据）。 */}
      <CustomerDeviceSection readOnly={readOnly} userId={customer.user_id} />
      <div className="customer-detail-settings-grid">
        {customer.activation_code !== "账号注册" && (
          <CustomerPriceEditor readOnly={readOnly} userId={customer.user_id} />
        )}
        <FreeCreditsSection
          key={`free-grant:${operatorId}:${customer.user_id}`}
          onGranted={onGranted}
          operatorId={operatorId}
          readOnly={readOnly}
          userId={customer.user_id}
        />
      </div>
    </div>
  );
}

function CustomerPriceEditor({
  userId,
  readOnly,
}: {
  userId: string;
  readOnly: boolean;
}) {
  const [pricing, setPricing] = useState<CustomerUnitPrice | null>(null);
  const [priceYuan, setPriceYuan] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // null = dialog closed; number = save that price; "reset" = restore default.
  const [pendingWrite, setPendingWrite] = useState<
    { kind: "save"; unitPriceFen: number } | { kind: "reset" } | null
  >(null);
  const [dialogError, setDialogError] = useState("");

  useEffect(() => {
    let cancelled = false;
    void fetchCustomerUnitPrice(userId)
      .then((result) => {
        if (cancelled) {
          return;
        }
        setPricing(result);
        setPriceYuan(formatYuanFromFen(result.unit_price_fen));
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(
            cause instanceof Error && cause.message.trim()
              ? cause.message
              : "读取客户单价失败",
          );
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  function requestSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const unitPriceFen = yuanInputToFen(priceYuan);
    if (unitPriceFen === null) {
      setError("客户售价必须是大于 0、最多两位小数的金额");
      return;
    }
    setError("");
    setDialogError("");
    setPendingWrite({ kind: "save", unitPriceFen });
  }

  function requestReset() {
    setError("");
    setDialogError("");
    setPendingWrite({ kind: "reset" });
  }

  async function persistPrice(reason: string) {
    if (!pendingWrite || saving) {
      return;
    }
    const unitPriceFen =
      pendingWrite.kind === "save" ? pendingWrite.unitPriceFen : null;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await updateCustomerUnitPrice(
        userId,
        unitPriceFen,
        reason,
      );
      setPricing(updated);
      setPriceYuan(formatYuanFromFen(updated.unit_price_fen));
      setNotice(
        unitPriceFen === null ? "已恢复全局默认售价" : "客户售价已保存",
      );
      setPendingWrite(null);
    } catch (cause) {
      setDialogError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "保存客户售价失败",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="客户售价" className="customer-detail-section">
      <h3>客户售价</h3>
      {loading ? <p className="admin-hint">正在读取客户售价…</p> : null}
      {pricing ? (
        <p className="admin-hint">
          当前 {formatFen(pricing.unit_price_fen)} / 秒 · 全局默认{" "}
          {formatFen(pricing.default_unit_price_fen)} / 秒
          {pricing.custom_unit_price_fen === null
            ? "（使用默认）"
            : "（独立定价）"}
        </p>
      ) : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!loading && pricing && !readOnly ? (
        <form className="admin-form" onSubmit={requestSave}>
          <label>
            售价（元/秒）
            <input
              inputMode="decimal"
              min="0.01"
              step="0.01"
              type="number"
              value={priceYuan}
              onChange={(event) => setPriceYuan(event.target.value)}
            />
          </label>
          <div className="admin-actions">
            <button disabled={saving} type="submit">
              {saving ? "正在保存" : "保存客户售价"}
            </button>
            <button
              className="btn-secondary"
              disabled={saving || pricing.custom_unit_price_fen === null}
              type="button"
              onClick={requestReset}
            >
              恢复全局默认
            </button>
          </div>
        </form>
      ) : null}
      {!loading && pricing && readOnly ? (
        <p className="admin-hint">审计员仅可查看定价，不能修改。</p>
      ) : null}

      <ConfirmDialog
        busy={saving}
        confirmLabel={
          pendingWrite?.kind === "reset" ? "确认恢复默认" : "确认保存"
        }
        description={
          pendingWrite?.kind === "reset"
            ? "将清除该客户的独立定价，恢复为全局默认售价。"
            : `将把该客户售价改为 ${pendingWrite ? formatFen(pendingWrite.unitPriceFen) : ""} / 秒（仅影响该客户之后的充值换算）。`
        }
        error={dialogError}
        level="standard"
        open={pendingWrite !== null}
        title={
          pendingWrite?.kind === "reset" ? "恢复全局默认售价" : "修改客户售价"
        }
        onClose={() => {
          setPendingWrite(null);
          setDialogError("");
        }}
        onConfirm={() =>
          void persistPrice(
            pendingWrite?.kind === "reset"
              ? "恢复客户默认售价"
              : "更新客户售价",
          )
        }
      />
    </section>
  );
}

/**
 * 赠送积分发放（FREE_GRANT，054）：为账号发放无收款积分。
 * 走 T23 审计调账闭环——账面金额为 0、钱包照增、自动单号与事由留痕。
 */
function FreeCreditsSection({
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
    const key = crypto.randomUUID();
    setPendingGrant({
      key,
      operatorId,
      userId,
      credits: creditsNumber,
      sourceType,
      sourceRef: `GRANT-${key}`,
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
        `已发放 ${intent.credits} 赠送积分（request id: ${result.request_id}），余额 ${result.wallet_balance_after} 积分`,
      );
      onGranted(result);
      setCredits("");
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
