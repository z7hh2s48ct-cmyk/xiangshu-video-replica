import { useEffect, useRef, useState } from "react";
import { adminRead, downloadBillingCsv } from "../api.admin";
import { BillingEvidenceForm } from "./BillingEvidenceForm";
import {
  type BillingService,
  billingStates,
  billingUnit,
} from "./billingTypes";
import { SourceActionPanorama } from "./SourceActionPanorama";
import { formatDateTime, formatFen } from "./ui/vocabulary";
import { ViralCollectionBilling } from "./ViralCollectionBilling";

type Metric = {
  period: string | null;
  operation_count: number;
  provider_call_count: number;
  shared_collection_charge_count: number;
  charged_credits: number;
  known_revenue_fen: number | string | null;
  known_cost_fen: number | string | null;
  profit_fen: number | string | null;
  unknown_cost_count: number;
  unknown_revenue_count: number;
  pending_count: number;
  legacy_cost_count?: number;
  legacy_settlement_count?: number;
  refunded_credits: number;
  seconds: string | null;
  images: string | null;
  calls: string | null;
  platform_cost_fen: string | null;
};
type Report = { totals: Metric; periods: Metric[]; basis: string };
type Operation = {
  id: string;
  user_id: string | null;
  collection_batch_id: string | null;
  username: string;
  service: string;
  unit: keyof typeof billingUnit;
  budget_units: string;
  actual_units: string | null;
  charged_credits: number;
  reserved_credits: number;
  revenue_fen: string | null;
  nominal_revenue_fen: string | null;
  cost_fen: string | null;
  profit_fen: string | null;
  state: string;
  completed_at: string | null;
  created_at: string;
  pricing_snapshot_json: string;
  attempts?: Attempt[];
  evidence?: {
    id: string;
    reference: string;
    reason: string;
    created_at: string;
  }[];
};
type Attempt = {
  effective_cost_fen: string | null;
  evidence_reference: string | null;
  id: string;
  service: string;
  provider: string;
  unit: keyof typeof billingUnit;
  unit_cost_fen: string | null;
  usage: string | null;
  cost_fen: string | null;
  state: string;
};
// P2-1：金额统一走 formatFen——两位小数，不足 1 分显示 < ¥0.01；
// 原实现除以 100 后留 10 位小数，运营对账得自己数字符。
const money = (value: string | number | null | undefined) =>
  value == null ? "待核对" : formatFen(Number(value));
const usageFormat = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 1,
  maximumFractionDigits: 1,
  useGrouping: false,
});
const usage = (
  value: string | number | null | undefined,
  pending = "待确认",
) =>
  value == null || !Number.isFinite(Number(value))
    ? pending
    : usageFormat.format(Number(value));
const modules: Record<string, string> = {
  video: "视频生成",
  replica: "视频分析",
  replacement: "首帧制作",
  people: "人物与声音",
  copy: "文案创作",
  oral: "数字人口播",
  viral: "爆款数据",
  workbench: "链接导入",
  internal: "内部检查",
  platform: "平台后台",
  infrastructure: "基础服务",
};
function today() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}
export type BillingAttention = "" | "pending" | "unknown_cost";
// attention 保持枚举类型而不是擦成 string：下拉值经 narrowAttention 窄化，
// 请求参数与服务端 attention 枚举始终同口径。
type BillingFilters = {
  start: string;
  end: string;
  grain: string;
  user_id: string;
  service: string;
  module: string;
  provider: string;
  attention: BillingAttention;
};
function narrowAttention(value: string): BillingAttention {
  return value === "pending" || value === "unknown_cost" ? value : "";
}
const initialFilters = (attention: BillingAttention = ""): BillingFilters => ({
  // 带着总览「今日」待办进来时只看今天，条数才与待办计数一致。
  start: attention ? today() : `${today().slice(0, 7)}-01`,
  end: today(),
  grain: "day",
  user_id: "",
  service: "",
  module: "",
  provider: "",
  // 总览待办跳进来时预置：清单条数与待办计数同口径（服务端 attention 参数）。
  attention,
});
function params(filters: ReturnType<typeof initialFilters>) {
  return new URLSearchParams(
    Object.entries(filters).filter(([, value]) => value),
  ).toString();
}

