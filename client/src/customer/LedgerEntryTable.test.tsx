import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { WalletLedgerEntry, WalletTransaction } from "../api";
import { LedgerEntryTable } from "./LedgerEntryTable";

function row(overrides: Partial<WalletTransaction>): WalletTransaction {
  return {
    id: "row",
    user_id: "u-1",
    type: "RESERVE",
    available_delta: 0,
    reserved_delta: 0,
    recharge_order_id: null,
    task_id: null,
    billing_round: 1,
    created_at: "2026-09-30T02:00:00Z",
    auth_source: "session",
    ...overrides,
  };
}

function cycle(
  overrides: Partial<WalletLedgerEntry> & Pick<WalletLedgerEntry, "outcome">,
): WalletLedgerEntry {
  return {
    key: "op-1",
    kind: "cycle",
    reserved_credits: 8,
    charged_credits: 0,
    refunded_credits: 0,
    net_available_delta: 0,
    started_at: "2026-09-30T02:00:00Z",
    updated_at: "2026-09-30T02:05:00Z",
    rows: [
      row({
        id: "reserve",
        type: "RESERVE",
        available_delta: -8,
        reserved_delta: 8,
        billing_operation_id: "op-1",
        service_name: "视频生成 768p",
        credit_price_version: 3,
      }),
    ],
    ...overrides,
  };
}

const FAILED = cycle({
  key: "op-failed",
  outcome: "FAILED",
  refunded_credits: 8,
  net_available_delta: 0,
  rows: [
    row({
      id: "f-reserve",
      type: "RESERVE",
      available_delta: -8,
      reserved_delta: 8,
      billing_operation_id: "op-failed",
      service_name: "视频生成 768p",
    }),
    row({
      id: "f-release",
      type: "RELEASE",
      available_delta: 8,
      reserved_delta: -8,
      billing_operation_id: "op-failed",
      service_name: "视频生成 768p",
    }),
  ],
});

test("整笔退回：结果、去向、花费都在一行里说清，不出现裸的 -8", () => {
  render(<LedgerEntryTable empty="空" entries={[FAILED]} />);
  const table = screen.getByRole("table", { name: "消费记录列表" });
  expect(within(table).getByText("未成功 · 已全额退回")).toBeVisible();
  expect(within(table).getByText("暂扣 8")).toBeVisible();
  expect(within(table).getByText("退回 +8")).toBeVisible();
  expect(within(table).getByText("0 积分")).toBeVisible();
  expect(within(table).getByText("已退回 8，余额已恢复")).toBeVisible();
  expect(within(table).queryByText("-8 积分")).toBeNull();
  // 有退回的行带标记，供样式在左侧画绿色竖条。
  expect(table.querySelector("tr.le-row--refund")).not.toBeNull();
});

test("处理中的任务只说暂扣，并交代结束后多退少补", () => {
  render(
    <LedgerEntryTable
      empty="空"
      entries={[cycle({ outcome: "PENDING", net_available_delta: -8 })]}
    />,
  );
  expect(screen.getByText("处理中")).toBeVisible();
  expect(screen.getByText("任务结束后多退少补")).toBeVisible();
  expect(screen.getByText("暂扣 8 积分")).toBeVisible();
  expect(screen.getByText("结算后多退少补")).toBeVisible();
});

test("已完成的任务显示实扣金额，没有退回就不出现退回芯片", () => {
  render(
    <LedgerEntryTable
      empty="空"
      entries={[
        cycle({
          outcome: "COMPLETED",
          charged_credits: 8,
          net_available_delta: -8,
        }),
      ]}
    />,
  );
  expect(screen.getByText("已完成")).toBeVisible();
  expect(screen.getByText("实扣 8")).toBeVisible();
  expect(screen.getByText("-8 积分")).toBeVisible();
  expect(screen.queryByText(/^退回 \+/)).toBeNull();
});

