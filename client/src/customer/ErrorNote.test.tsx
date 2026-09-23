import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { ErrorNote } from "./ErrorNote";

test("三层结构：分类提示 → 平台原因 → 重试入口", () => {
  render(
    <ErrorNote
      error={{ status: 503 }}
      message="服务暂时不可用"
      onRetry={() => {}}
      retryLabel="重试加载账号"
    />,
  );
  const alert = screen.getByRole("alert");
  expect(alert).toHaveTextContent("平台这边出了点问题");
  expect(alert).toHaveTextContent("服务暂时不可用");
  expect(
    screen.getByRole("button", { name: "重试加载账号" }),
  ).toBeInTheDocument();
});

test("没有 onRetry 时不渲染重试按钮（但仍给反馈入口）", () => {
  render(<ErrorNote message="读不到价格" />);
  expect(screen.queryByRole("button", { name: /重试|重新加载/ })).toBeNull();
  expect(screen.getByRole("button", { name: "复制错误信息" })).toBeVisible();
});

test("复制错误信息：带上分类与原因，成功后按钮改文案", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  render(<ErrorNote error={{ status: 404 }} message="找不到这个订单" />);

  fireEvent.click(screen.getByRole("button", { name: "复制错误信息" }));

  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  const copied = writeText.mock.calls[0][0] as string;
  expect(copied).toContain("找不到这个订单");
  expect(copied).toContain("时间：");
  expect(await screen.findByText("已复制，可发给客服")).toBeVisible();
});

test("剪贴板不可用时不谎报成功", async () => {
  Object.assign(navigator, {
    clipboard: {
      writeText: vi.fn().mockRejectedValue(new Error("not allowed")),
    },
  });
  render(<ErrorNote message="读不到价格" />);

  fireEvent.click(screen.getByRole("button", { name: "复制错误信息" }));

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "复制错误信息" })).toBeVisible(),
  );
  expect(screen.queryByText("已复制，可发给客服")).toBeNull();
});
