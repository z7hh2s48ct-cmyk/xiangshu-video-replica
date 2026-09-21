import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SubAccountManagementPage } from "./SubAccountManagementPage";
import type { CustomerCredentialStore } from "./useCustomerSession";

/** 最小凭证桩：页面只经 loadSessionToken 取会话（其余接口不参与本页）。 */
function storeWithSession(token: string | null): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(token),
    saveActivation: vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("instance-1"),
    devicePlatform: () => "windows",
    loadIdentity: vi.fn().mockResolvedValue(null),
    loadRememberedLogin: vi.fn().mockResolvedValue(null),
    saveRememberedLogin: vi.fn().mockResolvedValue(undefined),
    clearRememberedLogin: vi.fn().mockResolvedValue(undefined),
  };
}

const sessionToken = "session-token-text";

const subAccount = {
  id: "sub-1",
  username: "employee_001",
  display_name: "张三",
  account_type: "SUB" as const,
  parent_user_id: "master-1",
  is_active: true,
  has_password: true,
  created_at: "2026-09-01T08:00:00Z",
  updated_at: null,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: async () => payload,
  });
}

/** 记录请求；默认所有非列表端点返回成功形状。 */
function stubFetch(
  handler: (url: string, init?: RequestInit) => Promise<unknown>,
) {
  const fetchMock = vi.fn().mockImplementation(handler);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("SubAccountManagementPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("lists the organisation's sub-accounts under the customer session", async () => {
    const fetchMock = stubFetch((url) => {
      if (url.endsWith("/api/customer/sub-accounts")) {
        return jsonResponse({ sub_accounts: [subAccount], total_count: 1 });
      }
      return jsonResponse({}, 500);
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );

    expect(await screen.findByText("张三")).toBeInTheDocument();
    expect(screen.getByText("employee_001")).toBeInTheDocument();
    expect(screen.getByText("已设密码")).toBeInTheDocument();
    expect(screen.getByText("正常")).toBeInTheDocument();
    // 列表请求必须携带会话 token（客户围栏），不能是设备凭据。
    const [, init] = fetchMock.mock.calls[0];
    expect(new Headers(init?.headers).get("Authorization")).toBe(
      `Bearer ${sessionToken}`,
    );
  });

  it("creates a sub-account and surfaces a username conflict as a readable error", async () => {
    const fetchMock = stubFetch((url, init) => {
      if (
        url.endsWith("/api/customer/sub-accounts") &&
        init?.method === "POST"
      ) {
        return jsonResponse(
          { detail: { code: "USERNAME_TAKEN", message: "该用户名已被占用。" } },
          409,
        );
      }
      return jsonResponse({ sub_accounts: [], total_count: 0 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText(/还没有子账号/);

    fireEvent.change(screen.getByPlaceholderText("登录账号，全局唯一"), {
      target: { value: "employee_001" },
    });
    fireEvent.change(screen.getByPlaceholderText("团队里怎么称呼"), {
      target: { value: "张三" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建子账号" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "该用户名已被占用。",
    );
    const postCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(postCall).toBeDefined();
    const body = JSON.parse(String(postCall?.[1]?.body));
    expect(body).toEqual({ username: "employee_001", display_name: "张三" });
  });

  it("submits the optional initial password when provided", async () => {
    let created = false;
    const fetchMock = stubFetch((url, init) => {
      if (
        url.endsWith("/api/customer/sub-accounts") &&
        init?.method === "POST"
      ) {
        created = true;
        return jsonResponse({ ...subAccount, has_password: true }, 201);
      }
      return jsonResponse({
        sub_accounts: created ? [subAccount] : [],
        total_count: created ? 1 : 0,
      });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText(/还没有子账号/);

    fireEvent.change(screen.getByPlaceholderText("登录账号，全局唯一"), {
      target: { value: "employee_001" },
    });
    fireEvent.change(screen.getByPlaceholderText("团队里怎么称呼"), {
      target: { value: "张三" },
    });
    fireEvent.change(screen.getByPlaceholderText("留空则稍后设置"), {
      target: { value: "pass-9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建子账号" }));

    expect(
      await screen.findByText(/子账号已创建，可以立即登录。/),
    ).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      username: "employee_001",
      display_name: "张三",
      password: "pass-9",
    });
  });

  it("deactivation asks for confirmation first and then patches is_active", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "PATCH") {
        return jsonResponse({ ...subAccount, is_active: false });
      }
      return jsonResponse({ sub_accounts: [subAccount], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三");

    fireEvent.click(screen.getByRole("button", { name: "停用" }));

    expect(confirmSpy).toHaveBeenCalled();
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
      ).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      is_active: false,
    });
  });

  it("hands an expired session to onSessionExpired instead of showing an error", async () => {
    const onSessionExpired = vi.fn();
    stubFetch(() =>
      jsonResponse(
        { detail: { code: "SESSION_REQUIRED", message: "session required" } },
        401,
      ),
    );
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={onSessionExpired}
      />,
    );

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("delete degrades to deactivation with an explicit notice when history pins the row", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    stubFetch((_url, init) => {
      if (init?.method === "DELETE") {
        return jsonResponse({ id: "sub-1", deleted: false, is_active: false });
      }
      return jsonResponse({ sub_accounts: [subAccount], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三");

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    expect(
      await screen.findByText("该子账号已有消费记录，已转为停用保留。"),
    ).toBeInTheDocument();
  });
});
