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
  type CustomerAnnotation,
  type CustomerListItem,
  type CustomerOwnerCandidate,
  type CustomerUnitPrice,
  createCustomerAdjustment,
  fetchCustomerAnnotation,
  fetchCustomerUnitPrice,
  listAdminRechargeOrders,
  listAdminWalletTransactions,
  listCustomerOwnerCandidates,
  listCustomers,
  resumeCustomer,
  suspendCustomer,
  updateCustomerAnnotation,
  updateCustomerUnitPrice,
} from "../api.admin";
import { yuanInputToFen } from "../rechargePackageDisplay";
import { AccountCreditPanel } from "./AccountCreditPanel";
import { CustomerActivitySection } from "./CustomerActivitySection";
import { CustomerBenefitsSection } from "./CustomerBenefitsSection";
import { CustomerDeviceSection } from "./CustomerDeviceSection";
import { CustomerRefundSection } from "./CustomerRefundSection";
import { GenerationRecordsPage } from "./GenerationRecordsPage";
import { RecordCallsPanel } from "./RecordCallsPanel";
import { SessionsPage } from "./SessionsPage";
import { ConfirmDialog } from "./ui/ConfirmDialog";
import { CopyCustomerId } from "./ui/CopyCustomerId";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { CustomerStatusBadge, OrderStatusBadge } from "./ui/StatusBadge";
import { TabBar } from "./ui/TabBar";
import {
  formatDateTime,
  formatFen,
  formatYuanFromFen,
  transactionTypeLabel,
} from "./ui/vocabulary";
import "./admin-customer-detail.css";

/** 公司名称未填写时的显式占位。
 *
 * 不静默回退成用户名：那样既与相邻的「用户名」列重复，又掩盖了"这个账号还没填
 * 公司名"这一运营信号——而该信号的用处正是提醒运营去催客户补填。列表与详情共用
 * 此函数，避免同一字段在两处显示不同值。
 */
const COMPANY_NAME_FALLBACK = "未填写";

function companyNameOf(customer: CustomerListItem): string {
  return customer.display_name || COMPANY_NAME_FALLBACK;
}

/** 列表内标签列最多摆 3 个 pill，余量收成 +N（列宽有限，全摆会被截断）。 */
const TAG_PILL_LIMIT = 3;

function CustomerTagPills({ tags }: { tags: string[] }) {
  if (tags.length === 0) {
    return <span className="customer-cell-muted">—</span>;
  }
  const visible = tags.slice(0, TAG_PILL_LIMIT);
  const hidden = tags.length - visible.length;
  return (
    <span className="customer-cell-tags" title={tags.join("、")}>
      {visible.map((tag) => (
        <span className="customer-tag-pill" key={tag}>
          {tag}
        </span>
      ))}
      {hidden > 0 ? <span className="customer-tag-pill">+{hidden}</span> : null}
    </span>
  );
}

interface CustomersPageProps {
  embedded?: boolean;
  operatorId?: string;
  readOnly?: boolean;
  /**
   * 导航意图（AdminApp 从 hash `?intent=` 解析后透传）。总览快捷入口跳转到
   * 客户管理后必须"有下文"，否则管理员只看到一个与上下文无关的客户列表：
   * - customerPackage（开通套餐·已收款）/ customerAdjustments（赠送积分）/
   *   customerRefund（退款扣减）：展开客户时自动定位到对应表单。收款开通计入
   *   收入、赠送不计收入，两者分开入口，避免把收款误记成赠送；
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

/** 总览快捷操作 → 客户详情里的目标区块与引导文案。 */
const INTENT_TARGETS: Record<string, { sectionId: string; label: string }> = {
  customerPackage: {
    sectionId: "customer-benefits",
    label: "开通套餐（已收款）",
  },
  customerAdjustments: { sectionId: "customer-free-grant", label: "赠送积分" },
  customerRefund: { sectionId: "customer-refund", label: "退款扣减" },
};

function scrollToSection(sectionId: string) {
  // jsdom 没有 scrollIntoView，必须走可选调用。
  document
    .getElementById(sectionId)
    ?.scrollIntoView?.({ behavior: "smooth", block: "start" });
}

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
  // 资金意图只在可写角色下引导：auditor 的详情页没有这些表单。
  const intentTarget = readOnly
    ? null
    : (INTENT_TARGETS[initialIntent] ?? null);

