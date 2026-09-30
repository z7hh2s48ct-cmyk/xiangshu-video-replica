import { Fragment, type ReactNode, useState } from "react";
import type {
  CustomerSessionCredential,
  WalletLedgerEntry,
  WalletTransaction,
} from "../api";
import {
  businessDescription,
  formatLedgerClock,
  formatLedgerTime,
  ledgerSourceLabel,
  signedCredits,
} from "./ledgerDisplay";
import {
  HOLD_EXPLANATION,
  LEDGER_TERMS,
  LEDGER_TYPE_LABEL,
} from "./ledgerVocabulary";
import { TransactionPricingBreakdown } from "./TransactionPricingBreakdown";
import "./ledger-entries.css";

/** 一条任务在结果列里的说法。整笔退回不叫「失败」：取消任务同样会整笔退回。 */
const OUTCOME_BADGE: Record<
  WalletLedgerEntry["outcome"],
  { tone: string; text: string }
> = {
  PENDING: { tone: "pending", text: "处理中" },
  COMPLETED: { tone: "done", text: "已完成" },
  PARTIAL: { tone: "refunded", text: "部分成功" },
  FAILED: { tone: "refunded", text: "未成功 · 已全额退回" },
  POSTED: { tone: "posted", text: "入账 / 调整" },
};

/** 不属于计费周期的单笔流水，结果列直接说它是什么。 */
function postedBadge(row: WalletTransaction): { tone: string; text: string } {
  if (row.type === "CHARGE" || row.type === "CONVERSION") {
    return { tone: "posted", text: "入账" };
  }
  if (row.type === "REFUND") return { tone: "posted", text: "调账" };
  // 没有 operation 的历史暂扣 / 实扣 / 退回：只能按单笔说。
  return { tone: "done", text: LEDGER_TYPE_LABEL[row.type] };
}

/** 单笔流水对「可用积分」的实际影响：历史实扣行的金额记在暂扣列上，可用列恒为 0。 */
function rowImpact(row: WalletTransaction): number {
  return row.type === "SETTLE" ? row.reserved_delta : row.available_delta;
}

function Step({
  tone,
  children,
}: {
  tone: "hold" | "charge" | "refund" | "info";
  children: ReactNode;
}) {
  return <span className={`le-step le-step--${tone}`}>{children}</span>;
}

function Flow({ entry }: { entry: WalletLedgerEntry }) {
  const first = entry.rows[0];
  if (entry.kind === "row") {
    const impact = rowImpact(first);
    if (first.type === "RESERVE")
      return <Step tone="hold">{`${LEDGER_TERMS.hold} ${-impact}`}</Step>;
    if (first.type === "SETTLE")
      return <Step tone="charge">{`${LEDGER_TERMS.charge} ${-impact}`}</Step>;
    if (first.type === "RELEASE")
      return <Step tone="refund">{`${LEDGER_TERMS.refund} +${impact}`}</Step>;
    return <Step tone="info">{ledgerSourceLabel(first)}</Step>;
  }
  if (entry.outcome === "PENDING") {
    return (
      <div className="le-flow">
        <Step tone="hold">{`${LEDGER_TERMS.hold} ${entry.reserved_credits}`}</Step>
        <span className="le-arrow">→</span>
        <span className="le-wait">任务结束后多退少补</span>
      </div>
    );
  }
  return (
    <div className="le-flow">
      <Step tone="hold">{`${LEDGER_TERMS.hold} ${entry.reserved_credits}`}</Step>
      {entry.charged_credits > 0 && (
        <>
          <span className="le-arrow">→</span>
          <Step tone="charge">{`${LEDGER_TERMS.charge} ${entry.charged_credits}`}</Step>
        </>
      )}
      {entry.refunded_credits > 0 && (
        <>
          <span className="le-arrow">→</span>
          <Step tone="refund">{`${LEDGER_TERMS.refund} +${entry.refunded_credits}`}</Step>
        </>
      )}
    </div>
  );
}

/** 「本次实际花费」：用户真正想知道的那个数，退回的部分单独用绿字交代。 */
function Amount({ entry }: { entry: WalletLedgerEntry }) {
  if (entry.kind === "row") {
    const impact = rowImpact(entry.rows[0]);
    return (
      <span
        className={`le-amount${impact > 0 ? " le-amount--in" : ""}`}
      >{`${signedCredits(impact)}`}</span>
    );
  }
  switch (entry.outcome) {
    case "PENDING":
      return (
        <>
          <span className="le-amount le-amount--pending">
            {`${LEDGER_TERMS.hold} ${entry.reserved_credits} 积分`}
          </span>
          <small className="le-sub">结算后多退少补</small>
        </>
      );
    case "FAILED":
      return (
        <>
          <span className="le-amount le-amount--zero">0 积分</span>
          <small className="le-sub le-sub--good">
            {`已退回 ${entry.refunded_credits}，余额已恢复`}
          </small>
        </>
      );
    case "PARTIAL":
      return (
        <>
          <span className="le-amount">{`-${entry.charged_credits} 积分`}</span>
          <small className="le-sub le-sub--good">
            {`已退回 ${entry.refunded_credits}`}
          </small>
        </>
      );
    default:
      return (
        <span className="le-amount">{`-${entry.charged_credits} 积分`}</span>
      );
  }
}

