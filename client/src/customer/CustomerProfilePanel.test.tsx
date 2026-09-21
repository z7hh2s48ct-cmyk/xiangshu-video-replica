import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { CustomerDeviceListResponse, CustomerProfile } from "../api";
import { CustomerProfilePanel } from "./CustomerProfilePanel";
import type {
  CustomerCredentialStore,
  CustomerLogoutOutcome,
} from "./useCustomerSession";

const profile: CustomerProfile = {
  user_id: "user-1",
  username: "customer-1",
  display_name: "李丽",
  joined_at: "2026-08-01T00:00:00Z",
  activation_code_masked: "XS04-ABCD••••WXYZ",
  activation_status: "ACTIVE",
  activated_at: "2026-08-02T00:00:00Z",
  device_slots_used: 1,
  device_slots_total: 2,
};

const devices: CustomerDeviceListResponse = {
  slots: [
    {
      slot_no: 1,
      device: {
        id: "device-1",
        slot_no: 1,
        display_name: "工作电脑 •••• AB12",
        platform: "windows",
        status: "BOUND",
        bound_at: "2026-08-02T00:00:00Z",
        last_active_at: "2026-08-27T00:00:00Z",
        unbound_at: null,
        revoked_at: null,
        is_current: true,
      },
    },
    { slot_no: 2, device: null },
  ],
  history: [],
  pending_pairings: [],
};

const store: CustomerCredentialStore = {
  loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
  loadSessionToken: vi.fn().mockResolvedValue(null),
  saveActivation: vi.fn().mockResolvedValue(undefined),
  saveSessionToken: vi.fn().mockResolvedValue(undefined),
  clearSessionToken: vi.fn().mockResolvedValue(undefined),
  clearAllCredentials: vi.fn().mockResolvedValue(undefined),
  deviceInstanceId: vi.fn().mockResolvedValue("test-instance-id"),
  devicePlatform: () => "windows",
  // 「记住密码」在这些用例里不参与断言，给出满足接口的最小桩。
  loadRememberedLogin: async () => null,
  saveRememberedLogin: async () => {},
  clearRememberedLogin: async () => {},
};

