import { describe, expect, it } from "vitest";

import type { WalletTransaction } from "../api";
import {
  groupLedgerRows,
  type LedgerPair,
  netAvailableDelta,
  netReservedDelta,
  pairingSummary,
} from "./ledger-pairing";

function ledgerRow(
  overrides: Partial<WalletTransaction> = {},
): WalletTransaction {
  return {
    id: "tx-1",
    user_id: "user-1",
    type: "SETTLE",
    available_delta: 0,
    reserved_delta: -3,
    recharge_order_id: null,
    task_id: "task-1",
    billing_round: 1,
    created_at: "2026-09-22 10:00:00",
    ...overrides,
  };
}

/** 一个结算成功的计费周期：预扣 5 → 实扣 3 → 退回 2，三行共享 operation。 */
function settledCycle(): WalletTransaction[] {
  return [
    ledgerRow({
      id: "release",
      type: "RELEASE",
      available_delta: 2,
      reserved_delta: -2,
      billing_operation_id: "op-1",
      pair_state: "SETTLED",
      created_at: "2026-09-22 10:00:01",
    }),
    ledgerRow({
      id: "settle",
      type: "SETTLE",
      available_delta: 0,
      reserved_delta: -3,
      billing_operation_id: "op-1",
      pair_state: "SETTLED",
      created_at: "2026-09-22 10:00:01",
    }),
    ledgerRow({
      id: "reserve",
      type: "RESERVE",
      available_delta: -5,
      reserved_delta: 5,
      billing_operation_id: "op-1",
      pair_state: "SETTLED",
      created_at: "2026-09-22 10:00:00",
    }),
  ];
}

describe("groupLedgerRows", () => {
  it("folds the three rows of one billing cycle into a single entry", () => {
    const entries = groupLedgerRows(settledCycle());

    expect(entries).toHaveLength(1);
    const entry = entries[0];
    if (entry.kind !== "pair") throw new Error("expected a pair entry");
    expect(entry.pair.operationId).toBe("op-1");
    expect(entry.pair.state).toBe("SETTLED");
    // 组内还原真实资金流顺序，界面上读起来才是预扣→结算→退回。
    expect(entry.pair.rows.map((row) => row.id)).toEqual([
      "reserve",
      "settle",
      "release",
    ]);
  });

  it("keeps unrelated rows in place and folds a group whose rows are not adjacent", () => {
    const rows = [
      ledgerRow({
        id: "release",
        type: "RELEASE",
        available_delta: 5,
        reserved_delta: -5,
        billing_operation_id: "op-1",
        pair_state: "RELEASED",
        created_at: "2026-09-22 10:00:01",
      }),
      ledgerRow({
        id: "charge",
        type: "CHARGE",
        available_delta: 100,
        created_at: "2026-09-22 09:00:00",
      }),
      ledgerRow({
        id: "other",
        type: "SETTLE",
        task_id: "task-2",
        created_at: "2026-09-22 09:30:00",
      }),
      // 分页窗口或交错时同组行可以不相邻，仍要折叠成一个条目。
      ledgerRow({
        id: "reserve",
        type: "RESERVE",
        available_delta: -5,
        reserved_delta: 5,
        billing_operation_id: "op-1",
        pair_state: "RELEASED",
        created_at: "2026-09-22 10:00:00",
      }),
    ];

    const entries = groupLedgerRows(rows);

    expect(entries.map((entry) => entry.kind)).toEqual(["pair", "row", "row"]);
    const pair = entries[0];
    if (pair.kind !== "pair") throw new Error("expected a pair entry");
    expect(pair.pair.rows.map((row) => row.id)).toEqual(["reserve", "release"]);
    expect(entries[1].kind === "row" && entries[1].row.id).toBe("charge");
    expect(entries[2].kind === "row" && entries[2].row.id).toBe("other");
  });

  it("leaves rows without a billing operation as plain entries", () => {
    const entries = groupLedgerRows([
      ledgerRow({ id: "charge", type: "CHARGE", available_delta: 100 }),
      ledgerRow({ id: "legacy", type: "SETTLE", billing_operation_id: null }),
    ]);

    expect(entries.map((entry) => entry.kind)).toEqual(["row", "row"]);
  });

  it("orders the anchor group after the newest visible row of the cycle", () => {
    // 倒序列表里退回行在上：组的位置应跟随它，而不是发起时的预扣行。
    const rows = [
      ledgerRow({
        id: "release",
        type: "RELEASE",
        available_delta: 5,
        reserved_delta: -5,
        billing_operation_id: "op-1",
        pair_state: "RELEASED",
        created_at: "2026-09-22 10:00:01",
      }),
      ledgerRow({ id: "charge", type: "CHARGE", available_delta: 100 }),
      ledgerRow({
        id: "reserve",
        type: "RESERVE",
        available_delta: -5,
        reserved_delta: 5,
        billing_operation_id: "op-1",
        pair_state: "RELEASED",
        created_at: "2026-09-22 10:00:00",
      }),
    ];

    const entries = groupLedgerRows(rows);

    expect(entries.map((entry) => entry.kind)).toEqual(["pair", "row"]);
  });
});