export function BillingEconomics({
  readOnly = false,
  view = "profit",
  initialAttention = "",
}: {
  readOnly?: boolean;
  view?: "profit" | "cost";
  initialAttention?: BillingAttention;
}) {
  const costView = view === "cost";
  const detailRequest = useRef(0);
  const [filters, setFilters] = useState(() =>
    initialFilters(initialAttention),
  );
  const [query, setQuery] = useState(() =>
    params(initialFilters(initialAttention)),
  );
  const [revision, setRevision] = useState(0);
  const [offset, setOffset] = useState(0);
  const [catalog, setCatalog] = useState<BillingService[]>([]);
  const [report, setReport] = useState<Report>();
  const [operations, setOperations] = useState<{
    items: Operation[];
    total: number;
  }>();
  const [detail, setDetail] = useState<Operation>();
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [showCollections, setShowCollections] = useState(false);
  const [showActions, setShowActions] = useState(false);
  const name = (key: string) =>
    catalog.find((item) => item.service === key)?.name ?? key;
  useEffect(() => {
    let active = true;
    void adminRead<{ services: BillingService[] }>(
      "/api/control/billing/catalog",
      "读取业务失败",
    )
      .then((result) => {
        if (active) setCatalog(result.services);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取业务失败");
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    void revision;
    let active = true;
    setBusy(true);
    setError("");
    detailRequest.current += 1;
    setDetail(undefined);
    void Promise.all([
      adminRead<Report>(
        `/api/control/billing/statistics?${query}`,
        "读取经营统计失败",
      ),
      adminRead<{ items: Operation[]; total: number }>(
        `/api/control/billing/operations?${query}&limit=100&offset=${offset}`,
        "读取生成明细失败",
      ),
    ])
      .then(([summary, items]) => {
        if (active) {
          setReport(summary);
          setOperations(items);
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setReport(undefined);
          setOperations(undefined);
          setError(cause instanceof Error ? cause.message : "读取失败");
        }
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, [query, offset, revision]);
  async function inspect(id: string) {
    const sequence = ++detailRequest.current;
    try {
      const result = await adminRead<Operation>(
        `/api/control/billing/operations/${encodeURIComponent(id)}`,
        "读取生成详情失败",
      );
      if (sequence === detailRequest.current) setDetail(result);
    } catch (cause) {
      if (sequence === detailRequest.current)
        setError(cause instanceof Error ? cause.message : "读取失败");
    }
  }
  async function exportRows() {
    try {
      setNotice(await downloadBillingCsv(query));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出失败");
    }
  }
  const snapshot = detail
    ? (JSON.parse(detail.pricing_snapshot_json) as {
        unit_credits: string;
        unit_rounding: string;
        discount_basis_points: number;
        enabled: boolean;
        version: number;
      })
    : undefined;
  return (
    <section
      className="admin-panel billing-economics"
      aria-label={costView ? "成本明细" : "利润总览"}
    >
      <button
        type="button"
        onClick={() => setShowCollections((value) => !value)}
      >
        {showCollections ? "收起采集账单" : "查看爆款采集账单"}
      </button>
      {showCollections && <ViralCollectionBilling key={query} query={query} />}
      <button type="button" onClick={() => setShowActions((value) => !value)}>
        {showActions ? "收起操作全景" : "查看操作全景"}
      </button>
      {showActions && (
        <SourceActionPanorama key={query} query={query} name={name} />
      )}
      <form
        className="billing-economics-filters"
        onSubmit={(event) => {
          event.preventDefault();
          setQuery(params(filters));
          setOffset(0);
          setRevision((value) => value + 1);
        }}
      >
        <label>
          开始日期
          <input
            type="date"
            value={filters.start}
            onChange={(event) =>
              setFilters({ ...filters, start: event.target.value })
            }
            required
          />
        </label>
        <label>
          结束日期
          <input
            type="date"
            value={filters.end}
            min={filters.start}
            onChange={(event) =>
              setFilters({ ...filters, end: event.target.value })
            }
            required
          />
        </label>
        <label>
          统计周期
          <select
            value={filters.grain}
            onChange={(event) =>
              setFilters({ ...filters, grain: event.target.value })
            }
          >
            <option value="day">天</option>
            <option value="week">周（周一开始）</option>
            <option value="month">月 / 多月</option>
            <option value="year">年</option>
          </select>
        </label>
        <label>
          用户 ID
          <input
            value={filters.user_id}
            onChange={(event) =>
              setFilters({ ...filters, user_id: event.target.value })
            }
            placeholder="全部用户及平台后台"
          />
        </label>
        <label>
          业务
          <select
            value={filters.service}
            onChange={(event) =>
              setFilters({ ...filters, service: event.target.value })
            }
          >
            <option value="">全部业务</option>
            {catalog.map((item) => (
              <option key={item.service} value={item.service}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          业务模块
          <select
            value={filters.module}
            onChange={(event) =>
              setFilters({ ...filters, module: event.target.value })
            }
          >
            <option value="">全部模块</option>
            {Object.entries(modules).map(([key, label]) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          涉及服务商
          <select
            value={filters.provider}
            onChange={(event) =>
              setFilters({ ...filters, provider: event.target.value })
            }
          >
            <option value="">全部服务商</option>
            {[...new Set(catalog.map((item) => item.provider))].map((key) => (
              <option key={key}>{key}</option>
            ))}
          </select>
        </label>
        <label>
          只看
          <select
            value={filters.attention}
            onChange={(event) =>
              setFilters({
                ...filters,
                attention: narrowAttention(event.target.value),
              })
            }
          >
            <option value="">全部生成</option>
            <option value="pending">待结算</option>
            <option value="unknown_cost">成本待核对</option>
          </select>
        </label>
        <div className="billing-economics-filters__actions">
          <button type="submit" disabled={busy}>
            查询
          </button>
          <button
            type="button"
            disabled={busy || !operations}
            onClick={() => void exportRows()}
          >
            导出当前查询 CSV
          </button>
        </div>
      </form>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {busy && <p role="status">正在计算…</p>}
      {report && (
        <>
          <p>{report.basis} 时区：北京时间。</p>
          <p>
            供应商调用记录 {report.totals.provider_call_count ?? 0}{" "}
            次；客户采集计费 {report.totals.shared_collection_charge_count ?? 0}{" "}
            笔。 客户累计用量（含免费）：{usage(report.totals.seconds ?? 0)} 秒
            / {usage(report.totals.images ?? 0)} 张 /{" "}
            {usage(report.totals.calls ?? 0)} 次。平台承担的已确认成本：
            {money(report.totals.platform_cost_fen ?? 0)}。
          </p>
          {(report.totals.legacy_cost_count ?? 0) +
            (report.totals.legacy_settlement_count ?? 0) >
            0 && (
            <p role="status">
              该日期及客户范围内有 {report.totals.legacy_cost_count ?? 0}{" "}
              条历史成本、{report.totals.legacy_settlement_count ?? 0}{" "}
              条历史结算待核对。
            </p>
          )}
          <div className="economics-kpis economics-kpis--four">
            {(costView
              ? [
                  ["已确认成本", money(report.totals.known_cost_fen ?? 0)],
                  ["平台承担成本", money(report.totals.platform_cost_fen ?? 0)],
                  ["待核对成本", `${report.totals.unknown_cost_count} 项`],
                  ["生成次数", `${report.totals.operation_count} 次`],
                ]
              : [
                  ["确认收入", money(report.totals.known_revenue_fen ?? 0)],
                  ["已确认成本", money(report.totals.known_cost_fen ?? 0)],
                  ["利润", money(report.totals.profit_fen)],
                  ["净扣积分", String(report.totals.charged_credits ?? 0)],
                ]
            ).map(([label, value]) => (
              <article key={label}>
                <span>{label}</span>
                <strong>{value}</strong>
              </article>
            ))}
          </div>
          <p className="admin-hint">
            待核对成本 {report.totals.unknown_cost_count} 项 · 待核对收入{" "}
            {report.totals.unknown_revenue_count} 项 · 未结算{" "}
            {report.totals.pending_count} 项
          </p>
          <div className="admin-table-scroll">
            <table
              className="admin-data-table billing-economics-table"
              aria-label="周期汇总"
            >
              <thead>
                <tr>
                  <th>周期起始</th>
                  <th>账务记录数</th>
                  <th>{costView ? "秒 / 张 / 次" : "净扣积分"}</th>
                  {!costView && <th>确认收入</th>}
                  <th>已确认成本</th>
                  <th>{costView ? "平台承担成本" : "利润"}</th>
                </tr>
              </thead>
              <tbody>
                {report.periods.map((row) => (
                  <tr key={row.period}>
                    <td>{row.period}</td>
                    <td>{row.operation_count}</td>
                    <td>
                      {costView
                        ? `${usage(row.seconds ?? 0)} / ${usage(row.images ?? 0)} / ${usage(row.calls ?? 0)}`
                        : row.charged_credits}
                    </td>
                    {!costView && <td>{money(row.known_revenue_fen ?? 0)}</td>}
                    <td>{money(row.known_cost_fen ?? 0)}</td>
                    <td>
                      {money(
                        costView
                          ? (row.platform_cost_fen ?? 0)
                          : row.profit_fen,
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {operations && (
        <>
          <h3>生成账务明细（共 {operations.total} 条）</h3>
          {!costView && (
            <p className="admin-hint">
              按售价折合以受理时的积分售价计算；确认收入按所消费积分对应的实际充值金额分摊，
              赠送或免费加款不产生确认收入。利润按确认收入减成本计算。
            </p>
          )}
          <div className="admin-table-scroll">
            <table
              className="admin-data-table billing-economics-table"
              aria-label="生成明细"
            >
              <thead>
                <tr>
                  <th>用户</th>
                  <th>业务</th>
                  <th>状态</th>
                  <th>用量</th>
                  {!costView && (
                    <>
                      <th>积分</th>
                      <th>按售价折合</th>
                      <th>确认收入</th>
                    </>
                  )}
                  <th>成本</th>
                  {!costView && <th>利润</th>}
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {operations.items.map((row) => (
                  <tr key={row.id}>
                    <td>{row.username}</td>
                    <td>{name(row.service)}</td>
                    <td>{billingStates[row.state]}</td>
                    <td>
                      {usage(row.actual_units, "处理中")}{" "}
                      {billingUnit[row.unit]}
                    </td>
                    {!costView && (
                      <>
                        <td>{row.charged_credits}</td>
                        <td>{money(row.nominal_revenue_fen)}</td>
                        <td>{money(row.revenue_fen)}</td>
                      </>
                    )}
                    <td>{money(row.cost_fen)}</td>
                    {!costView && <td>{money(row.profit_fen)}</td>}
                    <td>
                      <button
                        type="button"
                        onClick={() => void inspect(row.id)}
                      >
                        查看详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {operations.total === 0 && <p>这个时间范围没有生成记录。</p>}
          <button
            type="button"
            disabled={busy || offset === 0}
            onClick={() => setOffset((value) => Math.max(0, value - 100))}
          >
            上一页
          </button>
          <span>第 {Math.floor(offset / 100) + 1} 页</span>
          <button
            type="button"
            disabled={busy || offset + 100 >= operations.total}
            onClick={() => setOffset((value) => value + 100)}
          >
            下一页
          </button>
        </>
      )}
      {detail && snapshot && (
        <aside aria-label="生成核算详情">
          <button
            type="button"
            onClick={() => {
              detailRequest.current += 1;
              setDetail(undefined);
            }}
          >
            关闭详情
          </button>
          <h3>
            {name(detail.service)} · {detail.username}
          </h3>
          <p>
            生成编号：{detail.id} · {billingStates[detail.state]}
          </p>
          {detail.collection_batch_id && (
            <p>
              采集批次：{detail.collection_batch_id}
              。公共成本记在平台生成记录，客户明细仅记录其扣分收入；批次账单汇总成本与利润。
            </p>
          )}
          <p>
            受理时售价：{snapshot.enabled ? snapshot.unit_credits : "0"} 积分 /{" "}
            {billingUnit[detail.unit]}；折扣{" "}
            {snapshot.discount_basis_points / 1000} 折；价格版本{" "}
            {snapshot.version}。
          </p>
          <p>
            预算 {usage(detail.budget_units)} {billingUnit[detail.unit]}，实际{" "}
            {usage(detail.actual_units)} {billingUnit[detail.unit]}；暂扣{" "}
            {detail.reserved_credits}，净扣 {detail.charged_credits} 积分。
          </p>
          <p>
            按售价折合 {money(detail.nominal_revenue_fen)} · 确认收入{" "}
            {money(detail.revenue_fen)} · 成本 {money(detail.cost_fen)} · 利润{" "}
            {money(detail.profit_fen)}
          </p>
          {!readOnly &&
            detail.state === "PENDING" &&
            ["video_768p", "video_2k", "oral", "asr"].includes(
              detail.service,
            ) && (
              <BillingEvidenceForm
                key={detail.id}
                operationId={detail.id}
                onSaved={() => setRevision((value) => value + 1)}
              />
            )}
          <h4>供应商调用（重试分别记成本）</h4>
          <table>
            <thead>
              <tr>
                <th>业务</th>
                <th>服务商</th>
                <th>用量</th>
                <th>成本单价</th>
                <th>成本</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {detail.attempts?.map((item) => (
                <tr key={item.id}>
                  <td>{name(item.service)}</td>
                  <td>{item.provider}</td>
                  <td>
                    {usage(item.usage)} {billingUnit[item.unit]}
                  </td>
                  <td>
                    {money(item.unit_cost_fen)} / {billingUnit[item.unit]}
                  </td>
                  <td>{money(item.effective_cost_fen)}</td>
                  <td>
                    {item.evidence_reference
                      ? "凭据已核对"
                      : billingStates[item.state]}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {!readOnly &&
            detail.state !== "PENDING" &&
            detail.attempts
              ?.filter((item) => item.effective_cost_fen == null)
              .map((item) => (
                <section key={item.id}>
                  <h4>
                    核对 {name(item.service)} · 调用 {item.id}
                  </h4>
                  <BillingEvidenceForm
                    operationId={detail.id}
                    attemptId={item.id}
                    onSaved={() => setRevision((value) => value + 1)}
                  />
                </section>
              ))}
          {detail.evidence?.map((item) => (
            <p key={item.id}>
              核对记录：{item.reference} · {item.reason} ·{" "}
              {formatDateTime(item.created_at)}
            </p>
          ))}
        </aside>
      )}
    </section>
  );
}
