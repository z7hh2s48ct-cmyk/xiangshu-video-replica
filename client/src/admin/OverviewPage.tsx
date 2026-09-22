import { useCallback, useEffect, useState } from "react";

import { type DashboardSummary, getDashboardSummary } from "../api.admin";
import clapperboardIcon from "../assets/icons/clapperboard.svg";
import gaugeIcon from "../assets/icons/gauge.svg";
import shieldIcon from "../assets/icons/shield-check.svg";
import usersIcon from "../assets/icons/users-round.svg";
import walletIcon from "../assets/icons/wallet.svg";
import { PageBanner } from "./ui/PageBanner";
import "./economics.css";

function fenToYuan(fen: number): string {
  return (fen / 100).toFixed(2);
}

function KpiCard({
  label,
  value,
  sub,
  tone,
  icon,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "warn" | "danger";
  icon: string;
}) {
  return (
    <section
      className={`admin-summary-card${tone === "warn" ? " is-warning" : ""}${tone === "danger" ? " is-danger" : ""}`}
      aria-label={label}
    >
      <img src={icon} alt={`${label}图标`} />
      <div>
        <span>{label}</span>
        <strong>{value}</strong>
        {sub ? <small>{sub}</small> : null}
      </div>
    </section>
  );
}

function TodoRow({
  label,
  count,
  tone,
  hint,
  onOpen,
}: {
  label: string;
  count: number;
  tone: "warn" | "danger" | "info";
  hint?: string;
  onOpen?: () => void;
}) {
  return (
    <li>
      <span>{label}</span>
      <span className={`status-badge status-badge--${tone}`}>{count}</span>
      {hint ? <small>{hint}</small> : null}
      {onOpen ? (
        <button type="button" onClick={onOpen}>
          去处理
        </button>
      ) : null}
    </li>
  );
}

/**
 * W15 — 总览仪表盘：KPI、近 7 日生成趋势、待办事项与快捷操作。
 * 数据来自 GET /api/control/dashboard/summary（AdminReader）。
 */
