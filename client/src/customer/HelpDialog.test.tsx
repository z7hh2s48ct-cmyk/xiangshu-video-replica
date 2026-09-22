import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { HELP_ENTRIES, HelpDialog, TermHint } from "./HelpDialog";

test("帮助中心列出全部条目，且答案与界面用词对得上", () => {
  render(<HelpDialog onClose={() => {}} />);
  expect(screen.getByRole("dialog", { name: "帮助中心" })).toBeVisible();
  for (const entry of HELP_ENTRIES) {
    expect(screen.getByText(entry.question)).toBeVisible();
  }
  // 界面上真实出现的三个术语都要能被解释到（术语可能在问句里，也答句里）
  const text = HELP_ENTRIES.map((entry) => entry.question + entry.answer).join(
    " ",
  );
  for (const term of ["待结算", "早期版本消费", "第 N 次更新"]) {
    expect(text).toContain(term);
  }
});

test("「知道了」关闭帮助", () => {
  let closed = 0;
  render(
    <HelpDialog
      onClose={() => {
        closed += 1;
      }}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "知道了" }));
  expect(closed).toBe(1);
});

test("术语提示带原生 title，键盘与读屏都能拿到", () => {
  render(<TermHint hint="任务结束后多扣的会退回" term="待结算" />);
  const node = screen.getByTitle("任务结束后多扣的会退回");
  expect(node).toHaveTextContent("待结算");
});