function EntryRows({
  entry,
  expanded,
  onToggle,
  credential,
  onOpenTask,
}: {
  entry: WalletLedgerEntry;
  expanded: boolean;
  onToggle: () => void;
  credential?: () => Promise<CustomerSessionCredential | null>;
  onOpenTask?: (row: WalletTransaction) => void;
}) {
  const first = entry.rows[0];
  const isCycle = entry.kind === "cycle";
  const badge = isCycle ? OUTCOME_BADGE[entry.outcome] : postedBadge(first);
  const taskRow = entry.rows.find(
    (row) => row.generation_batch_id || row.oral_task_id,
  );
  const title = isCycle
    ? businessDescription(first)
    : first.type === "RESERVE" ||
        first.type === "SETTLE" ||
        first.type === "RELEASE"
      ? businessDescription(first)
      : LEDGER_TYPE_LABEL[first.type];
  const sourceParts = [
    isCycle ||
    first.type === "RESERVE" ||
    first.type === "SETTLE" ||
    first.type === "RELEASE"
      ? ledgerSourceLabel(first)
      : null,
    first.credit_price_version != null
      ? `价格 V${first.credit_price_version}`
      : null,
  ].filter(Boolean);
  const detailId = `le-detail-${entry.key}`;
  return (
    <Fragment>
      <tr
        className={`le-row${entry.refunded_credits > 0 ? " le-row--refund" : ""}`}
        data-outcome={entry.outcome}
      >
        <td data-label="时间">
          {formatLedgerTime(entry.updated_at)}
          {isCycle && entry.started_at !== entry.updated_at ? (
            <small className="le-sub">
              提交于 {formatLedgerClock(entry.started_at)}
            </small>
          ) : null}
        </td>
        <td data-label="业务">
          <b>{title}</b>
          {sourceParts.length ? (
            <small className="le-sub">{sourceParts.join(" · ")}</small>
          ) : null}
          {onOpenTask && taskRow ? (
            <button
              type="button"
              className="le-link"
              onClick={() => onOpenTask(taskRow)}
            >
              查看任务
            </button>
          ) : null}
          {!isCycle ? (
            <TransactionPricingBreakdown
              credential={credential}
              transaction={first}
            />
          ) : null}
        </td>
        <td data-label="结果">
          <span className={`le-badge le-badge--${badge.tone}`}>
            {badge.text}
          </span>
        </td>
        <td data-label="资金去向">
          <Flow entry={entry} />
          {isCycle ? (
            <button
              type="button"
              className="le-link"
              aria-expanded={expanded}
              aria-controls={detailId}
              onClick={onToggle}
            >
              {expanded ? "收起明细" : `查看 ${entry.rows.length} 笔明细`}
            </button>
          ) : null}
        </td>
        <td className="le-num" data-label="本次实际花费">
          <Amount entry={entry} />
        </td>
      </tr>
      {isCycle && expanded ? (
        <tr className="le-detail" id={detailId}>
          <td colSpan={5}>
            <table className="le-mini" aria-label="逐笔明细">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>类型</th>
                  <th>可用积分变化</th>
                  <th>暂扣中变化</th>
                  <th>计费依据</th>
                </tr>
              </thead>
              <tbody>
                {entry.rows.map((row) => (
                  <tr key={row.id}>
                    <td>{formatLedgerTime(row.created_at)}</td>
                    <td>{LEDGER_TYPE_LABEL[row.type]}</td>
                    <td>{signedCredits(row.available_delta)}</td>
                    <td>{signedCredits(row.reserved_delta)}</td>
                    <td>
                      <TransactionPricingBreakdown
                        credential={credential}
                        transaction={row}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </td>
        </tr>
      ) : null}
    </Fragment>
  );
}

/**
 * 消费记录表：一条任务一行。
 *
 * 用户看账要回答的是「这条任务最后花了多少、有没有退回」，所以把暂扣 / 实扣 / 退回
 * 合成一行，结果、资金去向、本次实际花费放在一起；逐笔明细收在「查看 N 笔明细」里
 * 供核对。合并由服务端在分页之前完成，这里拿到的每一条都是完整的一笔。
 */
export function LedgerEntryTable({
  entries,
  credential,
  onOpenTask,
  empty,
}: {
  entries: readonly WalletLedgerEntry[];
  /** 取会话凭据，「查当时价目」按需调用；不传则不提供这个查询。 */
  credential?: () => Promise<CustomerSessionCredential | null>;
  /** 打开任务详情；不传则不显示「查看任务」。 */
  onOpenTask?: (row: WalletTransaction) => void;
  empty: ReactNode;
}) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const toggle = (key: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  return (
    <div className="le">
      <div className="le-scroll">
        <table className="le-table" aria-label="消费记录列表">
          <thead>
            <tr>
              <th scope="col">时间（北京时间）</th>
              <th scope="col">业务</th>
              <th scope="col">结果</th>
              <th scope="col">资金去向</th>
              <th scope="col" className="le-num">
                本次实际花费
              </th>
            </tr>
          </thead>
          <tbody>
            {entries.length ? (
              entries.map((entry) => (
                <EntryRows
                  key={`${entry.kind}:${entry.key}`}
                  entry={entry}
                  expanded={expanded.has(entry.key)}
                  onToggle={() => toggle(entry.key)}
                  credential={credential}
                  onOpenTask={onOpenTask}
                />
              ))
            ) : (
              <tr>
                <td colSpan={5}>{empty}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <p className="le-legend">
        <Step tone="hold">{LEDGER_TERMS.hold}</Step>
        <Step tone="charge">{LEDGER_TERMS.charge}</Step>
        <Step tone="refund">{LEDGER_TERMS.refund}</Step>
        {HOLD_EXPLANATION}
      </p>
    </div>
  );
}
