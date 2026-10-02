import { useEffect, useState } from "react";

import { type FundsSummary, getFundsSummary } from "../api";
import { SummaryReportExport } from "./SummaryReportExport";
import { formatFen } from "./ui/vocabulary";

const methodLabel = (method: string) =>
  ({
    alipay: "支付宝",
    wxpay: "微信",
    offline: "线下转账",
    unknown: "支付方式未知",
  })[method as "alipay"] ?? "支付方式未知";
function monthRange(month: string) {
  const year = Number(month.slice(0, 4)),
    number = Number(month.slice(5, 7));
  return {
    start: `${month}-01`,
    end:
      month === shanghaiToday().slice(0, 7)
        ? shanghaiToday()
        : `${month}-${String(new Date(Date.UTC(year, number, 0)).getUTCDate()).padStart(2, "0")}`,
  };
}
function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

/** 资金中心·资金概览（方案 P1）：财务在一个入口看今日与本月的钱怎么进出。
 *  预收余额与对账异常是时点数，两张卡单独展示；点对账异常跳异常清单。 */
export function FundsOverview({
  onOpenReconciliation,
  readOnly = false,
  selectedMonth,
  onMonthChange,
}: {
  onOpenReconciliation: () => void;
  readOnly?: boolean;
  selectedMonth?: string;
  onMonthChange?: (month: string) => void;
}) {
  const [localMonth, setLocalMonth] = useState(() =>
    shanghaiToday().slice(0, 7),
  );
  const month = selectedMonth ?? localMonth;
  const range = monthRange(month);
  const [todaySummary, setTodaySummary] = useState<FundsSummary>();
  const [monthSummary, setMonthSummary] = useState<FundsSummary>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    const today = shanghaiToday();

    setBusy(true);
    setError("");
    setMonthSummary(undefined);
    setTodaySummary(undefined);
    void Promise.all([
      getFundsSummary(today, today),
      getFundsSummary(range.start, range.end),
    ])
      .then(([todayResult, monthResult]) => {
        if (active) {
          setTodaySummary(todayResult);
          setMonthSummary(monthResult);
        }
      })
      .catch((cause: unknown) => {
        if (active)
          setError(cause instanceof Error ? cause.message : "读取资金概览失败");
      })
      .finally(() => {
        if (active) setBusy(false);
      });
    return () => {
      active = false;
    };
  }, [range.start, range.end]);

  const rows: { label: string; today: string; month: string; hint?: string }[] =
    [];
  if (todaySummary && monthSummary) {
    rows.push(
      {
        label: "充值实收",
        today: formatFen(todaySummary.recharge_fen),
        month: formatFen(monthSummary.recharge_fen),
      },
      {
        label: "线下收款",
        today: formatFen(todaySummary.offline_fen),
        month: formatFen(monthSummary.offline_fen),
      },
      {
        label: "赠送积分",
        today: `${todaySummary.grant_credits} 积分`,
        month: `${monthSummary.grant_credits} 积分`,
        hint: "赠送不产生收入",
      },
      {
        label: "退款扣减",
        today:
          todaySummary.refund_fen === null
            ? `${todaySummary.refund_credits} 积分（金额待核对）`
            : `${formatFen(todaySummary.refund_fen)}`,
        month:
          monthSummary.refund_fen === null
            ? `${monthSummary.refund_credits} 积分（金额待核对）`
            : `${formatFen(monthSummary.refund_fen)}`,
      },
      {
        label: "净收入",
        today:
          todaySummary.net_fen === null
            ? "待核对"
            : formatFen(todaySummary.net_fen),
        month:
          monthSummary.net_fen === null
            ? "待核对"
            : formatFen(monthSummary.net_fen),
        hint: "充值实收 − 退款扣减",
      },
    );
  }

  const channels = monthSummary?.by_method ?? [];

  return (
    <section className="admin-panel funds-overview" aria-label="资金概览">
      <label className="funds-month-filter">
        资金报表月份{" "}
        <input
          type="month"
          aria-label="资金报表月份"
          value={month}
          max={shanghaiToday().slice(0, 7)}
          onChange={(event) => {
            const next = event.target.value;
            if (/^\d{4}-(0[1-9]|1[0-2])$/.test(next)) {
              setLocalMonth(next);
              onMonthChange?.(next);
            }
          }}
        />
      </label>
      <p className="admin-hint">
        所选月份范围 {range.start} 至 {range.end}
        （北京时间）；今日单独对照。时点数据取当前余额与异常。
      </p>
      {error && <p role="alert">{error}</p>}
      {busy && <p role="status">正在计算…</p>}
      {todaySummary && monthSummary && (
        <>
          <div className="admin-table-scroll">
            <table className="admin-data-table" aria-label="资金进出汇总">
              <thead>
                <tr>
                  <th>指标</th>
                  <th>今日</th>
                  <th>所选月份 {month}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.label}>
                    <td>
                      {row.label}
                      {row.hint && (
                        <small className="admin-hint">（{row.hint}）</small>
                      )}
                    </td>
                    <td>{row.today}</td>
                    <td>{row.month}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <h3>时点数据</h3>
          <div className="economics-kpis economics-kpis--four">
            <article>
              <span>预收积分余额</span>
              <strong>{monthSummary.prepaid_credits} 积分</strong>
              <small>
                {monthSummary.prepaid_fen === null
                  ? "未配置积分兑换比例，暂不折算金额"
                  : `折合 ${formatFen(monthSummary.prepaid_fen)}`}
              </small>
            </article>
            <article>
              <button type="button" onClick={onOpenReconciliation}>
                <span>对账异常</span>
                <strong>{monthSummary.reconciliation_problems}</strong>
                <small>点击查看异常清单</small>
              </button>
            </article>
          </div>
          <h3>支付方式分布（{month}）</h3>
          {(monthSummary.unverified_manual_orders ?? 0) > 0 && (
            <p className="admin-hint">
              历史人工订单 {monthSummary.unverified_manual_orders} 笔，记录金额{" "}
              {formatFen(monthSummary.unverified_manual_fen ?? 0)}{" "}
              待核对，未计入实收或线下收款。补偿/赠送积分不产生收款。
            </p>
          )}
          {channels.length === 0 ? (
            <p>所选月份暂无已知支付方式的收款；缺失方式显示为未知。</p>
          ) : (
            <div className="admin-table-scroll">
              <table className="admin-data-table" aria-label="支付方式分布">
                <thead>
                  <tr>
                    <th>支付方式</th>
                    <th>订单数</th>
                    <th>金额</th>
                  </tr>
                </thead>
                <tbody>
                  {channels.map((channel) => (
                    <tr key={channel.method}>
                      <td>{methodLabel(channel.method)}</td>
                      <td>{channel.orders}</td>
                      <td>{formatFen(channel.amount_fen)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
      {!readOnly && (
        <SummaryReportExport
          kind="funds"
          range={range}
          disabled={busy || !monthSummary || Boolean(error)}
        />
      )}
    </section>
  );
}
