import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import type { AppUpdateController } from "./appUpdate";
import { UpdateDialog } from "./UpdateDialog";

function controllerWith(
  overrides: Partial<AppUpdateController>,
): AppUpdateController {
  return {
    supported: true,
    phase: "available",
    currentVersion: "2.4.0",
    update: { version: "2.5.0", notes: "- 消费记录一条任务一行\n- 用词统一" },
    progress: null,
    error: "",
    dialog: null,
    openManualCheck: vi.fn(),
    beginDownload: vi.fn(),
    restart: vi.fn(),
    retry: vi.fn(),
    close: vi.fn(),
    ...overrides,
  } as AppUpdateController;
}

test("发现新版本：展示新旧版本、更新日志与动作按钮", () => {
  const controller = controllerWith({});
  render(<UpdateDialog update={controller} />);
  expect(screen.getByText("发现新版本 V2.5.0")).toBeVisible();
  expect(screen.getByText(/当前版本 V2\.4\.0/)).toBeVisible();
  expect(screen.getByText(/消费记录一条任务一行/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "立即更新" }));
  expect(controller.beginDownload).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "稍后" }));
  expect(controller.close).toHaveBeenCalledTimes(1);
});

test("下载中：显示进度百分比，且没有可关闭的按钮", () => {
  const controller = controllerWith({
    phase: "downloading",
    progress: { received: 140, total: 200 },
  });
  render(<UpdateDialog update={controller} />);
  expect(screen.getByText("正在下载新版本 V2.5.0")).toBeVisible();
  expect(screen.getByText("70%")).toBeVisible();
  expect(
    screen.queryByRole("button", { name: /稍后|取消|关闭|知道了/ }),
  ).toBeNull();
});

test("已安装：提供立即重启与稍后重启", () => {
  const controller = controllerWith({ phase: "installed" });
  render(<UpdateDialog update={controller} />);
  expect(screen.getByText("新版本已就绪")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "立即重启" }));
  expect(controller.restart).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "稍后重启" }));
  expect(controller.close).toHaveBeenCalledTimes(1);
});

test("失败与已是最新：错误可重试，最新只提示当前版本", () => {
  const failed = controllerWith({
    phase: "failed",
    update: null,
    error: "检查更新失败，请稍后重试。",
  });
  render(<UpdateDialog update={failed} />);
  expect(screen.getByText("更新失败")).toBeVisible();
  expect(screen.getByText("检查更新失败，请稍后重试。")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "重试" }));
  expect(failed.retry).toHaveBeenCalledTimes(1);

  const latest = controllerWith({
    phase: "up-to-date",
    update: null,
  });
  render(<UpdateDialog update={latest} />);
  expect(screen.getByText("已是最新版本")).toBeVisible();
  expect(screen.getByText(/当前版本 V2\.4\.0/)).toBeVisible();
});
