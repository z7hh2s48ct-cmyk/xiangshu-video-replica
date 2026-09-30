import { useCallback, useEffect, useState } from "react";
import { adminRead } from "../api.admin";
import { formatFen } from "./ui/vocabulary";

type OverviewMetrics = {
  recharge_fen: number;
  paying_customers: number;
  new_paying_customers: number;
  revenue_fen: number;
  cost_fen: number;
  unknown_cost_count: number;
  pending_count: number;
  prepaid_credits: number;
  prepaid_fen: number | null;
};

type BusinessOverview = {
  start: string;
  end: string;
  prev_start: string;
  prev_end: string;
  metrics: OverviewMetrics;
  prev: OverviewMetrics;
  daily: { day: string; revenue_fen: number | null; cost_fen: number | null }[];
  modules: {
    service: string;
    label: string;
    revenue_fen: number;
    cost_fen: number;
    unknown_cost_count: number;
    pending_count: number;
  }[];
  top_customers: {
    user_id: string;
    username: string;
    display_name: string;
    revenue_fen: number;
  }[];
};

const pad = (value: number) => String(value).padStart(2, "0");

/** 上海挂钟「今天」（en-CA 即 YYYY-MM-DD），全站筛选同口径。 */
function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

function shiftDays(iso: string, days: number): string {
  const from = new Date(`${iso}T00:00:00Z`);
  from.setUTCDate(from.getUTCDate() + days);
  return from.toISOString().slice(0, 10);
}

function weekStart(iso: string): string {
  const from = new Date(`${iso}T00:00:00Z`);
  const weekday = from.getUTCDay() || 7; // 周一为一周开始
  return shiftDays(iso, 1 - weekday);
}

function prevMonthRange(iso: string): [string, string] {
  const year = Number(iso.slice(0, 4));
  const month = Number(iso.slice(5, 7));
  const pm = month === 1 ? 12 : month - 1;
  const py = month === 1 ? year - 1 : year;
  const lastDay = new Date(Date.UTC(py, pm, 0)).getUTCDate();
  return [`${py}-${pad(pm)}-01`, `${py}-${pad(pm)}-${pad(lastDay)}`];
}

type Preset = "today" | "week" | "month" | "lastMonth" | "custom";

function presetRange(preset: Preset, current: { start: string; end: string }) {
  const today = shanghaiToday();
  switch (preset) {
    case "today":
      return { start: today, end: today };
    case "week":
      return { start: weekStart(today), end: today };
    case "month":
      return { start: `${today.slice(0, 7)}-01`, end: today };
    case "lastMonth": {
      const [start, end] = prevMonthRange(today);
      return { start, end };
    }
    case "custom":
      return current;
  }
}

/** 环比：上一等长区间为 0 时无从算百分比，按方向文案如实表达。 */
function delta(cur: number, prev: number): string {
  if (cur === prev) return "持平";
  if (prev === 0) return "上一区间为 0";
  const pct = Math.round(((cur - prev) / prev) * 100);
  return `${pct > 0 ? "+" : ""}${pct}%`;
}

/** 经营看板（方案 P1）：给老板的一屏结论——8 张指标卡、按日趋势、业务构成、Top 客户。
 *  毛利与客单价由前端按卡片口径计算；确认收入/成本沿用服务端已知金额口径，
 *  缺证据的请求只进「待核对」条数，绝不冒充零成本或零收入。 */
