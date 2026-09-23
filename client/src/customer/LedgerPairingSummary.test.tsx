import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { WalletTransaction } from "../api";
import { LedgerPairingSummary } from "./LedgerPairingSummary";
import type { LedgerPair } from "./ledger-pairing";

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

function settledPair(): LedgerPair {
  return {
    operationId: "op-1",
    state: "SETTLED",
    rows: [
      ledgerRow({
        id: "reserve",
        type: "RESERVE",
        available_delta: -5,
        reserved_delta: 5,
      }),
      ledgerRow({ id: "settle" }),
      ledgerRow({
        id: "release",
        type: "RELEASE",
        available_delta: 2,
        reserved_delta: -2,
      }),
    ],
  };
}

describe("LedgerPairingSummary", () => {
  it("names the money flow and asks to expand the three rows", () => {
    const onToggle = vi.fn();
    render(
      <LedgerPairingSummary
        pair={settledPair()}
        expanded={false}
        onToggle={onToggle}
      />,
    );

    expect(screen.getByText("预扣 5 → 实扣 3 → 退回 2")).toBeVisible();
    const toggle = screen.getByRole("button", { name: /查看 3 笔明细/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(onToggle).toHaveBeenCalledOnce();
  });

  it("reads back the expanded state", () => {
    render(
      <LedgerPairingSummary pair={settledPair()} expanded onToggle={vi.fn()} />,
    );

    const toggle = screen.getByRole("button", { name: /收起明细/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("预扣 5 → 实扣 3 → 退回 2")).toBeVisible();
  });
});
