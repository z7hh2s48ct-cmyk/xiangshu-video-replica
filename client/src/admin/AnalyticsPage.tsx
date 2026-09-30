import { type FormEvent, useEffect, useState } from "react";
import { adminRead, exportBillingReportCsv } from "../api.admin";
import { type BillingAttention, BillingEconomics } from "./BillingEconomics";
import { BusinessDashboard } from "./BusinessDashboard";
import "./economics.css";
import { TabBar } from "./ui/TabBar";

const tabs = [
  { id: "dashboard", label: "经营看板" },
  { id: "cost", label: "成本核对" },
];

/** 镜像服务端 export_controller._MAX_EXPORT_DAYS，用于提交前的本地拦截。 */
const BILLING_REPORT_MAX_DAYS = 90;

/** 与 BillingEconomics 的默认筛选同口径：上海时区的「本月 1 日 → 今天」。 */
function today() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** 与服务端 (end_date - start_date).days 同口径的整数天差；解析失败返回 null。 */
function exportWindowDays(start: string, end: string): number | null {
  const from = Date.parse(`${start}T00:00:00Z`);
  const to = Date.parse(`${end}T00:00:00Z`);
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  return Math.round((to - from) / 86_400_000);
}

/** 服务端 400 之前的本地前置拦截；返回空串表示可以提交。 */
function exportWindowBlocker(start: string, end: string): string {
  if (!start || !end) return "请先选择导出开始与结束日期。";
  const days = exportWindowDays(start, end);
  if (days === null) return "导出日期格式不正确。";
  if (days < 0) return "导出开始日期不能晚于结束日期。";
  if (days > BILLING_REPORT_MAX_DAYS) {
    return `导出区间不能超过 ${BILLING_REPORT_MAX_DAYS} 天（当前 ${days} 天），请缩短后分批导出。`;
  }
  return "";
}

function readableSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

/**
 * 按月对账单 / 利润表导出：`POST /api/control/reports/export`（CSV + gzip）。
 *
 * 该端点只服务 AdminWriter，auditor 会话不渲染本区块（同会话页收敛口径）。
 * 服务端只记录操作人 + 时间窗日志、不消费原因字段，所以这里不放原因输入，
 * 只把窗口上限和产物形态写清楚。
 */
