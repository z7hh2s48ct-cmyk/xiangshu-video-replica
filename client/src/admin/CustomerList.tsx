import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { downloadCustomersCsv } from "../api";
import {
  type CustomerListItem,
  type CustomerListOptions,
  type CustomerListResponse,
  listCustomers,
} from "../api.admin";
import { CustomerLink } from "./CustomerLink";
import { CopyCustomerId } from "./ui/CopyCustomerId";
import { PageBanner } from "./ui/PageBanner";
import { Pagination } from "./ui/Pagination";
import { CustomerStatusBadge } from "./ui/StatusBadge";
import { formatDateTime, formatFen, parseUtcTimestamp } from "./ui/vocabulary";
import "./admin-customer-detail.css";

import { CustomerDetail } from "./CustomerDetail";
import { CustomerTagPills } from "./CustomerIdentity";
export interface CustomerListProps {
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
  initialCustomerId?: string;
  initialListQuery?: string;
  onScopeChange?: (scope: Record<string, string>) => void;
  onCustomer?: (userId: string) => void;
  onReturnToList?: () => void;
}

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

export function CustomerList({
  embedded = false,
  operatorId = "standalone-admin",
  readOnly = false,
  initialIntent = "",
  initialCustomerId = "",
  initialListQuery = "",
  onScopeChange,
  onCustomer,
  onReturnToList,
}: CustomerListProps = {}) {
  const initial = new URLSearchParams(initialListQuery);
  const initialFilters = {
    username: initial.get("username") ?? "",
    status: initial.get("status") ?? "all",
    createdFrom: initial.get("createdFrom") ?? "",
    createdTo: initial.get("createdTo") ?? "",
    balanceMin: initial.get("balanceMin") ?? "",
    balanceMax: initial.get("balanceMax") ?? "",
    threshold: initial.get("threshold") ?? "",
  };
  const [customers, setCustomers] = useState<CustomerListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [offset, setOffset] = useState(
    Math.max(0, Number(initial.get("offset")) || 0),
  );
  const [total, setTotal] = useState(0);
  const [usernameDraft, setUsernameDraft] = useState(initialFilters.username);
  const [statusFilter, setStatusFilter] = useState(initialFilters.status);
  const [createdFrom, setCreatedFrom] = useState(initialFilters.createdFrom);
  const [createdTo, setCreatedTo] = useState(initialFilters.createdTo);
  const [balanceMin, setBalanceMin] = useState(initialFilters.balanceMin);
  const [balanceMax, setBalanceMax] = useState(initialFilters.balanceMax);
  const [threshold, setThreshold] = useState(initialFilters.threshold);
  const [sort, setSort] = useState<CustomerListOptions["sort"]>(
    (initial.get("sort") as CustomerListOptions["sort"]) || "activated",
  );
  const [direction, setDirection] = useState<"asc" | "desc">(
    initial.get("direction") === "asc" ? "asc" : "desc",
  );
  const [attention, setAttention] = useState<CustomerListOptions["attention"]>(
    (initial.get("attention") as CustomerListOptions["attention"]) || "",
  );
  const [attentionCounts, setAttentionCounts] =
    useState<CustomerListResponse["attention_counts"]>();
  const [expandedUserId, setExpandedUserId] = useState<string | null>(
    initialCustomerId || null,
  );
  const [linkedCustomerId, setLinkedCustomerId] = useState(initialCustomerId);
  const [filters, setFilters] = useState(initialFilters);
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
        userId: linkedCustomerId || undefined,
        limit: PAGE_SIZE,
        offset: linkedCustomerId ? 0 : offset,
        ...(!linkedCustomerId
          ? {
              username_filter: filters.username || undefined,
              status: filters.status === "all" ? undefined : filters.status,
              createdFrom: filters.createdFrom || undefined,
              createdTo: filters.createdTo || undefined,
              balanceMin: filters.balanceMin
                ? Number(filters.balanceMin)
                : undefined,
              balanceMax: filters.balanceMax
                ? Number(filters.balanceMax)
                : undefined,
              sort,
              direction,
              attention,
              lowBalanceThreshold:
                filters.threshold !== ""
                  ? Number(filters.threshold)
                  : undefined,
            }
          : {}),
      });
      if (sequence !== requestId.current) return;
      setCustomers(response.items);
      setTotal(response.total);
      setAttentionCounts(response.attention_counts);
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
  }, [filters, offset, linkedCustomerId, sort, direction, attention]);

  useEffect(() => {
    if (linkedCustomerId) return;
    const query = new URLSearchParams({
      ...filters,
      offset: String(offset),
      sort: sort || "activated",
      direction,
      attention: attention || "",
    });
    onScopeChange?.({ listQuery: query.toString() });
  }, [
    filters,
    offset,
    sort,
    direction,
    attention,
    linkedCustomerId,
    onScopeChange,
  ]);

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
    setLinkedCustomerId("");
    setFilters({
      username: usernameDraft.trim(),
      status: statusFilter,
      createdFrom,
      createdTo,
      balanceMin,
      balanceMax,
      threshold,
    });
  };

  function openCustomer(userId: string) {
    if (onCustomer) onCustomer(userId);
    else setExpandedUserId(userId);
  }
  function changeSort(next: CustomerListOptions["sort"]) {
    setOffset(0);
    setSort(next);
    setDirection(sort === next && direction === "desc" ? "asc" : "desc");
  }

  const exportList = () => {
    void downloadCustomersCsv({
      status: filters.status === "all" ? undefined : filters.status,
      username: filters.username || undefined,
      createdFrom: filters.createdFrom || undefined,
      createdTo: filters.createdTo || undefined,
      balanceMin: filters.balanceMin ? Number(filters.balanceMin) : undefined,
      balanceMax: filters.balanceMax ? Number(filters.balanceMax) : undefined,
      attention,
      sort,
      direction,
      lowBalanceThreshold:
        filters.threshold !== "" ? Number(filters.threshold) : undefined,
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
      <CustomerDetail
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
        onBack={() => {
          if (onReturnToList) onReturnToList();
          else {
            setExpandedUserId(null);
            setLinkedCustomerId("");
          }
        }}
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
        <label className="admin-toolbar__field">
          <span>低余额阈值（积分）</span>
          <input
            aria-label="低余额阈值"
            type="number"
            min="0"
            max="2147483647"
            step="1"
            placeholder="未配置"
            value={threshold}
            onChange={(event) => setThreshold(event.target.value)}
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
          {!readOnly && (
            <button type="button" onClick={exportList}>
              导出列表 CSV
            </button>
          )}
        </div>
      </form>

      <section aria-label="需要处理" className="customer-attention-cards">
        {(
          [
            ["low_balance", "余额不足", attentionCounts?.low_balance],
            ["recent_failure", "近7天有失败", attentionCounts?.recent_failure],
            ["inactive", "30天未活跃", attentionCounts?.inactive],
          ] as const
        ).map(([id, label, count]) => (
          <button
            key={id}
            type="button"
            aria-pressed={attention === id}
            disabled={loading || count == null}
            onClick={() => {
              setAttention(id);
              setOffset(0);
            }}
          >
            <span>{label}</span>
            <strong>{loading ? "…" : (count ?? "未配置")}</strong>
          </button>
        ))}
      </section>
      <p className="admin-hint">
        全量当前筛选客户，不受分页影响。
        {filters.threshold !== ""
          ? `低余额：严格小于 ${filters.threshold} 积分。`
          : "低余额阈值未配置，输入阈值并筛选后启用。"}
        生成成功率按近30天创建任务计算；30天未活跃使用登录、心跳、生成和账本活动。
        {attention && (
          <button
            type="button"
            onClick={() => {
              setAttention("");
              setOffset(0);
            }}
          >
            显示全部客户
          </button>
        )}
      </p>
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
                  <th>客户</th>
                  <th>负责人</th>
                  <th>状态与权益</th>
                  <th>可用积分</th>
                  {(
                    [
                      ["recharge", "累计充值"],
                      ["month_consumed", "本月消耗"],
                    ] as const
                  ).map(([id, label]) => (
                    <th
                      key={id}
                      aria-sort={
                        sort === id
                          ? direction === "desc"
                            ? "descending"
                            : "ascending"
                          : "none"
                      }
                    >
                      <button type="button" onClick={() => changeSort(id)}>
                        {label}{" "}
                        {sort === id ? (direction === "desc" ? "↓" : "↑") : "↕"}
                      </button>
                    </th>
                  ))}
                  <th>近30天生成</th>
                  <th
                    aria-sort={
                      sort === "last_active"
                        ? direction === "desc"
                          ? "descending"
                          : "ascending"
                        : "none"
                    }
                  >
                    <button
                      type="button"
                      onClick={() => changeSort("last_active")}
                    >
                      最近活跃{" "}
                      {sort === "last_active"
                        ? direction === "desc"
                          ? "↓"
                          : "↑"
                        : "↕"}
                    </button>
                  </th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {customers.map((customer) => (
                  <tr key={customer.user_id}>
                    <td data-label="客户">
                      <CustomerLink
                        userId={customer.user_id}
                        company={customer.display_name}
                        username={customer.username}
                        onCustomer={openCustomer}
                      />
                      <CustomerTagPills tags={customer.tags ?? []} />
                      <CopyCustomerId compact value={customer.user_id} />
                    </td>
                    <td data-label="负责人">
                      {customer.owner_username || "未指定"}
                    </td>
                    <td data-label="状态与权益">
                      <CustomerStatusBadge status={customer.status} />
                      <small>{customer.current_benefit || "原价"}</small>
                    </td>
                    <td data-label="可用积分">
                      <strong
                        className={
                          customer.low_balance ? "customer-low-balance" : ""
                        }
                      >
                        {customer.available_credits ?? 0} 积分
                      </strong>
                      {customer.low_balance && (
                        <small className="customer-low-balance">低于阈值</small>
                      )}
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
                    <td data-label="近30天生成">
                      <strong>
                        {customer.success_rate_30d == null
                          ? "无统计"
                          : `${customer.success_rate_30d.toFixed(1)}%`}
                      </strong>
                      <small>
                        {customer.generation_total_30d
                          ? `${customer.generation_succeeded_30d ?? 0} / ${customer.generation_total_30d} 次`
                          : "无任务"}
                      </small>
                      {(customer.generation_failed_30d ?? 0) > 0 && (
                        <span className="customer-failure-badge">
                          失败 {customer.generation_failed_30d}
                        </span>
                      )}
                    </td>
                    <td data-label="最近活跃">
                      <time
                        dateTime={customer.last_active_at}
                        title={formatDateTime(customer.last_active_at)}
                      >
                        {relativeActivity(customer.last_active_at)}
                      </time>
                    </td>
                    <td data-label="操作">
                      <button
                        type="button"
                        aria-controls={`customer-detail-${customer.user_id}`}
                        aria-expanded="false"
                        onClick={() => openCustomer(customer.user_id)}
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

function relativeActivity(value?: string) {
  if (!value) return "暂无活动";
  const age = Date.now() - parseUtcTimestamp(value);
  if (!Number.isFinite(age)) return "时间未知";
  if (age < 60000) return "刚刚";
  if (age < 3600000) return `${Math.floor(age / 60000)} 分钟前`;
  if (age < 86400000) return `${Math.floor(age / 3600000)} 小时前`;
  return `${Math.floor(age / 86400000)} 天前`;
}
