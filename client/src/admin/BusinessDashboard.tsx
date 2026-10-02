import { useCallback, useEffect, useRef, useState } from "react";
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
  unknown_revenue_count?: number;
  legacy_cost_count?: number;
  legacy_settlement_count?: number;
  gross_fen?: number | null;
  margin_pct?: number | null;
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
  daily: {
    day: string;
    revenue_fen: number | null;
    cost_fen: number | null;
    known_revenue_fen?: number | null;
    known_cost_fen?: number | null;
    margin_pct?: number | null;
    unknown_cost_count?: number;
    unknown_revenue_count?: number;
    legacy_cost_count?: number;
    legacy_settlement_count?: number;
    pending_count?: number;
  }[];
  modules: {
    service: string;
    label: string;
    revenue_fen: number;
    cost_fen: number;
    unknown_cost_count: number;
    pending_count: number;
    unknown_revenue_count?: number;
    legacy_cost_count?: number;
    legacy_settlement_count?: number;
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
  range: controlledRange,
  onRangeChange,
  onCustomer,
}: {
  readOnly?: boolean;
  range?: { start: string; end: string };
  onRangeChange?: (range: { start: string; end: string }) => void;
  onCustomer?: (userId: string) => void;
}) {
  const [preset, setPreset] = useState<Preset>(
    controlledRange &&
      (controlledRange.start !== `${shanghaiToday().slice(0, 7)}-01` ||
        controlledRange.end !== shanghaiToday())
      ? "custom"
      : "month",
  );
  const [custom, setCustom] = useState(() => ({
    start: controlledRange?.start ?? `${shanghaiToday().slice(0, 7)}-01`,
    end: controlledRange?.end ?? shanghaiToday(),
  }));
  const range = controlledRange ?? presetRange(preset, custom);
  const [overview, setOverview] = useState<BusinessOverview>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState("");

  const requestSequence = useRef(0);
  const load = useCallback(async () => {
    const sequence = ++requestSequence.current;
    setOverview(undefined);
    setBusy(true);
    setError("");
    try {
      const query = new URLSearchParams({ start: range.start, end: range.end });
      const result = await adminRead<BusinessOverview>(
        `/api/control/business/overview?${query}`,
        "读取经营看板失败",
      );
      if (sequence === requestSequence.current) setOverview(result);
    } catch (cause) {
      if (sequence !== requestSequence.current) return;
      setOverview(undefined);
      setError(cause instanceof Error ? cause.message : "读取经营看板失败");
    } finally {
      if (sequence === requestSequence.current) setBusy(false);
    }
  }, [range.start, range.end]);

  useEffect(() => {
    void load();
    return () => {
      requestSequence.current += 1;
    };
  }, [load]);

  const metrics = overview?.metrics;
  const prev = overview?.prev;
  const gross =
    metrics && prev
      ? { fen: metrics.revenue_fen - metrics.cost_fen }
      : undefined;
  const grossPrev = prev ? prev.revenue_fen - prev.cost_fen : undefined;
  const attentionCount =
    metrics && prev
      ? metrics.unknown_cost_count +
        metrics.pending_count +
        (metrics.unknown_revenue_count ?? 0) +
        (metrics.legacy_cost_count ?? 0) +
        (metrics.legacy_settlement_count ?? 0)
      : 0;
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
    basis?: string;
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
        value: attentionCount > 0 ? "待核对" : formatFen(gross?.fen ?? 0),
        sub:
          attentionCount > 0
            ? `毛利率：待核对 · 含 ${attentionCount} 项待核对`
            : metrics.revenue_fen > 0
              ? `毛利率 ${Math.round(((gross?.fen ?? 0) / metrics.revenue_fen) * 100)}%`
              : "毛利率：无收入，无法计算",
        delta:
          attentionCount > 0 ||
          prev.unknown_cost_count +
            prev.pending_count +
            (prev.unknown_revenue_count ?? 0) +
            (prev.legacy_cost_count ?? 0) +
            (prev.legacy_settlement_count ?? 0) >
            0
            ? undefined
            : delta(gross?.fen ?? 0, grossPrev ?? 0),
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

  const bases = [
    "区间内已支付且金额大于零的充值，含线下实收；赠送、补偿、注册奖励不计。",
    "所消费积分对应的实付金额；赠送积分不产生收入。这里只合计已知收入。",
    "实际调用或已核对凭据的已知成本；重试分别计入，未知金额不当作零。",
    "确认收入减成本；毛利率为毛利除以确认收入。收入、成本或结算证据缺失时待核对，收入为零时不计算。",
    "区间内至少一次金额大于零的实付充值，按稳定客户编号去重。",
    "客户全历史首笔金额大于零的实付充值发生在当前区间；先赠送后实付仍以实付日期计。",
    "区间充值实收除以区间付费客户数；没有付费客户时不计算。",
    "当前全部客户可用积分加冻结积分，是当前时点余额；金额依当前配置兑换比例，不是历史月末余额。",
  ];
  cards.forEach((card, index) => {
    card.basis = bases[index];
  });
  const daily = overview?.daily ?? [];
  const trendRef = useRef<SVGSVGElement>(null);
  const [trendWidth, setTrendWidth] = useState(600);
  useEffect(() => {
    if (daily.length === 0) return;
    const element = trendRef.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver((entries) =>
      setTrendWidth(Math.max(300, entries[0].contentRect.width)),
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [daily.length]);
  const maxDaily = Math.max(
    1,
    ...daily.map((day) =>
      Math.max(
        day.known_revenue_fen ?? day.revenue_fen ?? 0,
        day.known_cost_fen ?? day.cost_fen ?? 0,
      ),
    ),
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
            onChange={(event) => {
              const next = event.target.value as Preset;
              setPreset(next);
              onRangeChange?.(presetRange(next, custom));
            }}
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
                value={range.start}
                onChange={(event) => {
                  const next = { ...range, start: event.target.value };
                  setCustom(next);
                  onRangeChange?.(next);
                }}
                required
              />
            </label>
            <label>
              结束日期
              <input
                type="date"
                value={range.end}
                min={range.start}
                onChange={(event) => {
                  const next = { ...range, end: event.target.value };
                  setCustom(next);
                  onRangeChange?.(next);
                }}
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
            className="economics-kpis economics-kpis--four business-kpis"
            aria-label="经营指标卡"
          >
            {cards.map((card, index) => (
              <article
                key={card.label}
                className={card.warn ? "business-card--warn" : undefined}
              >
                <span>
                  {card.label}{" "}
                  <button
                    type="button"
                    className="metric-help"
                    aria-label={`${card.label}口径`}
                    aria-describedby={`business-basis-${index}`}
                  >
                    ⓘ
                    <span role="tooltip" id={`business-basis-${index}`}>
                      {card.basis}
                    </span>
                  </button>
                </span>
                <strong>{card.value}</strong>
                {card.sub &&
                  ["成本", "毛利 / 毛利率", "预收积分余额"].includes(
                    card.label,
                  ) && <small>{card.sub}</small>}
                {card.delta && <small>环比 {card.delta}</small>}
              </article>
            ))}
          </section>
          <section className="business-trend-panel" aria-label="经营趋势">
            <h3>收入、成本与毛利率按日趋势</h3>
            {daily.length === 0 ? (
              <p>这个区间没有消耗记录。</p>
            ) : (
              <svg
                className="business-trend"
                ref={trendRef}
                viewBox={`0 0 ${trendWidth} 130`}
                role="img"
                aria-label="收入与成本按日趋势图，柱为收入与成本，折线为毛利率"
                preserveAspectRatio="none"
              >
                {(() => {
                  const width = trendWidth;
                  const step = (width - 60) / daily.length;
                  const rates = daily.map((day) => {
                    if (
                      (day.unknown_cost_count ?? 0) +
                        (day.unknown_revenue_count ?? 0) +
                        (day.legacy_cost_count ?? 0) +
                        (day.legacy_settlement_count ?? 0) +
                        (day.pending_count ?? 0) >
                        0 ||
                      day.revenue_fen == null ||
                      day.cost_fen == null ||
                      day.revenue_fen <= 0
                    )
                      return null;
                    return (
                      day.margin_pct ??
                      ((day.revenue_fen - day.cost_fen) / day.revenue_fen) * 100
                    );
                  });
                  const minRate = Math.min(
                    0,
                    ...rates.filter((v): v is number => v != null),
                  );
                  const maxRate = Math.max(
                    100,
                    ...rates.filter((v): v is number => v != null),
                  );
                  const y = (rate: number) =>
                    100 - ((rate - minRate) / (maxRate - minRate)) * 85;
                  let path = "";
                  rates.forEach((rate, i) => {
                    if (rate !== null)
                      path += `${i === 0 || rates[i - 1] === null ? "M" : "L"}${30 + (i + 0.5) * step},${y(rate)} `;
                  });
                  return (
                    <>
                      <text x={0} y={12} fontSize={10}>
                        {formatFen(maxDaily)}
                      </text>
                      <text x={width - 28} y={12} fontSize={10}>
                        {Math.round(maxRate)}%
                      </text>
                      <text x={width - 28} y={105} fontSize={10}>
                        {Math.round(minRate)}%
                      </text>
                      {daily.map((day, i) => {
                        const revenue =
                          day.known_revenue_fen ?? day.revenue_fen ?? 0;
                        const cost = day.known_cost_fen ?? day.cost_fen ?? 0;
                        const x = 30 + i * step;
                        const detail = `${day.day}：已知确认收入 ${formatFen(revenue)}，已知成本 ${formatFen(cost)}，毛利率 ${rates[i] === null ? "待核对或无收入" : `${Math.round(rates[i] ?? 0)}%`}`;
                        return (
                          <g key={day.day} tabIndex={0} aria-label={detail}>
                            <title>{detail}</title>
                            <rect
                              x={x + step * 0.12}
                              y={100 - (revenue / maxDaily) * 85}
                              width={step * 0.28}
                              height={(revenue / maxDaily) * 85}
                              className="business-trend__revenue"
                            />
                            <rect
                              x={x + step * 0.43}
                              y={100 - (cost / maxDaily) * 85}
                              width={step * 0.28}
                              height={(cost / maxDaily) * 85}
                              className="business-trend__cost"
                            />
                            {rates[i] != null && (
                              <circle
                                cx={x + step * 0.5}
                                cy={y(rates[i] ?? 0)}
                                r={2.5}
                                className="business-trend__margin"
                              />
                            )}
                            <text x={x} y={123} fontSize={10}>
                              {i === 0 || i === daily.length - 1
                                ? day.day.slice(5)
                                : ""}
                            </text>
                          </g>
                        );
                      })}
                      <path d={path} className="business-trend__margin-line" />
                    </>
                  );
                })()}
              </svg>
            )}
            <p className="admin-hint">
              金柱：已知确认收入 · 灰柱：已知成本 ·
              绿线：毛利率（右轴）。证据缺失或收入为零时折线断开；未知金额不当作零。
            </p>
          </section>
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
                    <td>
                      {module.unknown_cost_count +
                        module.pending_count +
                        (module.unknown_revenue_count ?? 0) +
                        (overview.metrics.legacy_cost_count ?? 0) +
                        (overview.metrics.legacy_settlement_count ?? 0) >
                      0
                        ? "待核对"
                        : formatFen(module.revenue_fen - module.cost_fen)}
                    </td>
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
                {(overview?.top_customers.length ?? 0) === 0 && (
                  <tr>
                    <td colSpan={3}>这个区间没有客户消耗记录。</td>
                  </tr>
                )}
                {overview?.top_customers.map((customer) => (
                  <tr key={customer.user_id}>
                    <td>
                      <button
                        type="button"
                        className="table-link-button"
                        onClick={() => {
                          if (onCustomer) onCustomer(customer.user_id);
                          else
                            window.location.hash = `admin/customersMgmt?userId=${encodeURIComponent(customer.user_id)}`;
                        }}
                      >
                        {customer.display_name || customer.username}
                      </button>
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