export function BusinessDashboard({
  readOnly = false,
}: {
  readOnly?: boolean;
}) {
  const [preset, setPreset] = useState<Preset>("month");
  const [custom, setCustom] = useState(() => ({
    start: `${shanghaiToday().slice(0, 7)}-01`,
    end: shanghaiToday(),
  }));
  const range = presetRange(preset, custom);
  const [overview, setOverview] = useState<BusinessOverview>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");

  const load = useCallback(async () => {
    setBusy(true);
    setError("");
    try {
      const query = new URLSearchParams({ start: range.start, end: range.end });
      const result = await adminRead<BusinessOverview>(
        `/api/control/business/overview?${query}`,
        "读取经营看板失败",
      );
      setOverview(result);
    } catch (cause) {
      setOverview(undefined);
      setError(cause instanceof Error ? cause.message : "读取经营看板失败");
    } finally {
      setBusy(false);
    }
  }, [range.start, range.end]);

  useEffect(() => {
    void load();
  }, [load]);

  const metrics = overview?.metrics;
  const prev = overview?.prev;
  const gross =
    metrics && prev
      ? { fen: metrics.revenue_fen - metrics.cost_fen }
      : undefined;
  const grossPrev = prev ? prev.revenue_fen - prev.cost_fen : undefined;
  const attentionCount =
    metrics && prev ? metrics.unknown_cost_count + metrics.pending_count : 0;
  const avgOrder =
    metrics && metrics.paying_customers > 0
      ? metrics.recharge_fen / metrics.paying_customers
      : null;
  const avgOrderPrev =
    prev && prev.paying_customers > 0
      ? prev.recharge_fen / prev.paying_customers
      : null;

  const cards: {
    label: string;
    value: string;
    sub?: string;
    delta?: string;
    warn?: boolean;
  }[] = [];
  if (metrics && prev) {
    cards.push(
      {
        label: "充值实收",
        value: formatFen(metrics.recharge_fen),
        delta: delta(metrics.recharge_fen, prev.recharge_fen),
      },
      {
        label: "确认收入",
        value: formatFen(metrics.revenue_fen),
        sub: "客户已消耗积分对应的实付金额，赠送积分不计",
        delta: delta(metrics.revenue_fen, prev.revenue_fen),
      },
      {
        label: "成本",
        value: formatFen(metrics.cost_fen),
        sub:
          metrics.unknown_cost_count > 0
            ? `含 ${metrics.unknown_cost_count} 项待核对`
            : undefined,
        delta: delta(metrics.cost_fen, prev.cost_fen),
      },
      {
        label: "毛利 / 毛利率",
        value: formatFen(gross?.fen ?? 0),
        sub:
          attentionCount > 0
            ? `含 ${attentionCount} 项待核对`
            : metrics.revenue_fen > 0
              ? `毛利率 ${Math.round(((gross?.fen ?? 0) / metrics.revenue_fen) * 100)}%`
              : undefined,
        delta: delta(gross?.fen ?? 0, grossPrev ?? 0),
        warn: attentionCount > 0,
      },
      {
        label: "付费客户数",
        value: `${metrics.paying_customers}`,
        sub: "区间内有实付充值的客户",
        delta: delta(metrics.paying_customers, prev.paying_customers),
      },
      {
        label: "新增付费客户",
        value: `${metrics.new_paying_customers}`,
        sub: "首次实付充值落在区间的客户",
        delta: delta(metrics.new_paying_customers, prev.new_paying_customers),
      },
      {
        label: "客单价",
        value: avgOrder === null ? "—" : formatFen(avgOrder),
        delta:
          avgOrder === null || avgOrderPrev === null
            ? undefined
            : delta(avgOrder, avgOrderPrev),
      },
      {
        label: "预收积分余额",
        value: `${metrics.prepaid_credits} 积分`,
        sub:
          metrics.prepaid_fen === null
            ? "未配置积分兑换比例，暂不折算金额"
            : `折合 ${formatFen(metrics.prepaid_fen)}`,
        delta: delta(metrics.prepaid_credits, prev.prepaid_credits),
      },
    );
  }

  const daily = overview?.daily ?? [];
  const maxDaily = Math.max(
    1,
    ...daily.map((day) => Math.max(day.revenue_fen ?? 0, day.cost_fen ?? 0)),
  );

  async function copyCustomerId(userId: string) {
    try {
      await navigator.clipboard.writeText(userId);
      setCopied(userId);
    } catch {
      setCopied("");
    }
  }

  return (
    <section className="admin-panel business-dashboard" aria-label="经营看板">
      <form
        className="billing-economics-filters"
        aria-label="经营看板时间选择"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <label>
          时间
          <select
            value={preset}
            onChange={(event) => setPreset(event.target.value as Preset)}
          >
            <option value="today">今日</option>
            <option value="week">本周</option>
            <option value="month">本月</option>
            <option value="lastMonth">上月</option>
            <option value="custom">自定义</option>
          </select>
        </label>
        {preset === "custom" && (
          <>
            <label>
              开始日期
              <input
                type="date"
                value={custom.start}
                onChange={(event) =>
                  setCustom({ ...custom, start: event.target.value })
                }
                required
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                value={custom.end}
                min={custom.start}
                onChange={(event) =>
                  setCustom({ ...custom, end: event.target.value })
                }
                required
              />
            </label>
            <button type="submit" disabled={busy}>
              查询
            </button>
          </>
        )}
      </form>
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">正在计算…</p>}
      {metrics && prev && (
        <>
          <p className="admin-hint">
            统计区间 {overview?.start} 至 {overview?.end}；环比对照{" "}
            {overview?.prev_start} 至 {overview?.prev_end}。时区：北京时间。
          </p>
          <section
            className="economics-kpis economics-kpis--four"
            aria-label="经营指标卡"
          >
            {cards.map((card) => (
              <article
                key={card.label}
                className={card.warn ? "business-card--warn" : undefined}
              >
                <span>{card.label}</span>
                <strong>{card.value}</strong>
                {card.sub && <small>{card.sub}</small>}
                {card.delta && <small>环比 {card.delta}</small>}
              </article>
            ))}
          </section>
          <h3>收入与成本按日趋势</h3>
          {daily.length === 0 ? (
            <p>这个区间没有消耗记录。</p>
          ) : (
            <svg
              className="business-trend"
              viewBox={`0 0 ${daily.length * 24} 160`}
              role="img"
              aria-label="收入与成本按日趋势图，柱为收入与成本"
              preserveAspectRatio="none"
            >
              {daily.map((day, index) => {
                const revenue = day.revenue_fen ?? 0;
                const cost = day.cost_fen ?? 0;
                const x = index * 24;
                return (
                  <g key={day.day}>
                    <title>{`${day.day}：确认收入 ${formatFen(revenue)}，成本 ${day.cost_fen === null ? "待核对" : formatFen(cost)}`}</title>
                    <rect
                      x={x + 2}
                      y={140 - (revenue / maxDaily) * 120}
                      width={8}
                      height={(revenue / maxDaily) * 120}
                      className="business-trend__revenue"
                    />
                    <rect
                      x={x + 12}
                      y={140 - (cost / maxDaily) * 120}
                      width={8}
                      height={(cost / maxDaily) * 120}
                      className="business-trend__cost"
                    />
                    <text x={x + 2} y={158} fontSize={7}>
                      {index === 0 || index === daily.length - 1
                        ? day.day.slice(5)
                        : ""}
                    </text>
                  </g>
                );
              })}
            </svg>
          )}
          <p className="admin-hint">
            柱：确认收入 / 成本。成本或收入证据未齐的日期，金额按已知部分显示，
            待核对条数见上方指标卡。
          </p>
          <h3>业务构成</h3>
          <div className="admin-table-scroll">
            <table
              className="admin-data-table"
              aria-label="业务模块收入成本构成"
            >
              <thead>
                <tr>
                  <th>业务模块</th>
                  <th>确认收入</th>
                  <th>成本</th>
                  <th>毛利</th>
                </tr>
              </thead>
              <tbody>
                {overview?.modules.map((module) => (
                  <tr key={module.service}>
                    <td>{module.label}</td>
                    <td>{formatFen(module.revenue_fen)}</td>
                    <td>
                      {formatFen(module.cost_fen)}
                      {module.unknown_cost_count > 0 && (
                        <>（含 {module.unknown_cost_count} 项待核对）</>
                      )}
                    </td>
                    <td>{formatFen(module.revenue_fen - module.cost_fen)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>Top 10 客户（按确认收入）</h3>
          <div className="admin-table-scroll">
            <table className="admin-data-table" aria-label="Top 10 客户">
              <thead>
                <tr>
                  <th>客户</th>
                  <th>确认收入</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {overview?.top_customers.map((customer) => (
                  <tr key={customer.user_id}>
                    <td>
                      {customer.display_name || customer.username}
                      <small>（{customer.username}）</small>
                    </td>
                    <td>{formatFen(customer.revenue_fen)}</td>
                    <td>
                      {/* 编号只用于复制：找人靠名称，不靠记 ID。 */}
                      {!readOnly && (
                        <button
                          type="button"
                          onClick={() => void copyCustomerId(customer.user_id)}
                        >
                          {copied === customer.user_id ? "已复制" : "复制编号"}
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      {!metrics && !busy && !error && <p>这个区间没有经营数据。</p>}
    </section>
  );
}
