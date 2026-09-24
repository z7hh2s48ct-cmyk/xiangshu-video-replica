import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
  // Phase 3a 月度额度：缺省 = 未设额度（null 不限 / 已用 0）。
  monthly_quota_credits: null,
  quota_used_credits: 0,
  quota_remaining_credits: null,
  // Phase 3b 功能权限：缺省 = 全允许（null 无权限行）。
  permissions: null,
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

/** 走完一次产品级确认框：勾选「我已知晓」→ 点确认按钮（P0 清单 #2 起的高危交互）。 */
async function acknowledgeAndConfirm(dialogName: string, confirmLabel: string) {
  const dialog = await screen.findByRole("dialog", { name: dialogName });
  fireEvent.click(within(dialog).getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: confirmLabel }));
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

    expect(
      await screen.findByText("张三", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(screen.getByText("employee_001")).toBeInTheDocument();
    expect(screen.getByText("已设密码")).toBeInTheDocument();
    expect(screen.getByText("正常")).toBeInTheDocument();
    // Phase 3a：未设额度的子账号在卡片上显示「额度不限」。
    expect(screen.getByText("额度不限")).toBeInTheDocument();
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
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "停用" }));

    // 未确认之前不发请求。
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);
    await acknowledgeAndConfirm("停用「张三」？", "停用子账号");

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

  it("keeps the session when the server denies an operation with 403 (SUB_ADMIN scope)", async () => {
    // 上线前检查 P1-1：403 是权限拒绝，不是会话失效。旧实现把 403 与 401
    // 同判，SUB_ADMIN 点任一母账号专属按钮就被强制登出且文案误导。
    const onSessionExpired = vi.fn();
    stubFetch((url, init) => {
      if (
        url.includes("/api/customer/sub-accounts") &&
        init?.method === "PATCH"
      ) {
        return jsonResponse(
          {
            detail: {
              code: "MASTER_ACCOUNT_REQUIRED",
              message: "该操作仅限母账号执行。",
            },
          },
          403,
        );
      }
      return jsonResponse({ sub_accounts: [subAccount], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={onSessionExpired}
      />,
    );

    // 列表正常渲染（列表对 SUB_ADMIN 放行），会话保持在线。
    expect(
      await screen.findByText("张三", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(onSessionExpired).not.toHaveBeenCalled();

    // 触发一个会 403 的操作（设为管理员走 PATCH）：错误留在页面，不登出。
    fireEvent.click(screen.getByRole("button", { name: "设为管理员" }));
    await acknowledgeAndConfirm("设为管理员：「张三」？", "设为管理员");
    await waitFor(() =>
      expect(screen.getByText("该操作仅限母账号执行。")).toBeInTheDocument(),
    );
    expect(onSessionExpired).not.toHaveBeenCalled();
  });

  it("hides master-only controls for a SUB_ADMIN caller but keeps quota and permissions", async () => {
    // 上线前检查 P1-1：服务端 _lock_master 边界 = 创建/改名/密码/角色/停用/删除
    // 仅母账号；额度（PUT quota）与权限（PUT permissions）对 SUB_ADMIN 放行。
    stubFetch(() =>
      jsonResponse({ sub_accounts: [subAccount], total_count: 1 }),
    );
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
        isMasterCaller={false}
      />,
    );

    expect(
      await screen.findByText("张三", { selector: "strong" }),
    ).toBeInTheDocument();
    // 母账号专属：创建表单与行内操作全部隐藏。
    expect(screen.queryByText("创建子账号")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "重命名" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "设置密码" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "设为管理员" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "停用" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "删除" }),
    ).not.toBeInTheDocument();
    // SUB_ADMIN 仍被授权的操作保留。
    expect(
      screen.getByRole("button", { name: "设置额度" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "设置权限" }),
    ).toBeInTheDocument();
  });

  it("delete degrades to deactivation with an explicit notice when history pins the row", async () => {
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
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await acknowledgeAndConfirm("删除子账号「张三」？", "删除子账号");

    expect(
      await screen.findByText("该子账号已有消费记录，已转为停用保留。"),
    ).toBeInTheDocument();
  });

  // Phase 3a：已设额度的子账号渲染进度条，用尽时加 is-exhausted 标记。
  it("renders the monthly quota progress and marks an exhausted cap", async () => {
    const capped = {
      ...subAccount,
      monthly_quota_credits: 2000,
      quota_used_credits: 2000,
      quota_remaining_credits: 0,
    };
    stubFetch(() => jsonResponse({ sub_accounts: [capped], total_count: 1 }));
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    expect(
      screen.getByText("本月已用 2000 / 2000 积分（100%）"),
    ).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("max", "2000");
    expect(bar).toHaveAttribute("value", "2000");
    expect(bar.className).toContain("is-exhausted");
  });

  // 评审 P3：负数已用量（历史跨月退回遗留）显示为 0，进度条不接受负值。
  it("clamps a negative monthly usage to zero in the progress bar", async () => {
    const negative = {
      ...subAccount,
      monthly_quota_credits: 2000,
      quota_used_credits: -40,
      quota_remaining_credits: 2000,
    };
    stubFetch(() => jsonResponse({ sub_accounts: [negative], total_count: 1 }));
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    expect(
      screen.getByText("本月已用 0 / 2000 积分（0%）"),
    ).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("value", "0");
    expect(bar.className).not.toContain("is-exhausted");
  });

  // 评审 P2：上限为 0（不允许消费）时进度条按满条渲染，不因 max=0 回退成空条。
  it("renders a full bar for a zero cap instead of an empty one", async () => {
    const zeroCapped = {
      ...subAccount,
      monthly_quota_credits: 0,
      quota_used_credits: 0,
      quota_remaining_credits: 0,
    };
    stubFetch(() =>
      jsonResponse({ sub_accounts: [zeroCapped], total_count: 1 }),
    );
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    expect(screen.getByText("本月已用 0 / 0 积分（100%）")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar).toHaveAttribute("max", "1");
    expect(bar).toHaveAttribute("value", "1");
    expect(bar.className).toContain("is-exhausted");
  });

  // Phase 3a：创建时可一并提交初始额度（留空则不下发该字段）。
  it("submits the optional monthly quota when creating a sub-account", async () => {
    let created = false;
    const fetchMock = stubFetch((url, init) => {
      if (
        url.endsWith("/api/customer/sub-accounts") &&
        init?.method === "POST"
      ) {
        created = true;
        return jsonResponse(
          {
            ...subAccount,
            monthly_quota_credits: 500,
            quota_remaining_credits: 500,
          },
          201,
        );
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
    fireEvent.change(screen.getByPlaceholderText("留空则不限"), {
      target: { value: "500" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建子账号" }));

    expect(
      await screen.findByText(/子账号已创建；设置密码后即可登录。/),
    ).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      username: "employee_001",
      display_name: "张三",
      monthly_quota_credits: 500,
    });
  });

  // Phase 3a：非法额度在前端拦截，不发出创建请求。
  it("rejects an invalid monthly quota before creating", async () => {
    const fetchMock = stubFetch(() =>
      jsonResponse({ sub_accounts: [], total_count: 0 }),
    );
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
    fireEvent.change(screen.getByPlaceholderText("留空则不限"), {
      target: { value: "1.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建子账号" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "月度额度必须是不超过 10 亿的整数",
    );
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
  });

  // Phase 3a → P2-3：「设置额度」经受控弹窗取值，PUT 到子账号额度端点。
  it("sets the monthly quota through the dialog and the quota endpoint", async () => {
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "PUT") {
        return jsonResponse({
          ...subAccount,
          monthly_quota_credits: 500,
          quota_remaining_credits: 500,
        });
      }
      return jsonResponse({ sub_accounts: [subAccount], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "设置额度" }));
    const dialog = await screen.findByRole("dialog", {
      name: "设置额度 张三",
    });
    fireEvent.change(within(dialog).getByLabelText("月度额度"), {
      target: { value: "500" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存额度" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PUT"),
      ).toBe(true),
    );
    const putCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PUT",
    );
    expect(String(putCall?.[0])).toContain(
      "/api/customer/sub-accounts/sub-1/quota",
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      monthly_quota_credits: 500,
    });
    expect(
      await screen.findByText("已将「张三」的月度额度设为 500 积分。"),
    ).toBeInTheDocument();
  });

  // Phase 3a：留空提交 → 清除额度（null），提示语走「清除」分支。
  it("clears the monthly quota when the operator submits an empty input", async () => {
    const capped = {
      ...subAccount,
      monthly_quota_credits: 2000,
      quota_used_credits: 300,
      quota_remaining_credits: 1700,
    };
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "PUT") {
        return jsonResponse(subAccount);
      }
      return jsonResponse({ sub_accounts: [capped], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "设置额度" }));
    const dialog = await screen.findByRole("dialog", {
      name: "设置额度 张三",
    });
    // 弹窗预填现值 2000；清空后提交即清除额度。
    fireEvent.change(within(dialog).getByLabelText("月度额度"), {
      target: { value: "" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存额度" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PUT"),
      ).toBe(true),
    );
    const putCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PUT",
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      monthly_quota_credits: null,
    });
    expect(
      await screen.findByText("已清除「张三」的额度限制。"),
    ).toBeInTheDocument();
  });

  // P2-3：「设置密码」改受控弹窗：密码遮蔽输入、6 字符下限就地报错、
  // 提交 PUT 到密码端点（原 window.prompt 明文回显）。
  it("sets the password through the masked dialog with a length floor", async () => {
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "POST") {
        return jsonResponse({ ...subAccount, has_password: true });
      }
      return jsonResponse({ sub_accounts: [subAccount], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "设置密码" }));
    const dialog = await screen.findByRole("dialog", {
      name: "设置密码 张三",
    });
    const input = within(dialog).getByLabelText(
      /新密码（6–128 个字符/,
    ) as HTMLInputElement;
    expect(input.type).toBe("password");

    // 过短密码在弹窗内就地报错，不发请求。
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存密码" }));
    expect(
      await within(dialog).findByText("密码至少 6 个字符。"),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);

    // 合法密码提交成功并关闭弹窗。
    fireEvent.change(input, { target: { value: "new-pw9" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存密码" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
      ).toBe(true),
    );
    const putCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(String(putCall?.[0])).toContain(
      "/api/customer/sub-accounts/sub-1/password",
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      password: "new-pw9",
    });
    expect(
      await screen.findByText("密码已更新；该子账号的旧登录已失效。"),
    ).toBeInTheDocument();
  });

  // 批次1：KPI 行聚合与消费占比条形图（按消费降序、占比为全局百分比）。
  it("renders the quota KPI row and the consumption share bars", async () => {
    const heavy = {
      ...subAccount,
      id: "sub-1",
      display_name: "张三",
      monthly_quota_credits: 5000,
      quota_used_credits: 1800,
      quota_remaining_credits: 3200,
    };
    const light = {
      ...subAccount,
      id: "sub-2",
      username: "sub_2",
      display_name: "李四",
      monthly_quota_credits: null,
      quota_used_credits: 300,
      quota_remaining_credits: null,
    };
    stubFetch(() =>
      jsonResponse({ sub_accounts: [light, heavy], total_count: 2 }),
    );
    render(
      <SubAccountManagementPage
        now={new Date("2026-09-22T04:00:00Z")}
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    // KPI：总数 2、本月总消费 2100、剩余额度总和 3200、无超限。
    const kpis = screen.getByRole("region", { name: "子账号额度概览" });
    expect(within(kpis).getByText("2")).toBeInTheDocument();
    expect(within(kpis).getByText("2100 积分")).toBeInTheDocument();
    expect(within(kpis).getByText("3200 积分")).toBeInTheDocument();
    expect(within(kpis).getByText("暂无额度超限的子账号")).toBeInTheDocument();

    // 条形图按消费降序：张三 1800（86%）在前，李四 300（14%）在后。
    const chart = screen.getByRole("region", { name: "本月消费占比" });
    const names = within(chart).getAllByText(/张三|李四/);
    expect(names[0]).toHaveTextContent("张三");
    expect(names[1]).toHaveTextContent("李四");
    expect(within(chart).getByText("86%")).toBeInTheDocument();
    expect(within(chart).getByText("14%")).toBeInTheDocument();
  });

  // 批次1：80% 邻近上限 → is-warning 三态 + 月末前用尽的预警文案。
  it("marks a near-limit sub-account as warning and forecasts exhaustion", async () => {
    const nearLimit = {
      ...subAccount,
      monthly_quota_credits: 5000,
      quota_used_credits: 4400,
      quota_remaining_credits: 600,
    };
    stubFetch(() =>
      jsonResponse({ sub_accounts: [nearLimit], total_count: 1 }),
    );
    render(
      <SubAccountManagementPage
        now={new Date("2026-09-20T04:00:00Z")}
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    expect(
      screen.getByText("本月已用 4400 / 5000 积分（88%）"),
    ).toBeInTheDocument();
    const bar = screen.getByRole("progressbar");
    expect(bar.className).toContain("is-warning");
    expect(screen.getByText("额度接近上限")).toBeInTheDocument();
    expect(
      screen.getByText("预计 3 天后额度用完，建议提前调整。"),
    ).toBeInTheDocument();
  });

  // 批次1：用尽卡片给出恢复路径徽章与提示（沿用 Phase 3a 的 is-exhausted 标记）。
  it("shows the recovery hint on an exhausted sub-account", async () => {
    const exhausted = {
      ...subAccount,
      monthly_quota_credits: 2000,
      quota_used_credits: 2000,
      quota_remaining_credits: 0,
    };
    stubFetch(() =>
      jsonResponse({ sub_accounts: [exhausted], total_count: 1 }),
    );
    render(
      <SubAccountManagementPage
        now={new Date("2026-09-22T04:00:00Z")}
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    expect(screen.getByText("额度已用尽")).toBeInTheDocument();
    expect(
      screen.getByText("额度已用尽；调高月度额度或等下月 1 日重置后恢复消费。"),
    ).toBeInTheDocument();
  });

  // 批次2：权限摘要行与徽章——受限显示禁用明细，不受限显示「全部开放」。
  it("surfaces the feature-permission state on the card", async () => {
    const restricted = {
      ...subAccount,
      permissions: {
        businesses: ["video", "oral"],
        allow_api_keys: false,
        allow_publish_accounts: false,
      },
    };
    const free = {
      ...subAccount,
      id: "sub-2",
      username: "sub_2",
      display_name: "李四",
      permissions: null,
    };
    stubFetch(() =>
      jsonResponse({ sub_accounts: [restricted, free], total_count: 2 }),
    );
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    expect(screen.getByText("权限受限")).toBeInTheDocument();
    expect(
      screen.getByText("权限：2/12 类业务 · Token 禁用 · 发布账号禁用"),
    ).toBeInTheDocument();
    expect(screen.getByText("权限：全部开放")).toBeInTheDocument();
  });

  // 批次2：管理员角色徽章与「取消管理员」入口（account_type=SUB_ADMIN）。
  it("marks an admin sub-account and offers the demotion entry", async () => {
    const admin = { ...subAccount, account_type: "SUB_ADMIN" as const };
    stubFetch(() => jsonResponse({ sub_accounts: [admin], total_count: 1 }));
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    expect(screen.getByText("管理员")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "取消管理员" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "设为管理员" }),
    ).not.toBeInTheDocument();
  });

  // 批次2：Modal 打开时回显服务端权限，保存经 PUT 三字段提交（规范化序）。
  it("edits the permission matrix in the modal and saves it", async () => {
    const restricted = {
      ...subAccount,
      permissions: {
        businesses: ["oral", "video"],
        allow_api_keys: false,
        allow_publish_accounts: true,
      },
    };
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "PUT") {
        return jsonResponse(restricted);
      }
      return jsonResponse({ sub_accounts: [restricted], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "设置权限" }));
    const dialog = screen.getByRole("dialog", { name: /功能权限 张三/ });

    expect(
      within(dialog).getByRole("checkbox", { name: "视频生成" }),
    ).toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "数字人口播" }),
    ).toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "视频拆解" }),
    ).not.toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", { name: "允许创建 API Token" }),
    ).not.toBeChecked();
    expect(
      within(dialog).getByRole("checkbox", {
        name: "允许使用发布账号（导入 / 扫码）",
      }),
    ).toBeChecked();

    fireEvent.click(within(dialog).getByRole("checkbox", { name: "视频拆解" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存权限" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PUT"),
      ).toBe(true),
    );
    const putCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PUT",
    );
    expect(String(putCall?.[0])).toContain(
      "/api/customer/sub-accounts/sub-1/permissions",
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      businesses: ["video", "oral", "analysis"],
      allow_api_keys: false,
      allow_publish_accounts: true,
    });
    expect(
      await screen.findByText("已更新「张三」的功能权限。"),
    ).toBeInTheDocument();
    // 保存成功后 Modal 关闭，不会滞留遮罩。
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  // 批次2：全开保存 = 恢复默认（后端折叠为删行），前端给出恢复提示。
  it("saves the full grant to restore the default permissions", async () => {
    const elevenKeys = [
      "video",
      "oral",
      "character",
      "first_frame",
      "analysis",
      "rewrite",
      "asr",
      "link_resolution",
      "prompt_optimize",
      "avatar_clone",
      "voice_clone",
    ];
    const restricted = {
      ...subAccount,
      permissions: {
        businesses: elevenKeys,
        allow_api_keys: true,
        allow_publish_accounts: false,
      },
    };
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "PUT") {
        return jsonResponse({ ...subAccount, permissions: null });
      }
      return jsonResponse({ sub_accounts: [restricted], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "设置权限" }));
    const dialog = screen.getByRole("dialog", { name: /功能权限 张三/ });
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "爆款数据" }));
    fireEvent.click(
      within(dialog).getByRole("checkbox", {
        name: "允许使用发布账号（导入 / 扫码）",
      }),
    );
    expect(
      within(dialog).getByText("当前为全部开放：保存后该子账号恢复默认权限。"),
    ).toBeInTheDocument();

    fireEvent.click(within(dialog).getByRole("button", { name: "保存权限" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PUT"),
      ).toBe(true),
    );
    const putCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PUT",
    );
    expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
      businesses: [...elevenKeys, "viral_data"],
      allow_api_keys: true,
      allow_publish_accounts: true,
    });
    expect(
      await screen.findByText("已恢复「张三」的全部权限。"),
    ).toBeInTheDocument();
  });

  // 批次2：保存失败（422 未知业务键）在 Modal 内展示，不关闭。
  it("keeps the modal open with the backend error when saving fails", async () => {
    const restricted = {
      ...subAccount,
      permissions: {
        businesses: ["video"],
        allow_api_keys: true,
        allow_publish_accounts: true,
      },
    };
    stubFetch((_url, init) => {
      if (init?.method === "PUT") {
        return jsonResponse(
          {
            detail: {
              code: "INVALID_BUSINESS_PERMISSIONS",
              message: "未知业务权限键：wat。",
            },
          },
          422,
        );
      }
      return jsonResponse({ sub_accounts: [restricted], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "设置权限" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "保存权限",
      }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "未知业务权限键：wat。",
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  // 批次2：设为管理员（PATCH account_type=SUB_ADMIN），confirm 确认后提交。
  it("promotes a plain sub-account to SUB_ADMIN after confirmation", async () => {
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "PATCH") {
        return jsonResponse({ ...subAccount, account_type: "SUB_ADMIN" });
      }
      return jsonResponse({ sub_accounts: [subAccount], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "设为管理员" }));
    await acknowledgeAndConfirm("设为管理员：「张三」？", "设为管理员");

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
      ).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(String(patchCall?.[0])).toContain(
      "/api/customer/sub-accounts/sub-1",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      account_type: "SUB_ADMIN",
    });
    expect(
      await screen.findByText("已将「张三」设为管理员。"),
    ).toBeInTheDocument();
  });

  // 批次2：取消管理员——拒绝确认不发请求，确认后 PATCH 回 SUB。
  it("demotes an admin only after the confirmation is accepted", async () => {
    const admin = { ...subAccount, account_type: "SUB_ADMIN" as const };
    const fetchMock = stubFetch((_url, init) => {
      if (init?.method === "PATCH") {
        return jsonResponse({ ...subAccount, account_type: "SUB" });
      }
      return jsonResponse({ sub_accounts: [admin], total_count: 1 });
    });
    render(
      <SubAccountManagementPage
        store={storeWithSession(sessionToken)}
        onSessionExpired={vi.fn()}
      />,
    );
    await screen.findByText("张三", { selector: "strong" });

    fireEvent.click(screen.getByRole("button", { name: "取消管理员" }));
    const cancelled = await screen.findByRole("dialog", {
      name: "取消管理员：「张三」？",
    });
    fireEvent.click(within(cancelled).getByRole("button", { name: "取消" }));
    // 取消不落任何写操作。
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "取消管理员" }));
    await acknowledgeAndConfirm("取消管理员：「张三」？", "取消管理员");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
      ).toBe(true),
    );
    const patchCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({
      account_type: "SUB",
    });
    expect(
      await screen.findByText("已取消「张三」的管理员身份。"),
    ).toBeInTheDocument();
  });
});
