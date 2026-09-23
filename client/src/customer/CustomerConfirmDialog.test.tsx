import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";
import {
  type CustomerConfirmLevel,
  useCustomerConfirm,
} from "./CustomerConfirmDialog";

// 用一个小外壳把 hook 的「打开 → 确认 → 关闭」整条链路跑起来，测的是调用方
// 真正会用的那套 API，而不是内部状态。
function Harness({
  level,
  onConfirm,
}: {
  level: CustomerConfirmLevel;
  onConfirm: () => void | Promise<void>;
}) {
  const { confirm, dialog } = useCustomerConfirm();
  return (
    <div>
      <button
        onClick={() =>
          confirm({
            title: "危险操作？",
            description: "这个动作不可撤销。",
            level,
            confirmLabel: "执行",
            onConfirm,
          })
        }
        type="button"
      >
        打开
      </button>
      {dialog}
    </div>
  );
}

test("standard 级：只需一次明确的确认，不需要勾选", async () => {
  const onConfirm = vi.fn();
  render(<Harness level="standard" onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole("button", { name: "打开" }));

  const dialog = await screen.findByRole("dialog", { name: "危险操作？" });
  expect(within(dialog).queryByRole("checkbox")).toBeNull();
  fireEvent.click(within(dialog).getByRole("button", { name: "执行" }));

  await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "危险操作？" })).toBeNull(),
  );
});

test("acknowledge 级：没勾选之前不执行，勾选后才放行", async () => {
  const onConfirm = vi.fn();
  render(<Harness level="acknowledge" onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole("button", { name: "打开" }));

  const dialog = await screen.findByRole("dialog", { name: "危险操作？" });
  fireEvent.click(within(dialog).getByRole("button", { name: "执行" }));
  expect(onConfirm).not.toHaveBeenCalled();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "请先勾选确认操作",
  );

  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "执行" }));
  await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
});

test("失败的信息留在框内，用户还能重试或取消", async () => {
  const onConfirm = vi
    .fn()
    .mockRejectedValueOnce(new Error("服务暂时不可用"))
    .mockResolvedValueOnce(undefined);
  render(<Harness level="standard" onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole("button", { name: "打开" }));

  const dialog = await screen.findByRole("dialog", { name: "危险操作？" });
  fireEvent.click(within(dialog).getByRole("button", { name: "执行" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "服务暂时不可用",
  );
  // 框还在，重试直接成功。
  expect(screen.getByRole("dialog", { name: "危险操作？" })).toBeVisible();
  fireEvent.click(within(dialog).getByRole("button", { name: "执行" }));
  await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(2));
  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "危险操作？" })).toBeNull(),
  );
});

test("取消不会触发动作", async () => {
  const onConfirm = vi.fn();
  render(<Harness level="acknowledge" onConfirm={onConfirm} />);
  fireEvent.click(screen.getByRole("button", { name: "打开" }));

  const dialog = await screen.findByRole("dialog", { name: "危险操作？" });
  fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));

  await waitFor(() =>
    expect(screen.queryByRole("dialog", { name: "危险操作？" })).toBeNull(),
  );
  expect(onConfirm).not.toHaveBeenCalled();
});
