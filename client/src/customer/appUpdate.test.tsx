import { invoke } from "@tauri-apps/api/core";
import { check, type Update } from "@tauri-apps/plugin-updater";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { type AppUpdateController, useAppUpdate } from "./appUpdate";

vi.mock("@tauri-apps/plugin-updater", () => ({ check: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const invokeMock = vi.mocked(invoke);
const checkMock = vi.mocked(check);

/** 与 appUpdate 内部的每日节流同一个键与本地日期戳，验证节流行为。 */
const AUTO_CHECK_DAY_KEY = "uc:update-auto-check-day";
function localDayStamp(): string {
  const now = new Date();
  const month = `${now.getMonth() + 1}`.padStart(2, "0");
  const day = `${now.getDate()}`.padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function fakeUpdate(overrides: Partial<Update> = {}): Update {
  return {
    version: "2.5.0",
    body: "  更新日志内容  ",
    date: null,
    currentVersion: "2.4.0",
    downloadAndInstall: vi.fn(() => Promise.resolve()),
    close: vi.fn(),
    ...overrides,
  } as unknown as Update;
}

let controller: AppUpdateController | null = null;
function Harness({ autoCheck = false }: { autoCheck?: boolean }) {
  controller = useAppUpdate({ autoCheck });
  return (
    <>
      {controller.dialog}
      <button onClick={controller.openManualCheck} type="button">
        手动检查
      </button>
    </>
  );
}

function mountTauriRuntime(): void {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
}

beforeEach(() => {
  vi.resetAllMocks();
  invokeMock.mockResolvedValue(undefined);
  window.localStorage.clear();
  mountTauriRuntime();
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

test("浏览器 lane 不支持更新：手动检查不发起请求也不弹窗", () => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  render(<Harness />);
  expect(controller?.supported).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "手动检查" }));
  expect(checkMock).not.toHaveBeenCalled();
  expect(controller?.dialog).toBeNull();
});

test("手动检查发现新版本：确认后下载安装，最后可一键重启", async () => {
  checkMock.mockResolvedValue(fakeUpdate());
  render(<Harness />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "手动检查" }));
  });
  expect(await screen.findByText("发现新版本 V2.5.0")).toBeVisible();
  expect(screen.getByText("更新日志内容")).toBeVisible();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "立即更新" }));
  });
  expect(await screen.findByText("新版本已就绪")).toBeVisible();

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "立即重启" }));
  });
  expect(invokeMock).toHaveBeenCalledWith("restart_app");
});

test("手动检查已是最新：提示当前版本并记录当日已检查", async () => {
  checkMock.mockResolvedValue(null);
  render(<Harness />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "手动检查" }));
  });
  expect(await screen.findByText("已是最新版本")).toBeVisible();
  expect(window.localStorage.getItem(AUTO_CHECK_DAY_KEY)).toBe(localDayStamp());
});

test("手动检查失败：给出可重试的错误提示", async () => {
  checkMock.mockRejectedValueOnce(new Error("network"));
  render(<Harness />);

  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "手动检查" }));
  });
  expect(await screen.findByText("更新失败")).toBeVisible();
  expect(screen.getByText("检查更新失败，请稍后重试。")).toBeVisible();

  checkMock.mockResolvedValue(null);
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
  });
  expect(await screen.findByText("已是最新版本")).toBeVisible();
});

test("启动自动检查：静默；已是最新不弹窗；一天只查一次", async () => {
  vi.useFakeTimers();
  try {
    checkMock.mockResolvedValue(null);
    const view = render(<Harness autoCheck />);

    // 延迟窗口内不发起检查。
    expect(checkMock).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(8000);
    });
    expect(checkMock).toHaveBeenCalledTimes(1);
    // 已是最新：静默，不弹任何弹窗。
    expect(controller?.dialog).toBeNull();
    expect(controller?.phase).toBe("up-to-date");
    expect(screen.queryByText(/最新版本|新版本/)).toBeNull();
    expect(window.localStorage.getItem(AUTO_CHECK_DAY_KEY)).toBe(
      localDayStamp(),
    );

    // 同一天内再挂载（模拟重启）不再自动检查。
    view.unmount();
    checkMock.mockClear();
    render(<Harness autoCheck />);
    await act(async () => {
      vi.advanceTimersByTime(30000);
    });
    expect(checkMock).not.toHaveBeenCalled();
  } finally {
    vi.useRealTimers();
  }
});

test("启动自动检查发现新版本：延迟后弹窗展示更新日志", async () => {
  vi.useFakeTimers();
  try {
    checkMock.mockResolvedValue(fakeUpdate());
    render(<Harness autoCheck />);

    expect(checkMock).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(8000);
    });
    expect(screen.getByText("发现新版本 V2.5.0")).toBeVisible();
    expect(screen.getByText("更新日志内容")).toBeVisible();
  } finally {
    vi.useRealTimers();
  }
});
