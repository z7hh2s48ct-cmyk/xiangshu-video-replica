import { type LedgerPair, pairingSummary } from "./ledger-pairing";
import "./ledger-pairing.css";

/** P1-7：同一计费周期的预扣→结算/退回折叠成一行摘要，可展开逐笔核对。 */
export function LedgerPairingSummary({
  pair,
  expanded,
  onToggle,
}: {
  pair: LedgerPair;
  expanded: boolean;
  onToggle: () => void;
}) {
  const summary = pairingSummary(pair);
  const toggleLabel = expanded ? "收起明细" : `查看 ${pair.rows.length} 笔明细`;
  return (
    <div className="ledger-pair-summary">
      <span className="ledger-pair-flow">{summary}</span>
      <button
        type="button"
        className="ledger-pair-toggle"
        aria-expanded={expanded}
        aria-label={`${summary}，${toggleLabel}`}
        onClick={onToggle}
      >
        {toggleLabel}
      </button>
    </div>
  );
}
