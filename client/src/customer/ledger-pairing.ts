import type { WalletTransaction } from "../api";

/** P1-7 配对态：服务端按全量账本裁定，组内每行同值。 */
export type LedgerPairState = "PENDING" | "SETTLED" | "RELEASED";

/** 参与预扣→结算/退回配对的三种流水；入账、转换、补偿不配对。 */
const PAIRABLE_TYPES = new Set(["RESERVE", "SETTLE", "RELEASE"]);

/** 同一事务写入的行时间戳相同，用类型序还原真实资金流。 */
const TYPE_ORDER: Record<string, number> = {
  RESERVE: 0,
  SETTLE: 1,
  RELEASE: 2,
};

export interface LedgerPair {
  operationId: string;
  state: LedgerPairState;
  /** 组内明细，按 RESERVE→SETTLE→RELEASE 排列。 */
  rows: WalletTransaction[];
}

export type LedgerEntry =
  | { kind: "pair"; pair: LedgerPair }
  | { kind: "row"; row: WalletTransaction };

/** 只有带计费周期的预扣/结算/退回行参与配对。 */
function pairOperationId(row: WalletTransaction): string | null {
  if (!row.billing_operation_id) return null;
  return PAIRABLE_TYPES.has(row.type) ? row.billing_operation_id : null;
}

function createdAtMs(row: WalletTransaction): number | null {
  const parsed = Date.parse(row.created_at);
  return Number.isFinite(parsed) ? parsed : null;
}

/** 组内排序：时间升序；同一事务写入的行按 RESERVE→SETTLE→RELEASE。 */
function comparePairRows(a: WalletTransaction, b: WalletTransaction): number {
  const left = createdAtMs(a);
  const right = createdAtMs(b);
  if (left !== null && right !== null && left !== right) return left - right;
  if (left === null || right === null) {
    if (a.created_at !== b.created_at)
      return a.created_at < b.created_at ? -1 : 1;
  }
  return (TYPE_ORDER[a.type] ?? 9) - (TYPE_ORDER[b.type] ?? 9);
}

/** 服务端配对态缺失时的兜底：只能看可见行，跨页时措辞可能略保守。 */
function inferPairState(rows: WalletTransaction[]): LedgerPairState {
  if (rows.some((row) => row.type === "SETTLE")) return "SETTLED";
  if (rows.some((row) => row.type === "RELEASE")) return "RELEASED";
  return "PENDING";
}

/** 用量会跨页、跨筛选被切开，但同一计费周期里的行始终折叠成一个条目。 */
export function groupLedgerRows(items: WalletTransaction[]): LedgerEntry[] {
  const groups = new Map<
    string,
    { state: LedgerPairState | null; rows: WalletTransaction[] }
  >();
  const anchors = new Map<string, number>();

  items.forEach((row, index) => {
    const operationId = pairOperationId(row);
    if (operationId === null) return;
    let group = groups.get(operationId);
    if (!group) {
      group = { state: null, rows: [] };
      groups.set(operationId, group);
    }
    group.rows.push(row);
    group.state ??= row.pair_state ?? null;
    const anchor = anchors.get(operationId);
    const anchorRow = anchor === undefined ? undefined : items[anchor];
    if (anchorRow === undefined || comparePairRows(row, anchorRow) > 0) {
      anchors.set(operationId, index);
    }
  });

  for (const group of groups.values()) {
    group.rows.sort(comparePairRows);
    group.state ??= inferPairState(group.rows);
  }

  const entries: LedgerEntry[] = [];
  items.forEach((row, index) => {
    const operationId = pairOperationId(row);
    if (operationId === null) {
      entries.push({ kind: "row", row });
      return;
    }
    // 组条目跟在组内最新的一行位置，时间倒序的列表观感保持不变。
    if (anchors.get(operationId) !== index) return;
    const group = groups.get(operationId);
    if (!group) return;
    entries.push({
      kind: "pair",
      pair: {
        operationId,
        state: group.state ?? inferPairState(group.rows),
        rows: group.rows,
      },
    });
  });
  return entries;
}

/** 一句话讲清这一组钱怎么走的：预扣→实扣→退回。 */
export function pairingSummary(pair: LedgerPair): string {
  const reserve = pair.rows.find((row) => row.type === "RESERVE");
  const settle = pair.rows.find((row) => row.type === "SETTLE");
  const release = pair.rows.find((row) => row.type === "RELEASE");

  const parts: string[] = [];
  if (reserve) parts.push(`预扣 ${reserve.reserved_delta}`);
  if (settle) parts.push(`实扣 ${-settle.reserved_delta}`);
  if (release) {
    // 配对态由全量账本裁定：跨页时「全额退回」不能只看可见行推断。
    parts.push(
      pair.state === "RELEASED"
        ? `全额退回 ${release.available_delta}`
        : `退回 ${release.available_delta}`,
    );
  }
  if (pair.state === "PENDING" && parts.length === 1) {
    return `${parts[0]}（结算中）`;
  }
  return parts.join(" → ");
}

/** 折叠后的净可用积分变化，与展开明细逐行相加一致。 */
export function netAvailableDelta(rows: WalletTransaction[]): number {
  return rows.reduce((total, row) => total + row.available_delta, 0);
}

/** 折叠后的净待结算变化；周期完成后应为 0。 */
export function netReservedDelta(rows: WalletTransaction[]): number {
  return rows.reduce((total, row) => total + row.reserved_delta, 0);
}