export function OverviewPage({
  readOnly = false,
  onNavigate,
}: {
  readOnly?: boolean;
  onNavigate?: (tab: string) => void;
}) {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setError("");
      setSummary(await getDashboardSummary());
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "读取仪表盘失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return <PageBanner tone="error">{error}</PageBanner>;
  }
  if (!summary) {
    return (
      <section className="admin-panel" aria-label="总览仪表盘">
        <p>正在加载仪表盘…</p>
      </section>
    );
  }

  const { today, trend, todos } = summary;
  const maxTrend = Math.max(
    1,
    ...trend.map((point) => point.succeeded + point.failed),
  );
  const maxCost = Math.max(1, ...trend.map((point) => point.cost_fen ?? 0));
  const successPoints = trend.map((point, index) => ({
    x: ((index + 0.5) / trend.length) * 100,
    y: 94 - (point.succeeded / maxTrend) * 82,
  }));
  const successPath = successPoints
    .map((point, index) => {
      if (index === 0) return `M ${point.x} ${point.y}`;
      const previous = successPoints[index - 1];
      const middleX = (previous.x + point.x) / 2;
      // 水平切线让相邻段平滑连接，控制点不越过数据区间，避免虚构峰谷。
      return `C ${middleX} ${previous.y}, ${middleX} ${point.y}, ${point.x} ${point.y}`;
    })
    .join(" ");
  // 拆解失败中最集中的那条上游原因，直接摆在待办行上供定位。
  const topAnalysisReason =
    todos.analysis_failure_reasons?.[0]?.reason ??
    todos.analysis_failure_reasons?.[0]?.error_code ??
    undefined;
  const todoItems: Array<{
    key: string;
    label: string;
    count: number;
    tone: "warn" | "danger" | "info";
    tab: string;
    hint?: string;
  }> = [
    {
      key: "failed",
      label: "失败任务待处理",
      count: todos.failed_tasks_7d,
      tone: "danger",
      tab: "failedGenerationRecords",
    },
    {
      // 拆解是付费上游调用：2026-09-20 那种「拆解全挂、总览全绿」不能再出现，
      // 单列一行并把最集中的上游原因直接摆出来。
      key: "analysis",
      label: "拆解失败待排查",
      count: todos.analysis_failures_7d ?? 0,
      tone: "danger",
      tab: "analysisFailures",
      hint: topAnalysisReason,
    },
    {
      key: "recon",
      label: "对账不一致",
      count: todos.reconciliation_problems,
      tone: "warn",
      tab: "funds",
    },
    {
      key: "rates",
      label: "成本待配置",
      count: todos.unconfigured_rates ?? 0,
      tone: "danger",
      tab: "rates",
    },
  ];

  return (
    <div className="dashboard-page">
      {(today.legacy_cost_records ?? 0) + (today.legacy_settlements ?? 0) >
        0 && (
        <p role="status">
          今日有 {today.legacy_cost_records ?? 0} 条历史成本、
          {today.legacy_settlements ?? 0} 条历史结算待核对。
        </p>
      )}
      <div className="dashboard-kpis">
        <KpiCard
          icon={clapperboardIcon}
          label="今日生成"
          value={String(today.generation_count)}
          sub={`输出 ${(today.output_seconds ?? 0).toLocaleString()} 秒`}
        />
        <KpiCard
          icon={gaugeIcon}
          label="生成成功率"
          value={
            today.success_rate_pct == null ? "—" : `${today.success_rate_pct}%`
          }
          sub={`成功 ${today.succeeded}`}
        />
        <KpiCard
          icon={walletIcon}
          label="今日成本"
          value={yuanOrUnknown(today.cost_fen, todos.unknown_cost_records)}
          sub={today.cost_fen == null ? "结算或成本待核对" : "已确认成本"}
          tone={today.cost_fen == null ? "warn" : undefined}
        />
        <KpiCard
          icon={gaugeIcon}
          label="今日毛利"
          value={
            today.gross_fen == null
              ? "待核对"
              : `¥${fenToYuan(today.gross_fen)}`
          }
          sub={
            today.margin_pct == null
              ? "利润率未知"
              : `利润率 ${today.margin_pct}%`
          }
        />
        <KpiCard
          icon={usersIcon}
          label="有效客户"
          value={String(today.active_customers)}
          sub="已启用的客户账户"
        />
        <KpiCard
          icon={walletIcon}
          label="今日充值"
          value={`¥${fenToYuan(today.recharge_fen)}`}
          sub={`${today.recharge_orders ?? 0} 单`}
        />
        <KpiCard
          icon={shieldIcon}
          label="对账异常"
          value={String(todos.reconciliation_problems)}
          sub="资金账务核对"
          tone={todos.reconciliation_problems ? "warn" : undefined}
        />
      </div>

      <div className="dashboard-main-grid">
        <section className="admin-panel" aria-label="近 7 日生成与成本">
          <div className="dashboard-chart-heading">
            <h2>近 7 日生成与成本</h2>
            <div className="dashboard-chart-legend">
              <span className="is-cost">成本（元）</span>
              <span className="is-success">成功生成数（条）</span>
            </div>
          </div>
          {trend.length === 0 ? (
            <p>暂无数据。</p>
          ) : (
            <div
              className="dashboard-combo"
              role="img"
              aria-label="成本柱状图与成功数趋势曲线"
            >
              <div
                className="dashboard-axis dashboard-axis--left"
                aria-hidden="true"
              >
                <span>¥{Math.round(maxCost / 100).toLocaleString()}</span>
                <span>¥0</span>
              </div>
              <div
                className="dashboard-axis dashboard-axis--right"
                aria-hidden="true"
              >
                <span>{maxTrend}</span>
                <span>0</span>
              </div>
              <svg
                viewBox="0 0 100 100"
                preserveAspectRatio="none"
                aria-hidden="true"
              >
                <path
                  d={successPath}
                  fill="none"
                  stroke="#75a47c"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  vectorEffect="non-scaling-stroke"
                />
              </svg>
              {trend.map((point) => (
                <div
                  className="dashboard-trend__col"
                  key={point.day}
                  title={`${point.day}：成功 ${point.succeeded} / 失败 ${point.failed}`}
                >
                  <div className="dashboard-trend__bars">
                    {point.cost_fen == null ? (
                      <span title="成本待核对">待核对</span>
                    ) : (
                      <div
                        className="dashboard-trend__bar dashboard-trend__bar--cost"
                        title={`成本 ¥${fenToYuan(point.cost_fen ?? 0)}`}
                        style={{
                          height: `${Math.round(((point.cost_fen ?? 0) / maxCost) * 100)}%`,
                        }}
                      >
                        <b>{fenToYuan(point.cost_fen ?? 0)}</b>
                      </div>
                    )}
                  </div>
                  <span>
                    {point.day.slice(5)}
                    <small>{point.succeeded} 条</small>
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>

        <section className="admin-panel" aria-label="待办事项">
          <h2>待办事项</h2>
          <ul className="dashboard-todos">
            {todoItems.map((item) => (
              <TodoRow
                key={item.key}
                count={item.count}
                hint={item.hint}
                label={item.label}
                tone={item.tone}
                onOpen={
                  !readOnly && item.count > 0
                    ? () => onNavigate?.(item.tab)
                    : undefined
                }
              />
            ))}
          </ul>
        </section>
      </div>

      <div className="dashboard-bottom-grid">
        {!readOnly ? (
          <section className="admin-panel" aria-label="快捷操作">
            <h2>快捷操作</h2>
            <div className="dashboard-quick-actions">
              <button
                type="button"
                onClick={() => onNavigate?.("customerAdjustments")}
              >
                后台加款
              </button>
              <button
                type="button"
                onClick={() => onNavigate?.("customerAdjustments")}
              >
                发放赠送积分
              </button>
              <button type="button" onClick={() => onNavigate?.("costDetails")}>
                成本核对
              </button>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}

function yuanOrUnknown(
  costFen: number | null | undefined,
  unknownCount: number | undefined,
): string {
  if (costFen == null || (unknownCount ?? 0) > 0) return "待核对";
  return `¥${fenToYuan(costFen)}`;
}
