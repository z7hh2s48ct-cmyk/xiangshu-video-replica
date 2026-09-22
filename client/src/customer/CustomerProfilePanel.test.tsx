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
  // 激活码字段已废弃（2026-09-19），保留为 null 以兼容类型定义
  activation_code_masked: null,
  activation_status: null,
  activated_at: null,
  device_slots_used: 1,
  device_slots_total: 2,
  // CW-062：母账号身份（无 parent）。
  account_type: "MASTER",
  parent_user_id: null,
  parent_display_name: null,
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
  // CW-062：身份缓存不参与这些用例的断言，给出满足接口的最小桩。
  loadIdentity: async () => null,
  // 「记住密码」在这些用例里不参与断言，给出满足接口的最小桩。
  loadRememberedLogin: async () => null,
  saveRememberedLogin: async () => {},
  clearRememberedLogin: async () => {},
};

describe("CustomerProfilePanel", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
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
    // onResetActivationCode 已删除（激活码方案废弃，2026-09-19）
    onSessionExpired: vi.fn(),
    onLogout: vi.fn().mockResolvedValue(undefined),
    onUnbind: vi.fn(),
    onUpdateProfile: vi.fn(),
    profile,
    profileLoadError: "",
    store,
    walletRefreshKey: 0,
  };

  it("groups account and device details in the personal centre", () => {
    render(<CustomerProfilePanel {...defaultProps} />);

    expect(screen.getByRole("heading", { name: "李丽" })).toBeInTheDocument();
    // 激活码展示已删除（激活码方案废弃，2026-09-19），不再断言掩码文本。
    expect(screen.getByText("1 台")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "设备管理" }));
    expect(defaultProps.onRefreshDevices).toHaveBeenCalled();
    expect(screen.getByText("工作电脑 •••• AB12")).toBeInTheDocument();
    expect(screen.getByText(/还没有绑定设备/)).toBeInTheDocument();
  });

  // CW-062：母账号身份在个人中心可见，并且是子账号管理入口的开关。
  it("shows the master badge and the sub-account tab for a master identity", () => {
    render(
      <CustomerProfilePanel
        {...defaultProps}
        identity={{
          accountType: "MASTER",
          parentUserId: null,
          parentDisplayName: null,
        }}
      />,
    );

    expect(screen.getByText("母账号")).toBeInTheDocument();
    // 标题的 accessible name 不含徽章文本（h2 外挂）。
    expect(screen.getByRole("heading", { name: "李丽" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "子账号管理" }),
    ).toBeInTheDocument();
  });

  // CW-062：子账号能看到所属母账号，但没有组织管理权（页签不出现）。
  it("marks a sub-account and hides the management tab", async () => {
    const subProfile: CustomerProfile = {
      ...profile,
      account_type: "SUB",
      parent_user_id: "parent-1",
      parent_display_name: "总部机构",
    };
    render(
      <CustomerProfilePanel
        {...defaultProps}
        profile={subProfile}
        identity={{
          accountType: "SUB",
          parentUserId: "parent-1",
          parentDisplayName: "总部机构",
        }}
      />,
    );

    expect(await screen.findByText("子账号")).toBeInTheDocument();
    expect(screen.getByText(/所属母账号：总部机构/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "子账号管理" }),
    ).not.toBeInTheDocument();
  });

  // Phase 3a：子账号带月度额度时，个人中心出现「本月额度」卡（含用尽警示）。
  it("shows the monthly quota card for a sub-account with a cap", () => {
    const cappedSubProfile: CustomerProfile = {
      ...profile,
      account_type: "SUB",
      parent_user_id: "parent-1",
      parent_display_name: "总部机构",
      monthly_quota_credits: 1000,
      quota_used_credits: 1200,
    };
    render(
      <CustomerProfilePanel
        {...defaultProps}
        now={new Date("2026-09-22T04:00:00Z")}
        profile={cappedSubProfile}
      />,
    );

    expect(screen.getByText("本月额度")).toBeInTheDocument();
    // 剩余额度钳制到 0，不出现负数。
    expect(screen.getByText("剩余 0 积分")).toBeInTheDocument();
    expect(
      screen.getByText("额度已用尽；调高额度或等下月 1 日重置"),
    ).toBeInTheDocument();
  });

  // 评审 P3：负数已用量（历史跨月退回遗留）显示前钳到 0，剩余与文案都不为负。
  it("clamps a negative monthly usage to zero before display", () => {
    const negativeSubProfile: CustomerProfile = {
      ...profile,
      account_type: "SUB",
      parent_user_id: "parent-1",
      parent_display_name: "总部机构",
      monthly_quota_credits: 1000,
      quota_used_credits: -50,
    };
    render(
      <CustomerProfilePanel
        {...defaultProps}
        now={new Date("2026-09-20T04:00:00Z")}
        profile={negativeSubProfile}
      />,
    );

    expect(screen.getByText("剩余 1000 积分")).toBeInTheDocument();
    expect(
      screen.getByText("剩余 100% · 距离月末还有 10 天"),
    ).toBeInTheDocument();
  });

  // Phase 3a：未设额度的子账号只展示用量说明；母账号不出现该卡。
  it("explains an uncapped sub-account and hides the card for a master", () => {
    const { unmount } = render(<CustomerProfilePanel {...defaultProps} />);
    expect(screen.queryByText("本月额度")).not.toBeInTheDocument();
    unmount();

    const uncappedSubProfile: CustomerProfile = {
      ...profile,
      account_type: "SUB",
      parent_user_id: "parent-1",
      parent_display_name: "总部机构",
      monthly_quota_credits: null,
      quota_used_credits: 320,
    };
    render(
      <CustomerProfilePanel {...defaultProps} profile={uncappedSubProfile} />,
    );

    expect(screen.getByText("本月额度")).toBeInTheDocument();
    expect(screen.getByText("已用 320 积分")).toBeInTheDocument();
    expect(
      screen.getByText("额度不限，消费由母账号统一承担"),
    ).toBeInTheDocument();
  });

  // CW-062：身份未知（重装/旧金库）时徽章隐藏，但个人中心其余内容不受影响。
  it("degrades to no badge when the identity is unknown", async () => {
    const identityLoader = vi.fn().mockResolvedValue(null);
    render(
      <CustomerProfilePanel
        {...defaultProps}
        identity={null}
        identityLoader={identityLoader}
        profile={null}
        profileLoadError="账号资料加载失败，请稍后重试。"
      />,
    );

    await waitFor(() => expect(identityLoader).toHaveBeenCalled());
    expect(screen.queryByText("母账号")).not.toBeInTheDocument();
    expect(screen.queryByText("子账号")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "子账号管理" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "客户账号" }),
    ).toBeInTheDocument();
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

  // 测试用例 "shows the replacement activation code once after a confirmed reset" 已删除（激活码方案废弃，2026-09-19）

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

  // 批次1：额度卡显示剩余比例 / 距离月末 / 日均与月末外推（原型 v4 文案）。
  it("shows remaining share and the monthly forecast for a capped sub-account", () => {
    const cappedSubProfile: CustomerProfile = {
      ...profile,
      account_type: "SUB",
      parent_user_id: "parent-1",
      parent_display_name: "总部机构",
      monthly_quota_credits: 5000,
      quota_used_credits: 1800,
    };
    render(
      <CustomerProfilePanel
        {...defaultProps}
        now={new Date("2026-09-19T04:00:00Z")}
        profile={cappedSubProfile}
      />,
    );

    expect(
      screen.getByText("剩余 64% · 距离月末还有 11 天"),
    ).toBeInTheDocument();
    expect(screen.getByText("日均 95 · 预计月末用量 2843")).toBeInTheDocument();
  });

  // 批次1：母账号概览新增「子账号数 / 本月子账号消费」两卡（惰性拉取）。
  it("shows sub-account count and monthly spend cards for a master", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({
        sub_accounts: [
          {
            id: "sub-1",
            username: "employee_001",
            display_name: "张三",
            account_type: "SUB",
            parent_user_id: "user-1",
            is_active: true,
            has_password: true,
            created_at: "2026-09-01T08:00:00Z",
            updated_at: null,
            monthly_quota_credits: 5000,
            quota_used_credits: 350,
            quota_remaining_credits: 4650,
          },
        ],
        total_count: 1,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerProfilePanel
        {...defaultProps}
        store={{
          ...store,
          loadSessionToken: vi.fn().mockResolvedValue("session-token"),
        }}
      />,
    );

    expect(await screen.findByText("子账号数")).toBeInTheDocument();
    expect(screen.getByText("1 个已设月度额度")).toBeInTheDocument();
    expect(screen.getByText("350 积分")).toBeInTheDocument();
  });

  // 批次1：子账号列表拉取失败时概览卡静默隐藏，不打扰个人中心。
  it("keeps the master overview quiet when the sub-account list fails", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
      headers: new Headers(),
      json: async () => ({}),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerProfilePanel
        {...defaultProps}
        store={{
          ...store,
          loadSessionToken: vi.fn().mockResolvedValue("session-token"),
        }}
      />,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(screen.queryByText("子账号数")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  // 评审 P3：无会话 token 时概览卡不发起列表请求（惰性加载的第一道闸）。
  it("skips the overview request when the session token is missing", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const loadSessionToken = vi.fn().mockResolvedValue(null);
    render(
      <CustomerProfilePanel
        {...defaultProps}
        store={{ ...store, loadSessionToken }}
      />,
    );

    await waitFor(() => expect(loadSessionToken).toHaveBeenCalled());
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByText("子账号数")).not.toBeInTheDocument();
  });

  // 评审 P3：子账号身份无组织概览权，概览 effect 连 token 都不读。
  it("does not request the overview for a sub-account identity", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const loadSessionToken = vi.fn().mockResolvedValue("session-token");
    const subProfile: CustomerProfile = {
      ...profile,
      account_type: "SUB",
      parent_user_id: "parent-1",
      parent_display_name: "总部机构",
    };
    render(
      <CustomerProfilePanel
        {...defaultProps}
        profile={subProfile}
        store={{ ...store, loadSessionToken }}
      />,
    );

    expect(await screen.findByText("子账号")).toBeInTheDocument();
    expect(loadSessionToken).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  // 评审 P3：概览拉取成功后切走再切回不重复请求（快照语义，允许过期）。
  it("fetches the overview only once across tab switches", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      headers: new Headers(),
      json: async () => ({
        sub_accounts: [
          {
            id: "sub-1",
            username: "employee_001",
            display_name: "张三",
            account_type: "SUB",
            parent_user_id: "user-1",
            is_active: true,
            has_password: true,
            created_at: "2026-09-01T08:00:00Z",
            updated_at: null,
            monthly_quota_credits: 5000,
            quota_used_credits: 350,
            quota_remaining_credits: 4650,
          },
        ],
        total_count: 1,
      }),
    });
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerProfilePanel
        {...defaultProps}
        store={{
          ...store,
          loadSessionToken: vi.fn().mockResolvedValue("session-token"),
        }}
      />,
    );

    expect(await screen.findByText("子账号数")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "设备管理" }));
    fireEvent.click(screen.getByRole("button", { name: "账号概览" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });
});
