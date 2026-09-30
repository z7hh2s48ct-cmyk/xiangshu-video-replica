import { useEffect, useState } from "react";

import { type FundsSummary, getFundsSummary } from "../api";
import { formatFen } from "./ui/vocabulary";

/** 渠道值 → 中性支付方式名（与收款订单页同一套业务词）。 */
function channelLabel(provider: string): string {
  const labels: Record<string, string> = {
    zpay: "支付宝",
    wechat_native: "微信",
    activation_code: "激活码",
    admin_adjustment: "线下转账",
  };
  return labels[provider] ?? provider;
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
}: {
  onOpenReconciliation: () => void;
}) {
  const [todaySummary, setTodaySummary] = useState<FundsSummary>();
  const [monthSummary, setMonthSummary] = useState<FundsSummary>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    const today = shanghaiToday();
    const monthStart = `${today.slice(0, 7)}-01`;
    setBusy(true);
    setError("");
    void Promise.all([
      getFundsSummary(today, today),
      getFundsSummary(monthStart, today),
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
  }, []);

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
            ? `${todaySummary.refund_credits} 积分`
            : `${formatFen(todaySummary.refund_fen)}`,
        month:
          monthSummary.refund_fen === null
            ? `${monthSummary.refund_credits} 积分`
            : `${formatFen(monthSummary.refund_fen)}`,
      },
      {
        label: "净收入",
        today:
          todaySummary.net_fen === null ? "—" : formatFen(todaySummary.net_fen),
        month:
          monthSummary.net_fen === null ? "—" : formatFen(monthSummary.net_fen),
        hint: "充值实收 − 退款扣减",
      },
    );
  }

  const channels = monthSummary?.by_channel ?? [];

  return (
    <section className="admin-panel" aria-label="资金概览">
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
                  <th>本月至今</th>
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
          <h3>支付方式分布（本月）</h3>
          {channels.length === 0 ? (
            <p>本月暂无收款。</p>
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
                    <tr key={channel.provider}>
                      <td>{channelLabel(channel.provider)}</td>
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
    </section>
  );
}