  // 总览快捷操作跳进来后直接落在用户名筛选上，管理员可以立刻输入客户名，
  // 而不是先自己找筛选框。
  useEffect(() => {
    if (intentTarget) {
      usernameFilterRef.current?.focus();
    }
  }, [intentTarget]);

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
        focusSectionId={intentTarget?.sectionId ?? null}
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

      {intentTarget ? (
        <PageBanner tone="notice">
          {intentTarget.label}
          在客户详情内完成：先筛选并展开目标客户，页面会自动定位到「
          {intentTarget.label}」区块。
        </PageBanner>
      ) : null}

      <form
        className="admin-toolbar customer-list-filters"
        onSubmit={handleFilterSubmit}
      >
        <label className="admin-toolbar__field">
          {/* 关键字同时匹配用户名与公司名称（服务端 u.username OR
              u.display_name）：运营的识别路径是「这家公司是哪个账号」，
              只按用户名筛选会让公司名搜不到。 */}
          <span>用户名 / 公司名称</span>
          <input
            placeholder="按用户名或公司名称筛选"
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
                  <th>标签</th>
                  <th>负责人</th>
                  <th>客户 ID</th>
                  <th>注册时间</th>
                  <th>状态</th>
                  <th>当前权益</th>
                  <th>可用积分</th>
                  <th>累计充值</th>
                  <th>本月消耗</th>
                  <th>生成情况</th>
                  <th>最近活跃</th>
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
                        title={companyNameOf(customer)}
                      >
                        {companyNameOf(customer)}
                      </span>
                    </td>
                    <td data-label="标签">
                      <CustomerTagPills tags={customer.tags ?? []} />
                    </td>
                    <td data-label="负责人">
                      {customer.owner_username ? (
                        <span
                          className="customer-cell-ellipsis"
                          title={customer.owner_username}
                        >
                          {customer.owner_username}
                        </span>
                      ) : (
                        <span className="customer-cell-muted">未指定</span>
                      )}
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
                    <td data-label="当前权益">
                      {customer.current_benefit || (
                        <span className="customer-cell-muted">原价</span>
                      )}
                    </td>
                    <td data-label="可用积分">
                      <strong>{customer.available_credits ?? 0} 积分</strong>
                    </td>
                    <td
                      aria-label={`${customer.username} 累计充值`}
                      data-label="累计充值"
                    >
                      {formatFen(customer.total_recharge_fen ?? 0)}
                    </td>
                    <td data-label="本月消耗">
                      {customer.month_consumed_credits ?? 0} 积分
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
                    <td data-label="最近活跃">
                      <time
                        className="customer-cell-date"
                        dateTime={customer.last_active_at}
                      >
                        {customer.last_active_at
                          ? formatDateTime(customer.last_active_at).split(
                              " ",
                            )[0]
                          : "—"}
                      </time>
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

// 充值订单不进生成记录列表（它不是生成任务），但第三方调用同源落库：
// #9 要求用订单号也能反查这张单出网调了什么，支付回调报文就在原始响应里。
const RECHARGE_ORDER_RECORD_TYPE = "RECHARGE_ORDER";

function Customer360Data({
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
function CustomerSuspendButton({ customer }: { customer: CustomerListItem }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const suspended = customer.status === "SUSPENDED";
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
      window.alert(
        suspended
          ? `已恢复 ${companyNameOf(customer)}，客户可重新登录。`
          : `已暂停 ${companyNameOf(customer)}，当前会话已下线，余额保持不变。`,
      );
      window.location.reload();
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

/** 详情页签（方案 P1 六页签）：对任一客户的全部操作都在详情页完成。 */
const CUSTOMER_DETAIL_TABS = [
  { id: "overview", label: "概览" },
  { id: "funds", label: "充值与积分" },
  { id: "records", label: "生成记录" },
  { id: "devices", label: "登录与设备" },
  { id: "benefits", label: "价格与权益" },
  { id: "activity", label: "操作记录" },
] as const;

type CustomerDetailTab = (typeof CUSTOMER_DETAIL_TABS)[number]["id"];

/** 资金操作目标区块 → 所属页签：顶部主操作按钮先切页签再滚动定位。 */
const SECTION_TAB: Record<string, CustomerDetailTab> = {
  "customer-benefits": "benefits",
  "customer-free-grant": "funds",
  "customer-refund": "funds",
  "customer-annotation": "overview",
  "customer-activity": "activity",
  "customer-devices": "devices",
};

function CustomerDetailView({
  customer,
  focusSectionId,
  operatorId,
  onChanged,
  onGranted,
  readOnly,
  refreshError,
  onBack,
}: {
  customer: CustomerListItem;
  focusSectionId: string | null;
  operatorId: string;
  onChanged: () => void;
  onGranted: (result: AdjustmentWriteResult) => void;
  readOnly: boolean;
  refreshError: string;
  onBack: () => void;
}) {
  // 带资金意图进入时，先切到对应页签，展开客户即直达表单。
  const [tab, setTab] = useState<CustomerDetailTab>(
    (focusSectionId && SECTION_TAB[focusSectionId]) || "overview",
  );
  const [pendingSection, setPendingSection] = useState<string | null>(
    focusSectionId,
  );
  // 切页签后再滚动：目标区块随页签挂载，同帧滚动会落空。
  useEffect(() => {
    if (!pendingSection) return;
    scrollToSection(pendingSection);
    setPendingSection(null);
  }, [tab, pendingSection]);
  useEffect(() => {
    if (!focusSectionId) return;
    setTab(SECTION_TAB[focusSectionId] ?? "overview");
    setPendingSection(focusSectionId);
  }, [focusSectionId]);

  function focusSection(sectionId: string) {
    const target = SECTION_TAB[sectionId];
    if (target && target !== tab) {
      setTab(target);
      setPendingSection(sectionId);
    } else {
      scrollToSection(sectionId);
    }
  }

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
              公司名称 <strong>{companyNameOf(customer)}</strong>
            </p>
            <p>注册时间 {formatDateTime(customer.created_at)}</p>
          </div>
        </div>
        <div className="customer-detail-operations">
          {/* 收款开通计入收入、赠送不计收入：两个入口分开，避免把客户付过的
              钱误记成赠送（方案 P0-1）；退款扣减从会话页迁到这里（P0-2）。 */}
          {!readOnly ? (
            <>
              <button
                type="button"
                onClick={() => focusSection("customer-benefits")}
              >
                开通套餐（已收款）
              </button>
              <button
                type="button"
                onClick={() => focusSection("customer-free-grant")}
              >
                赠送积分
              </button>
              <button
                type="button"
                onClick={() => focusSection("customer-refund")}
              >
                退款扣减
              </button>
              <CustomerSuspendButton customer={customer} />
            </>
          ) : null}
        </div>
      </section>

      <section aria-label="客户核心指标" className="customer-detail-kpis">
        <article>
          <span>可用积分</span>
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

      <TabBar
        active={tab}
        ariaLabel="客户详情页签"
        items={CUSTOMER_DETAIL_TABS.map(({ id, label }) => ({ id, label }))}
        onChange={(id) => setTab(id as CustomerDetailTab)}
      />

      {tab === "overview" ? (
        <>
          <CustomerAnnotationSection
            key={`annotation:${customer.user_id}`}
            onChanged={onChanged}
            readOnly={readOnly}
            userId={customer.user_id}
          />
          {/* 概览的「最近动态」复用操作记录视图（P0-3 的客户侧审计），
              两个页签各自挂载，key 区分避免状态互串。 */}
          <CustomerActivitySection
            key={`overview-activity:${customer.user_id}`}
            userId={customer.user_id}
          />
        </>
      ) : null}

      {tab === "funds" ? (
        <>
          <AccountCreditPanel
            key={`account:${customer.user_id}:${customer.available_credits}`}
            onChanged={onChanged}
            userId={customer.user_id}
            readOnly={readOnly}
          />
          <Customer360Data
            key={`ledger:${customer.user_id}:${customer.available_credits}`}
            readOnly={readOnly}
            userId={customer.user_id}
          />
          <div className="customer-detail-settings-grid">
            <FreeCreditsSection
              key={`free-grant:${operatorId}:${customer.user_id}`}
              onGranted={onGranted}
              operatorId={operatorId}
              readOnly={readOnly}
              userId={customer.user_id}
            />
            <CustomerRefundSection
              key={`refund:${customer.user_id}`}
              availableCredits={customer.available_credits ?? 0}
              onRefunded={onGranted}
              readOnly={readOnly}
              userId={customer.user_id}
            />
          </div>
        </>
      ) : null}

      {tab === "records" ? (
        <GenerationRecordsPage
          key={`records:${customer.user_id}`}
          initialUsername={customer.username}
          readOnly={readOnly}
        />
      ) : null}

      {tab === "devices" ? (
        <>
          {/* 任务书 C：设备视图（BOUND 设备 + 解绑/吊销凭据）。 */}
          <CustomerDeviceSection
            readOnly={readOnly}
            userId={customer.user_id}
          />
          {/* 该客户的在线会话（嵌入模式只看这一个客户）。 */}
          <SessionsPage readOnly={readOnly} userId={customer.user_id} />
        </>
      ) : null}

      {tab === "benefits" ? (
        <>
          <CustomerBenefitsSection
            key={`benefits:${customer.user_id}`}
            onChanged={onChanged}
            readOnly={readOnly}
            userId={customer.user_id}
          />
          {customer.activation_code !== "账号注册" && (
            <CustomerPriceEditor
              readOnly={readOnly}
              userId={customer.user_id}
            />
          )}
        </>
      ) : null}

      {tab === "activity" ? (
        /* 方案 P0-3：客户自己的动作（建项目、读素材等）——与管理员处置共用
           同一张审计表，这里按 scope=customer + 客户 ID 取出属于自己的部分。 */
        <CustomerActivitySection
          key={`activity:${customer.user_id}`}
          userId={customer.user_id}
        />
      ) : null}
    </div>
  );
}

/** 标签输入按中英文逗号 / 顿号拆分：运营在单行输入框里随手打分隔符。 */
function splitTagInput(raw: string): string[] {
  const tags: string[] = [];
  for (const piece of raw.split(/[,，、]/)) {
    const tag = piece.trim();
    if (tag && !tags.includes(tag)) {
      tags.push(tag);
    }
  }
  return tags;
}

/** 与迁移 / 服务端同口径（20260929T1000：jsonb_array_length <= 10）。 */
const MAX_ANNOTATION_TAGS = 10;

/**
 * 客户标注（方案 P2-3）：标签 / 备注 / 负责人。
 *
 * 整体替换语义，表单即现状（不做展示 / 编辑双态）——三项全空时服务端删行，
 * 列表回到「未标注」。保存走 ConfirmDialog + 固定事由，与定价 / 调账同写契约。
 */
function CustomerAnnotationSection({
  userId,
  readOnly,
  onChanged,
}: {
  userId: string;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const [annotation, setAnnotation] = useState<CustomerAnnotation | null>(null);
  const [candidates, setCandidates] = useState<CustomerOwnerCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tagsDraft, setTagsDraft] = useState("");
  const [noteDraft, setNoteDraft] = useState("");
  const [ownerDraft, setOwnerDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [dialogError, setDialogError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    void Promise.all([
      fetchCustomerAnnotation(userId),
      listCustomerOwnerCandidates(),
    ])
      .then(([current, ownerCandidates]) => {
        if (cancelled) {
          return;
        }
        setAnnotation(current);
        setCandidates(ownerCandidates.items);
        setTagsDraft(current.tags.join("，"));
        setNoteDraft(current.note);
        setOwnerDraft(current.owner_user_id);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setError(
            cause instanceof Error && cause.message.trim()
              ? cause.message
              : "读取客户标注失败",
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
    if (splitTagInput(tagsDraft).length > MAX_ANNOTATION_TAGS) {
      setError(`标签最多 ${MAX_ANNOTATION_TAGS} 个`);
      return;
    }
    setError("");
    setDialogError("");
    setDialogOpen(true);
  }

  async function persistAnnotation(reason: string) {
    if (saving) {
      return;
    }
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const updated = await updateCustomerAnnotation(
        userId,
        {
          tags: splitTagInput(tagsDraft),
          note: noteDraft.trim(),
          owner_user_id: ownerDraft || null,
        },
        reason,
      );
      setAnnotation(updated);
      setTagsDraft(updated.tags.join("，"));
      setNoteDraft(updated.note);
      setOwnerDraft(updated.owner_user_id);
      setNotice(
        updated.tags.length === 0 && !updated.note && !updated.owner_user_id
          ? "客户标注已清空"
          : "客户标注已保存",
      );
      setDialogOpen(false);
      onChanged();
    } catch (cause) {
      setDialogError(
        cause instanceof Error && cause.message.trim()
          ? cause.message
          : "保存客户标注失败",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="客户标注" className="customer-detail-section">
      <h3>客户标注</h3>
      {loading ? <p className="admin-hint">正在读取客户标注…</p> : null}
      {error ? <PageBanner tone="error">{error}</PageBanner> : null}
      {notice ? <PageBanner tone="notice">{notice}</PageBanner> : null}
      {!loading && annotation ? (
        readOnly ? (
          <div className="customer-annotation-view">
            <p>
              <span className="customer-annotation-label">标签</span>
              <CustomerTagPills tags={annotation.tags} />
            </p>
            <p>
              <span className="customer-annotation-label">负责人</span>
              {annotation.owner_username || "未指定"}
            </p>
            <p>
              <span className="customer-annotation-label">备注</span>
              <span className="customer-annotation-note">
                {annotation.note || "—"}
              </span>
            </p>
            {annotation.updated_at ? (
              <p className="admin-hint">
                最后更新 {formatDateTime(annotation.updated_at)}
              </p>
            ) : null}
            <p className="admin-hint">审计员仅可查看标注，不能修改。</p>
          </div>
        ) : (
          <form className="admin-form" onSubmit={requestSave}>
            <label>
              标签（逗号分隔，最多 {MAX_ANNOTATION_TAGS} 个）
              <input
                placeholder="如：VIP，重点客户"
                type="text"
                value={tagsDraft}
                onChange={(event) => setTagsDraft(event.target.value)}
              />
            </label>
            <label>
              负责人
              <select
                value={ownerDraft}
                onChange={(event) => setOwnerDraft(event.target.value)}
              >
                <option value="">未指定</option>
                {candidates.map((candidate) => (
                  <option key={candidate.user_id} value={candidate.user_id}>
                    {candidate.display_name || candidate.username}
                  </option>
                ))}
              </select>
            </label>
            <label>
              备注（最多 2000 字，仅运营可见）
              <textarea
                rows={3}
                value={noteDraft}
                onChange={(event) => setNoteDraft(event.target.value)}
              />
            </label>
            {annotation.updated_at ? (
              <p className="admin-hint">
                最后更新 {formatDateTime(annotation.updated_at)}
              </p>
            ) : null}
            <div className="admin-actions">
              <button disabled={saving} type="submit">
                {saving ? "正在保存" : "保存标注"}
              </button>
            </div>
          </form>
        )
      ) : null}

      <ConfirmDialog
        busy={saving}
        confirmLabel="确认保存"
        description="将整体替换该客户的标签、负责人与备注；三项全部清空会删除标注记录。"
        error={dialogError}
        level="standard"
        open={dialogOpen}
        title="保存客户标注"
        onClose={() => {
          setDialogOpen(false);
          setDialogError("");
        }}
        onConfirm={() => void persistAnnotation("更新客户标注")}
      />
    </section>
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
 *
 * 客服工单 / 补偿审批两类补偿（服务端同为正向口径）也从这里发起：区别只在
 * 来源单据——必须挂真实工单号 / 审批单号，事后能回溯到客服与审批流程。
 */
const REFERENCED_GRANT_SOURCES = [
  "CS_TICKET",
  "COMPENSATION_APPROVAL",
] as const;

function needsSourceRef(sourceType: string): boolean {
  return (REFERENCED_GRANT_SOURCES as readonly string[]).includes(sourceType);
}

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
        `已发放 ${intent.credits} 赠送积分（request id: ${result.request_id}），余额 ${result.wallet_balance_after} 积分`,
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
