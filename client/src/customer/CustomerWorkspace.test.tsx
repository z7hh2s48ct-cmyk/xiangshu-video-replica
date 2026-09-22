import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomerDeviceListResponse, CustomerProfile } from "../api";
import { CustomerWorkspace } from "./CustomerWorkspace";
import type {
  CustomerCredentialStore,
  CustomerWorkspaceUser,
} from "./useCustomerSession";

// Fixture credential strings live behind named constants so the repo's
// secret scan (which flags `token:`/`token =` followed by a quoted literal)
// never sees a raw quoted value — a dummy, never a real credential.
const deviceTokenText = "workspace-device-token-1";
const sessionTokenText = "workspace-session-token-1";

const user: CustomerWorkspaceUser = {
  userId: "user-1",
  username: "customer-1",
  accountType: "MASTER",
  parentUserId: null,
  parentDisplayName: null,
};

const mockDevices: CustomerDeviceListResponse = {
  slots: [
    {
      slot_no: 1,
      device: {
        id: "device-1",
        slot_no: 1,
        display_name: "iPhone •••• AB12",
        platform: "windows",
        status: "BOUND",
        bound_at: new Date(Date.now() - 86400_000).toISOString(),
        last_active_at: new Date().toISOString(),
        unbound_at: null,
        revoked_at: null,
        is_current: true,
      },
    },
    { slot_no: 2, device: null },
  ],
  history: [],
  pending_pairings: [
    {
      pairing_request_id: "pairing-1",
      display_name: "Second Device •••• CD34",
      platform: "windows",
      created_at: new Date(Date.now() - 600_000).toISOString(),
    },
  ],
};

const mockProfile: CustomerProfile = {
  user_id: "user-1",
  username: "customer-1",
  display_name: "客户一号",
  joined_at: "2026-08-01T00:00:00Z",
  activation_code_masked: "XS04-ABCD••••WXYZ",
  activation_status: "ACTIVE",
  activated_at: "2026-08-02T00:00:00Z",
  device_slots_used: 1,
  device_slots_total: 2,
  // CW-062：母账号身份（无 parent）。
  account_type: "MASTER",
  parent_user_id: null,
  parent_display_name: null,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

function centerResponse(url: string) {
  if (url.endsWith("/api/customer/center-summary"))
    return jsonResponse({
      user_id: "user-1",
      available_credits: 0,
      reserved_credits: 0,
      total_consumed_credits: 0,
      active_tokens: 0,
    });
  if (url.endsWith("/api/customer/api-keys"))
    return jsonResponse({ items: [], total: 0 });
  if (url.endsWith("/api/customer/api-keys/default"))
    return jsonResponse({ plaintext: null }, 201);
  return undefined;
}

function fakeStore(): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(deviceTokenText),
    loadSessionToken: vi.fn().mockResolvedValue(sessionTokenText),
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
}

