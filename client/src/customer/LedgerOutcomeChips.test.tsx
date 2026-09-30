import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { LedgerOutcomeChips } from "./LedgerOutcomeChips";

const COUNTS = { total: 17, pending: 3, completed: 7, refunded: 5, posted: 2 };

test("每个筛选项带条数，当前项按下", () => {
  render(
    <LedgerOutcomeChips counts={COUNTS} value="refunded" onChange={vi.fn()} />,
  );
  expect(screen.getByRole("group", { name: "按结果筛选" })).toBeVisible();
  const expected: [RegExp, string][] = [
    [/全部/, "17"],
    [/处理中/, "3"],
    [/已完成/, "7"],
    [/有退回/, "5"],
    [/入账与调整/, "2"],
  ];
  for (const [name, count] of expected) {
    expect(screen.getByRole("button", { name })).toHaveTextContent(count);
  }
  expect(screen.getByRole("button", { name: /有退回/ })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(screen.getByRole("button", { name: /全部/ })).toHaveAttribute(
    "aria-pressed",
    "false",
  );
});

test("点击把对应的结果交给调用方，「全部」交空串", () => {
  const onChange = vi.fn();
  render(<LedgerOutcomeChips counts={COUNTS} value="" onChange={onChange} />);
  fireEvent.click(screen.getByRole("button", { name: /处理中/ }));
  fireEvent.click(screen.getByRole("button", { name: /入账与调整/ }));
  fireEvent.click(screen.getByRole("button", { name: /全部/ }));
  expect(onChange.mock.calls.map(([value]) => value)).toEqual([
    "pending",
    "posted",
    "",
  ]);
});

test("条数还没读到时只显示名字，不拿 0 冒充", () => {
  render(<LedgerOutcomeChips counts={undefined} value="" onChange={vi.fn()} />);
  expect(screen.getByRole("button", { name: "有退回" })).toHaveTextContent(
    /^有退回$/,
  );
});