describe("pairingSummary", () => {
  function pair(
    rows: WalletTransaction[],
    state: LedgerPair["state"],
  ): LedgerPair {
    return { operationId: "op-1", state, rows };
  }

  it("walks a partial refund: 预扣 5 → 实扣 3 → 退回 2", () => {
    expect(pairingSummary(pair(settledCycle(), "SETTLED"))).toBe(
      "预扣 5 → 实扣 3 → 退回 2",
    );
  });

  it("says 全额退回 only when the cycle really settled nothing", () => {
    const rows = [
      ledgerRow({
        id: "reserve",
        type: "RESERVE",
        available_delta: -5,
        reserved_delta: 5,
      }),
      ledgerRow({
        id: "release",
        type: "RELEASE",
        available_delta: 5,
        reserved_delta: -5,
      }),
    ];

    expect(pairingSummary(pair(rows, "RELEASED"))).toBe("预扣 5 → 全额退回 5");
  });

  it("marks a fully charged cycle and an open reservation", () => {
    expect(
      pairingSummary(
        pair(
          [
            ledgerRow({
              id: "reserve",
              type: "RESERVE",
              available_delta: -3,
              reserved_delta: 3,
            }),
            ledgerRow({ id: "settle", type: "SETTLE", reserved_delta: -3 }),
          ],
          "SETTLED",
        ),
      ),
    ).toBe("预扣 3 → 实扣 3");

    expect(
      pairingSummary(
        pair(
          [
            ledgerRow({
              id: "reserve",
              type: "RESERVE",
              available_delta: -3,
              reserved_delta: 3,
            }),
          ],
          "PENDING",
        ),
      ),
    ).toBe("预扣 3（结算中）");
  });

  it("still reads correctly when the paging window cut the cycle in half", () => {
    // 预扣行在上一页：只剩结算与退回，也要能从配对态读出正确措辞。
    expect(
      pairingSummary(
        pair(
          [
            ledgerRow({ id: "settle", type: "SETTLE", reserved_delta: -3 }),
            ledgerRow({
              id: "release",
              type: "RELEASE",
              available_delta: 2,
              reserved_delta: -2,
            }),
          ],
          "SETTLED",
        ),
      ),
    ).toBe("实扣 3 → 退回 2");

    // 全退周期的退回行单独可见时说「全额退回」，与分组态一致。
    expect(
      pairingSummary(
        pair(
          [
            ledgerRow({
              id: "release",
              type: "RELEASE",
              available_delta: 5,
              reserved_delta: -5,
            }),
          ],
          "RELEASED",
        ),
      ),
    ).toBe("全额退回 5");
  });
});

describe("net deltas", () => {
  it("sums the cycle so the folded row shows one net movement", () => {
    const rows = settledCycle();

    expect(netAvailableDelta(rows)).toBe(-3);
    expect(netReservedDelta(rows)).toBe(0);
  });
});
