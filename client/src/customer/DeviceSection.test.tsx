import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import type { CustomerDeviceListResponse } from "../api";
import { DeviceSection } from "./DeviceSection";

afterEach(() => {
  window.localStorage.clear();
});

const currentDevice = {
  id: "dev-1",
  slot_no: 1,
  display_name: "办公室台式机",
  platform: "windows",
  status: "BOUND",
  bound_at: "2026-09-01T02:00:00Z",
  last_active_at: "2026-09-22T02:00:00Z",
  unbound_at: null,
  revoked_at: null,
  is_current: true,
};

const retiredDevice = {
  ...currentDevice,
  id: "dev-0",
  display_name: "旧笔记本",
  is_current: false,
  unbound_at: "2026-08-01T02:00:00Z",
};

const pendingPairing = {
  pairing_request_id: "pair-1",
  display_name: "新的笔记本",
  platform: "macos",
  created_at: "2026-09-22T03:00:00Z",
};

const liveRuntime = {
  connectivity: "reachable" as const,
  lastHeartbeatAt: "2026-09-22T02:00:00Z",
  leaseExpiresAt: "2099-01-01T00:00:00Z",
};

const devicePayload = {
  slots: [{ slot_no: 1, device: currentDevice }],
  history: [retiredDevice],
  pending_pairings: [pendingPairing],
} as unknown as CustomerDeviceListResponse;

function fakeHandlers() {
  return {
    onApprovePairing: vi.fn(),
    onDismissPairing: vi.fn(),
    onManualHeartbeat: vi.fn(),
    onPairDevice: vi.fn(),
    onRecharge: vi.fn(),
    onRefreshDevices: vi.fn().mockResolvedValue(undefined),
    onUnbind: vi.fn(),
  };
}

function setup(overrides: Record<string, unknown> = {}) {
  const handlers = fakeHandlers();
  render(
    <DeviceSection
      deviceError=""
      devices={devicePayload}
      scope="user-1"
      sessionRuntime={liveRuntime}
      {...handlers}
      {...overrides}
    />,
  );
  return handlers;
}

test("renders the bound slot, the retired history and the pending pairing", () => {
  setup();

  expect(screen.getByText("办公室台式机")).toBeVisible();
  // 旧面板只渲染 2 个槽位，解绑过的设备在界面上无迹可寻——历史在这里补上。
  expect(screen.getByText("旧笔记本")).toBeVisible();
  expect(screen.getByText(/新的笔记本 · macos/)).toBeVisible();
  expect(screen.getByText("本机在线")).toBeVisible();
});

test("unbinding hands the device id to the caller", () => {
  const handlers = setup();

  fireEvent.click(screen.getByRole("button", { name: "解绑当前设备" }));

  expect(handlers.onUnbind).toHaveBeenCalledWith("dev-1");
});

test("a lapsed lease flips the slot offline and disables unbinding", () => {
  setup({
    sessionRuntime: { ...liveRuntime, leaseExpiresAt: "2020-01-01T00:00:00Z" },
  });

  expect(screen.getByText("本机离线")).toBeVisible();
  expect(screen.getByRole("button", { name: "解绑当前设备" })).toBeDisabled();
});

test("the pairing card routes confirm and delete to their own handlers", () => {
  const handlers = setup();

  fireEvent.click(screen.getByRole("button", { name: "确认绑定" }));
  expect(handlers.onApprovePairing).toHaveBeenCalledWith("pair-1");
});

test("deferring hides the request but keeps a way back", () => {
  setup();

  fireEvent.click(screen.getByRole("button", { name: "暂不处理" }));

  expect(screen.queryByRole("button", { name: "确认绑定" })).toBeNull();
  const toggle = screen.getByRole("button", { name: "显示已暂缓的 1 条" });
  fireEvent.click(toggle);

  expect(screen.getByRole("button", { name: "确认绑定" })).toBeVisible();
});

test("refresh asks the caller to reload devices", async () => {
  const handlers = setup();

  fireEvent.click(screen.getByRole("button", { name: "刷新" }));

  await waitFor(() =>
    expect(handlers.onRefreshDevices).toHaveBeenCalledTimes(1),
  );
});

test("a device error is surfaced as an alert", () => {
  setup({ deviceError: "读取设备失败，请重试。" });

  expect(screen.getByRole("alert")).toHaveTextContent("读取设备失败，请重试。");
});

test("「暂不处理」按账号记住：重挂后仍是暂缓态", () => {
  const first = setup();
  fireEvent.click(screen.getByRole("button", { name: "暂不处理" }));
  expect(screen.queryByRole("button", { name: "确认绑定" })).toBeNull();
  expect(first.onApprovePairing).not.toHaveBeenCalled();

  // 卸载后用同一个 scope 重新挂载：暂缓状态应从 localStorage 读回来
  cleanup();
  setup();
  expect(screen.queryByRole("button", { name: "确认绑定" })).toBeNull();
  expect(
    screen.getByRole("button", { name: "显示已暂缓的 1 条" }),
  ).toBeVisible();
});

test("另一个账号的暂缓不会串进来", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: "暂不处理" }));
  cleanup();

  // 换账号（不同 scope）：那条请求应仍然可见
  setup({ scope: "user-2" });
  expect(screen.getByRole("button", { name: "确认绑定" })).toBeVisible();
});

test("scope 变化先重读新账号的暂缓，不用旧集合覆盖它（评审 #7）", () => {
  // user-2 此前暂缓过 pair-1——这是本次要保护的数据。
  window.localStorage.setItem(
    "uc:deferred-pairings:user-2",
    JSON.stringify(["pair-1"]),
  );
  const handlers = fakeHandlers();
  const element = (scope: string) => (
    <DeviceSection
      deviceError=""
      devices={devicePayload}
      scope={scope}
      sessionRuntime={liveRuntime}
      {...handlers}
    />
  );

  // profile 还没到时组件以 anonymous 挂载（它是空集合），随后 profile 到达、
  // scope 换成真实 user_id。此前这次切换会把匿名空集合写到 user-2 的键上。
  const { rerender } = render(element("anonymous"));
  rerender(element("user-2"));

  expect(window.localStorage.getItem("uc:deferred-pairings:user-2")).toBe(
    JSON.stringify(["pair-1"]),
  );
  // 而且确实把 user-2 的暂缓读了回来：那条配对请求在界面上是隐藏的
  expect(screen.queryByRole("button", { name: "确认绑定" })).toBeNull();
});
