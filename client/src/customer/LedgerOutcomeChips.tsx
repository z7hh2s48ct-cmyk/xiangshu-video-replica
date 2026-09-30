import type { WalletLedgerPage } from "../api";
import "./ledger-entries.css";

const CHIPS: ReadonlyArray<{
  value: string;
  label: string;
  count: (counts: WalletLedgerPage["counts"]) => number;
}> = [
  { value: "", label: "全部", count: (counts) => counts.total },
  { value: "pending", label: "处理中", count: (counts) => counts.pending },
  { value: "completed", label: "已完成", count: (counts) => counts.completed },
  { value: "refunded", label: "有退回", count: (counts) => counts.refunded },
  { value: "posted", label: "入账与调整", count: (counts) => counts.posted },
];

/**
 * 结果筛选条：全部 / 处理中 / 已完成 / 有退回 / 入账与调整，每项带条数。
 *
 * 条数是服务端在「当前其他筛选」下按条算的，不随结果筛选变化：用户点之前就知道
 * 「有退回」有几条，不用一个个点过去试。
 */
export function LedgerOutcomeChips({
  counts,
  value,
  onChange,
}: {
  counts: WalletLedgerPage["counts"] | undefined;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <fieldset className="le-chips">
      <legend className="le-visually-hidden">按结果筛选</legend>
      {CHIPS.map((chip) => (
        <button
          aria-pressed={value === chip.value}
          className="le-chip"
          key={chip.value || "all"}
          type="button"
          onClick={() => onChange(chip.value)}
        >
          {chip.label}
          {counts ? <em>{chip.count(counts)}</em> : null}
        </button>
      ))}
    </fieldset>
  );
}