describe("CustomerProfilePanel", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const defaultProps = {
    deviceError: "",
    devices,
    onApprovePairing: vi.fn(),
    onDismissPairing: vi.fn(),
    onProfileUpdated: vi.fn(),
    onRefreshProfile: vi.fn().mockResolvedValue(undefined),
    onRecharge: vi.fn(),
    onRefreshDevices: vi.fn().mockResolvedValue(undefined),
    onResetActivationCode: vi.fn(),
    onSessionExpired: vi.fn(),
    onLogout: vi.fn().mockResolvedValue(undefined),
    onUnbind: vi.fn(),
    onUpdateProfile: vi.fn(),
    profile,
    profileLoadError: "",
    store,
    walletRefreshKey: 0,
  };

  it("groups account, activation and device details in the personal centre", () => {
    render(<CustomerProfilePanel {...defaultProps} />);

    expect(screen.getByRole("heading", { name: "李丽" })).toBeInTheDocument();
    expect(screen.getByText("XS04-ABCD••••WXYZ")).toBeInTheDocument();
    expect(screen.getByText("1 台")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "设备管理" }));
    expect(defaultProps.onRefreshDevices).toHaveBeenCalled();
    expect(screen.getByText("工作电脑 •••• AB12")).toBeInTheDocument();
    expect(screen.getByText(/还没有绑定设备/)).toBeInTheDocument();
  });

  it("edits the display name while keeping the account number read-only", async () => {
    const onUpdateProfile = vi.fn().mockResolvedValue({
      ...profile,
      display_name: "丽丽工作室",
    });
    const onProfileUpdated = vi.fn();
    render(
      <CustomerProfilePanel
        {...defaultProps}
        onProfileUpdated={onProfileUpdated}
        onUpdateProfile={onUpdateProfile}
      />,
    );

    expect(screen.getByText("customer-1")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("显示名称"), {
      target: { value: "丽丽工作室" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存个人资料" }));

    await waitFor(() =>
      expect(onUpdateProfile).toHaveBeenCalledWith("丽丽工作室"),
    );
    expect(onProfileUpdated).toHaveBeenCalledWith(
      expect.objectContaining({ display_name: "丽丽工作室" }),
    );
  });

  it("shows the replacement activation code once after a confirmed reset", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const onResetActivationCode = vi.fn().mockResolvedValue({
      activation_code: "XS04-NEWCODE-NEWCODE-NEWCODE-NEWCODE",
      masked_code: "XS04-NEWC***-*******-*******-***CODE",
    });
    render(
      <CustomerProfilePanel
        {...defaultProps}
        onResetActivationCode={onResetActivationCode}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "重置激活码" }));

    expect(
      await screen.findByText("XS04-NEWCODE-NEWCODE-NEWCODE-NEWCODE"),
    ).toBeInTheDocument();
    expect(screen.getByText(/新激活码仅显示这一次/)).toBeInTheDocument();
  });

  it("shows heartbeat and lease health in the device tab and renews on demand", async () => {
    const onManualHeartbeat = vi.fn();
    const leaseExpiresAt = new Date(Date.now() + 30 * 60_000).toISOString();
    render(
      <CustomerProfilePanel
        {...defaultProps}
        sessionRuntime={{
          connectivity: "reachable",
          lastHeartbeatAt: new Date(Date.now() - 5_000).toISOString(),
          leaseExpiresAt,
        }}
        onManualHeartbeat={onManualHeartbeat}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "设备管理" }));

    expect(screen.getByText(/上次心跳/)).toBeInTheDocument();
    expect(screen.getByText(/连接正常/)).toBeInTheDocument();
    expect(screen.getByText(/本次会话有效至/)).toBeInTheDocument();
    expect(screen.getByText(/剩余 \d+ 分钟/)).toBeInTheDocument();
    // The device page header mirrors the live lease instead of the old null.
    expect(screen.getByText(/本次登录有效至/)).toBeInTheDocument();

    fireEvent.click(
      screen.getAllByRole("button", { name: /立即续约|立即发送心跳/ })[0],
    );
    expect(onManualHeartbeat).toHaveBeenCalledTimes(1);
  });

  it("shows a failed profile request and retries it explicitly", async () => {
    const onRefreshProfile = vi.fn().mockResolvedValue(undefined);
    render(
      <CustomerProfilePanel
        {...defaultProps}
        profile={null}
        profileLoadError="账号资料加载失败，请稍后重试。"
        onRefreshProfile={onRefreshProfile}
      />,
    );

    expect(
      screen.getByRole("alert", { name: "账号资料加载失败，请稍后重试。" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载账号资料" }));

    await waitFor(() => expect(onRefreshProfile).toHaveBeenCalledTimes(1));
  });

  it("derives the device online state from the server lease", () => {
    render(
      <CustomerProfilePanel
        {...defaultProps}
        sessionRuntime={{
          connectivity: "unreachable",
          lastHeartbeatAt: new Date(Date.now() - 40_000).toISOString(),
          leaseExpiresAt: new Date(Date.now() - 1_000).toISOString(),
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "设备管理" }));

    expect(screen.getByText("网络暂不可达")).toBeInTheDocument();
    expect(screen.getByText("本机离线")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "解绑当前设备" })).toBeDisabled();
  });

  it("switches the device offline when the active server lease reaches its expiry", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-07T12:00:00Z"));
    render(
      <CustomerProfilePanel
        {...defaultProps}
        sessionRuntime={{
          connectivity: "reachable",
          lastHeartbeatAt: new Date().toISOString(),
          leaseExpiresAt: new Date(Date.now() + 500).toISOString(),
        }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "设备管理" }));
    expect(screen.getByText("本机在线")).toBeInTheDocument();

    await act(async () => vi.advanceTimersByTimeAsync(501));

    expect(screen.getByText("本机离线")).toBeInTheDocument();
  });

  it("prevents duplicate logout actions while the first request is pending", async () => {
    let finishLogout: ((outcome: CustomerLogoutOutcome) => void) | undefined;
    const onLogout = vi.fn(
      () =>
        new Promise<CustomerLogoutOutcome>((resolve) => {
          finishLogout = resolve;
        }),
    );
    render(<CustomerProfilePanel {...defaultProps} onLogout={onLogout} />);

    const logoutButton = screen.getByRole("button", { name: "退出登录" });
    fireEvent.click(logoutButton);
    fireEvent.click(logoutButton);

    expect(onLogout).toHaveBeenCalledTimes(1);
    expect(logoutButton).toBeDisabled();
    expect(logoutButton).toHaveTextContent("正在退出");

    await act(async () =>
      finishLogout?.({ serverReleased: true, credentialCleared: true }),
    );
    expect(logoutButton).toBeEnabled();
    expect(logoutButton).toHaveTextContent("退出登录");
  });
});