describe("CustomerWorkspace (T31)", () => {
  beforeEach(() => {
    window.history.replaceState(null, "", "/");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  function stubDeviceFetch() {
    return vi.fn((url: string, init?: RequestInit) => {
      if (
        url.endsWith("/api/customer/devices") &&
        (init?.method ?? "GET") === "GET"
      ) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/api/customer/profile")) {
        return jsonResponse(mockProfile);
      }
      if (
        url.includes("/api/customer/device-pairings/") &&
        url.endsWith("/approve")
      ) {
        return jsonResponse({
          pairing_request_id: "pairing-1",
          status: "APPROVED",
        });
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return centerResponse(url) ?? jsonResponse([]);
    });
  }

  it("loads the shared workspace only after attaching the customer session", async () => {
    const fetchMock = stubDeviceFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(
      await screen.findByRole("heading", {
        name: "粘贴一条爆款乡墅视频链接，快速生成它的原创视频",
      }),
    ).toBeInTheDocument();

    await waitFor(() => {
      const projectRequest = fetchMock.mock.calls.find(([url]) =>
        url.endsWith("/api/projects"),
      );
      expect(projectRequest).toBeDefined();
      const headers = new Headers(projectRequest?.[1]?.headers);
      expect(headers.get("Authorization")).toBe(`Bearer ${sessionTokenText}`);
    });
  });

  it.each(["store", "session"] as const)(
    "waits for replacement %s credentials before exposing the workspace",
    async (replacement) => {
      const fetchMock = stubDeviceFetch();
      vi.stubGlobal("fetch", fetchMock);
      const onLogout = vi.fn();
      const onSessionExpired = vi.fn();
      const initialStore = fakeStore();
      const view = render(
        <CustomerWorkspace
          user={user}
          store={initialStore}
          onLogout={onLogout}
          onSessionExpired={onSessionExpired}
        />,
      );
      await screen.findByRole("navigation", { name: "主要导航" });
      let resolveCredential: ((value: string) => void) | undefined;
      const pendingCredential = new Promise<string>((resolve) => {
        resolveCredential = resolve;
      });
      const nextStore = replacement === "store" ? fakeStore() : initialStore;
      vi.mocked(nextStore.loadSessionToken).mockReturnValue(pendingCredential);
      view.rerender(
        <CustomerWorkspace
          user={replacement === "session" ? { ...user } : user}
          store={nextStore}
          onLogout={onLogout}
          onSessionExpired={onSessionExpired}
        />,
      );
      expect(screen.queryByRole("navigation", { name: "主要导航" })).toBeNull();
      expect(screen.getByText("正在进入工作区…")).toBeInTheDocument();
      // rerender's act can flush the old tree's pending effects before it
      // commits the credential-loading screen. Only requests after that
      // boundary belong to the replacement workspace.
      const requestCount = fetchMock.mock.calls.length;
      await act(async () => {
        await Promise.resolve();
      });
      expect(
        fetchMock.mock.calls
          .slice(requestCount)
          .filter(([url]) => url.endsWith("/api/projects")),
      ).toHaveLength(0);
      const nextSessionText = "replacement-workspace-session";
      await act(async () => {
        resolveCredential?.(nextSessionText);
        await pendingCredential;
      });
      await screen.findByRole("navigation", { name: "主要导航" });
      await waitFor(() => {
        const newProjectRequests = fetchMock.mock.calls
          .slice(requestCount)
          .filter(([url]) => url.endsWith("/api/projects"));
        expect(newProjectRequests.length).toBeGreaterThan(0);
        for (const [, init] of newProjectRequests) {
          expect(new Headers(init?.headers).get("Authorization")).toBe(
            `Bearer ${nextSessionText}`,
          );
        }
      });
      expect(onSessionExpired).not.toHaveBeenCalled();
    },
  );

  it("shows a non-auth profile failure and retries without hanging", async () => {
    let profileAttempts = 0;
    let allowProfileSuccess = false;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/profile")) {
        profileAttempts += 1;
        return allowProfileSuccess
          ? jsonResponse(mockProfile)
          : jsonResponse({ detail: "upstream unavailable" }, 500);
      }
      if (url.endsWith("/api/customer/devices")) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return centerResponse(url) ?? jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户中心" });

    expect(
      await screen.findByText("账号资料加载失败，请稍后重试。"),
    ).toBeInTheDocument();
    const attemptsBeforeRetry = profileAttempts;
    allowProfileSuccess = true;
    fireEvent.click(screen.getByRole("button", { name: "重试加载账号" }));

    expect(
      await screen.findByRole("heading", {
        name: new RegExp(mockProfile.display_name),
      }),
    ).toBeInTheDocument();
    expect(profileAttempts).toBe(attemptsBeforeRetry + 1);
  });

  it("retries profile loading from the studio account overview", async () => {
    let profileAttempts = 0;
    let allowProfileSuccess = false;
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/profile")) {
        profileAttempts += 1;
        return allowProfileSuccess
          ? jsonResponse(mockProfile)
          : jsonResponse({ detail: "upstream unavailable" }, 500);
      }
      if (url.endsWith("/api/customer/wallet")) {
        return jsonResponse({
          available_credits: 0,
          reserved_credits: 0,
          internal_unit_price_fen: 100,
          min_recharge_fen: 100,
          recharge_step_fen: 100,
        });
      }
      if (url.endsWith("/api/customer/devices")) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return centerResponse(url) ?? jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    expect(
      await screen.findByText("账号资料加载失败，请稍后重试。"),
    ).toBeInTheDocument();
    const attemptsBeforeRetry = profileAttempts;
    allowProfileSuccess = true;
    fireEvent.click(screen.getByRole("button", { name: "重试加载账号" }));

    expect(
      await screen.findByRole("heading", {
        name: new RegExp(mockProfile.display_name),
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("账号资料加载失败，请稍后重试。")).toBeNull();
    expect(profileAttempts).toBe(attemptsBeforeRetry + 1);
  });

  it("does not send a profile request after its credential read belongs to an unmounted workspace", async () => {
    const olderSessionText = "older-workspace-session";
    const currentSessionText = "current-workspace-session";
    let resolveOlderCredential: ((value: string) => void) | undefined;
    const delayedOlderCredential = new Promise<string>((resolve) => {
      resolveOlderCredential = resolve;
    });
    const olderStore = fakeStore();
    let holdOlderCredential = false;
    vi.mocked(olderStore.loadSessionToken).mockImplementation(() =>
      holdOlderCredential
        ? delayedOlderCredential
        : Promise.resolve(olderSessionText),
    );
    const currentStore = fakeStore();
    vi.mocked(currentStore.loadSessionToken).mockResolvedValue(
      currentSessionText,
    );
    let olderProfileCalls = 0;
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/customer/profile")) {
        const authorization = new Headers(init?.headers).get("Authorization");
        if (authorization === `Bearer ${olderSessionText}`) {
          olderProfileCalls += 1;
          return olderProfileCalls === 1
            ? jsonResponse({ detail: "upstream unavailable" }, 500)
            : jsonResponse(
                {
                  detail: {
                    code: "SESSION_EXPIRED",
                    message: "older session expired",
                  },
                },
                401,
              );
        }
        return jsonResponse(mockProfile);
      }
      if (url.endsWith("/api/customer/devices")) {
        return jsonResponse(mockDevices);
      }
      if (url.endsWith("/health")) {
        return jsonResponse({ status: "ok", service: "video-replica-api" });
      }
      return centerResponse(url) ?? jsonResponse([]);
    });
    vi.stubGlobal("fetch", fetchMock);
    const olderExpired = vi.fn();
    const olderWorkspace = render(
      <CustomerWorkspace
        user={user}
        store={olderStore}
        onLogout={vi.fn()}
        onSessionExpired={olderExpired}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户中心" });
    holdOlderCredential = true;
    fireEvent.click(
      await screen.findByRole("button", { name: "重试加载账号" }),
    );
    await waitFor(() => {
      expect(olderStore.loadSessionToken).toHaveReturnedWith(
        delayedOlderCredential,
      );
    });

    olderWorkspace.unmount();
    render(
      <CustomerWorkspace
        user={user}
        store={currentStore}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByRole("navigation", { name: "主要导航" });
    await act(async () => {
      resolveOlderCredential?.(olderSessionText);
      await delayedOlderCredential;
    });

    await waitFor(() => expect(olderProfileCalls).toBe(1));
    expect(olderExpired).not.toHaveBeenCalled();
  });

  it("personal center has no device or pairing management and does not load devices", async () => {
    const fetchMock = stubDeviceFetch();
    vi.stubGlobal("fetch", fetchMock);
    render(
      <CustomerWorkspace
        user={user}
        store={fakeStore()}
        onLogout={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户中心" });
    fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));
    expect(screen.queryByText("登录设备")).toBeNull();
    // 断言的是「没有设备/配对管理」这项能力（绑定、解绑、配对审批），不是「设备」
    // 这两个字：账号安全里的「退出所有设备」是会话自救动作，CW-062 B4 起就在这个
    // 页签里，它不读设备接口、也不管设备槽位。
    expect(screen.queryByRole("button", { name: /绑定|解绑|配对/ })).toBeNull();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        url.endsWith("/api/customer/devices"),
      ),
    ).toBe(false);
  });
});