function ReportExport() {
  const [start, setStart] = useState(() => `${today().slice(0, 7)}-01`);
  const [end, setEnd] = useState(today);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    const blocker = exportWindowBlocker(start, end);
    if (blocker) {
      setNotice("");
      setError(blocker);
      return;
    }
    setBusy(true);
    setNotice("");
    setError("");
    try {
      const result = await exportBillingReportCsv({
        start_date: start,
        end_date: end,
      });
      setNotice(`已导出 ${result.filename}（${readableSize(result.bytes)}）。`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "导出计费报表失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-panel" aria-label="计费报表导出">
      <h3>计费报表导出</h3>
      <p>
        导出区间内的逐笔计费明细（CSV，gzip
        压缩；服务端按当前操作人与时间窗记日志）。单次窗口最多{" "}
        {BILLING_REPORT_MAX_DAYS} 天，超出请分批导出。
      </p>
      <form
        className="billing-economics-filters"
        aria-label="计费报表导出条件"
        onSubmit={submit}
      >
        <label>
          导出开始日期
          <input
            type="date"
            value={start}
            onChange={(event) => setStart(event.target.value)}
            required
          />
        </label>
        <label>
          导出结束日期
          <input
            type="date"
            value={end}
            min={start}
            onChange={(event) => setEnd(event.target.value)}
            required
          />
        </label>
        <button type="submit" disabled={busy}>
          {busy ? "正在导出…" : "导出计费报表"}
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {busy && <p role="status">正在导出…</p>}
    </section>
  );
}

/** 成本核对队列卡：点击即把下方明细筛到对应队列，条数与清单同口径。 */
function CostQueues({ onPick }: { onPick: (queue: CostQueue) => void }) {
  const [counts, setCounts] = useState<Record<CostQueue, number | undefined>>({
    pending: undefined,
    unknown_cost: undefined,
    legacy: undefined,
  });
  const [error, setError] = useState("");
  const monthStart = `${today().slice(0, 7)}-01`;

  useEffect(() => {
    let active = true;
    // 队列计数用 limit=1 的明细请求拿 total：与点击后清单完全同口径。
    const queues: { key: CostQueue; query: string }[] = [
      {
        key: "pending",
        query: `start=${monthStart}&end=${today()}&attention=pending&limit=1`,
      },
      {
        key: "unknown_cost",
        query: `start=${monthStart}&end=${today()}&attention=unknown_cost&limit=1`,
      },
      {
        key: "legacy",
        query: `start=2019-01-01&end=${today()}&attention=unknown_cost&limit=1`,
      },
    ];
    void Promise.all(
      queues.map(async ({ key, query }) =>
        adminRead<{ total: number }>(
          `/api/control/billing/operations?${query}`,
          "读取待核对队列失败",
        ).then((result) => [key, result.total] as const),
      ),
    )
      .then((entries) => {
        if (active)
          setCounts(Object.fromEntries(entries) as Record<CostQueue, number>);
      })
      .catch((cause: unknown) => {
        if (active)
          setError(
            cause instanceof Error ? cause.message : "读取待核对队列失败",
          );
      });
    return () => {
      active = false;
    };
  }, [monthStart]);

  const cards: { key: CostQueue; label: string }[] = [
    { key: "pending", label: "待结算" },
    { key: "unknown_cost", label: "待核对成本" },
    { key: "legacy", label: "历史待核对" },
  ];
  return (
    <section
      className="economics-kpis economics-kpis--four"
      aria-label="待核对队列"
    >
      {error && <p role="alert">{error}</p>}
      {cards.map((card) => (
        <article key={card.key}>
          <button type="button" onClick={() => onPick(card.key)}>
            <span>{card.label}</span>
            <strong>{counts[card.key] ?? "…"}</strong>
          </button>
        </article>
      ))}
    </section>
  );
}

type CostQueue = "pending" | "unknown_cost" | "legacy";

/** 经营分析 v2：经营看板（老板看结论）与成本核对（财务处理待核对）两页签。 */
export function AnalyticsPage({
  readOnly = false,
  initialTab = "dashboard",
  initialAttention = "",
  initialStart,
}: {
  readOnly?: boolean;
  initialTab?: "dashboard" | "cost";
  initialAttention?: BillingAttention;
  initialStart?: string;
}) {
  const [tab, setTab] = useState<string>(initialTab);
  const [queue, setQueue] = useState<{ key: CostQueue; seq: number }>();
  return (
    <div>
      <TabBar
        active={tab}
        ariaLabel="经营分析页签"
        items={tabs}
        onChange={setTab}
      />
      {tab === "cost" && (
        <CostQueues
          onPick={(key) => setQueue({ key, seq: (queue?.seq ?? 0) + 1 })}
        />
      )}
      {tab === "cost" && (
        <BillingEconomics
          key={`cost:${queue?.seq ?? 0}`}
          initialAttention={
            queue?.key === "pending"
              ? "pending"
              : queue?.key === "legacy" || queue?.key === "unknown_cost"
                ? "unknown_cost"
                : initialAttention
          }
          initialStart={queue?.key === "legacy" ? "2019-01-01" : initialStart}
          readOnly={readOnly}
          view="cost"
        />
      )}
      {tab === "dashboard" && (
        <>
          <BusinessDashboard readOnly={readOnly} />
          {/* 导出端点要求 AdminWriter，auditor 会话不渲染入口。 */}
          {readOnly ? null : <ReportExport />}
        </>
      )}
    </div>
  );
}