test("展开逐笔明细：按暂扣 → 实扣 → 退回，收起后消失", () => {
  render(<LedgerEntryTable empty="空" entries={[FAILED]} />);
  const toggle = screen.getByRole("button", { name: "查看 2 笔明细" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  fireEvent.click(toggle);
  const detail = screen.getByRole("table", { name: "逐笔明细" });
  expect(
    within(detail)
      .getAllByRole("row")
      .slice(1)
      .map((tr) => tr.children[1].textContent),
  ).toEqual(["暂扣", "退回"]);
  // 每一笔都同时带「可用」与「暂扣中」两列：暂扣是 -8 / +8，退回是 +8 / -8。
  expect(
    within(detail)
      .getAllByRole("row")
      .slice(1)
      .map((tr) => [1, 2, 3].map((i) => tr.children[i].textContent)),
  ).toEqual([
    ["暂扣", "-8 积分", "+8 积分"],
    ["退回", "+8 积分", "-8 积分"],
  ]);
  expect(screen.getByRole("button", { name: "收起明细" })).toHaveAttribute(
    "aria-expanded",
    "true",
  );
  fireEvent.click(screen.getByRole("button", { name: "收起明细" }));
  expect(screen.queryByRole("table", { name: "逐笔明细" })).toBeNull();
});

test("入账不是消费：标题说入账，来源说充值渠道，绝不落到「早期版本消费」", () => {
  const charge: WalletLedgerEntry = {
    key: "tx-charge",
    kind: "row",
    outcome: "POSTED",
    reserved_credits: 0,
    charged_credits: 0,
    refunded_credits: 0,
    net_available_delta: 600,
    started_at: "2026-09-28T02:00:00Z",
    updated_at: "2026-09-28T02:00:00Z",
    rows: [
      row({
        id: "tx-charge",
        type: "CHARGE",
        available_delta: 600,
        auth_source: null,
        credit_source: "wechat_native",
      }),
    ],
  };
  render(<LedgerEntryTable empty="空" entries={[charge]} />);
  expect(screen.getByText("积分入账")).toBeVisible();
  expect(screen.getByText("微信充值")).toBeVisible();
  expect(screen.getByText("+600 积分")).toBeVisible();
  expect(screen.queryByText("早期版本消费")).toBeNull();
  // 入账没有逐笔明细可展开。
  expect(screen.queryByRole("button", { name: /笔明细/ })).toBeNull();
});

test("没有计费周期的历史实扣行按单笔说话：金额取暂扣列而不是恒为 0 的可用列", () => {
  const legacy: WalletLedgerEntry = {
    key: "legacy-settle",
    kind: "row",
    outcome: "POSTED",
    reserved_credits: 0,
    charged_credits: 0,
    refunded_credits: 0,
    net_available_delta: 0,
    started_at: "2026-08-01T02:00:00Z",
    updated_at: "2026-08-01T02:00:00Z",
    rows: [
      row({
        id: "legacy-settle",
        type: "SETTLE",
        available_delta: 0,
        reserved_delta: -6,
        task_id: "task-old",
        auth_source: null,
      }),
    ],
  };
  render(<LedgerEntryTable empty="空" entries={[legacy]} />);
  expect(screen.getByText("实扣 6")).toBeVisible();
  expect(screen.getByText("-6 积分")).toBeVisible();
  expect(screen.getByText("早期版本消费")).toBeVisible();
});

test("查看任务只在提供回调且这条任务带任务编号时出现，并把那一笔交给回调", () => {
  const withTask = cycle({
    outcome: "COMPLETED",
    charged_credits: 8,
    rows: [
      row({
        id: "reserve",
        type: "RESERVE",
        available_delta: -8,
        reserved_delta: 8,
        billing_operation_id: "op-1",
        generation_batch_id: "batch-1",
      }),
    ],
  });
  const onOpenTask = vi.fn();
  const { rerender } = render(
    <LedgerEntryTable empty="空" entries={[withTask]} />,
  );
  expect(screen.queryByRole("button", { name: "查看任务" })).toBeNull();

  rerender(
    <LedgerEntryTable
      empty="空"
      entries={[withTask]}
      onOpenTask={onOpenTask}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "查看任务" }));
  expect(onOpenTask).toHaveBeenCalledWith(
    expect.objectContaining({ id: "reserve", generation_batch_id: "batch-1" }),
  );

  rerender(
    <LedgerEntryTable
      empty="空"
      entries={[cycle({ outcome: "PENDING" })]}
      onOpenTask={onOpenTask}
    />,
  );
  expect(screen.queryByRole("button", { name: "查看任务" })).toBeNull();
});

test("没有条目时显示空态文案，并始终附带三个词的说明", () => {
  render(<LedgerEntryTable empty="暂无额度流水" entries={[]} />);
  expect(screen.getByText("暂无额度流水")).toBeVisible();
  const legend = screen.getByText(/失败的任务会全额退回/);
  expect(legend).toBeVisible();
  expect(legend).toHaveTextContent("暂扣");
  expect(legend).toHaveTextContent("实扣");
  expect(legend).toHaveTextContent("退回");
});
