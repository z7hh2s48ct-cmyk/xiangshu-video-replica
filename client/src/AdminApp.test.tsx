import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminApp, adminRouteFromHash } from "./AdminApp";
import { getAdminCsrfToken, SESSION_EXPIRED_EVENT } from "./api";
import { getCustomerPricing } from "./api.admin";

const SERVICE_KEY_TEXT = ["service", "key"].join("-");
const MASKED_SERVICE_KEY = ["********", "cret"].join("");
const MASKED_STORAGE_SECRET = ["********", "5678"].join("");
const CSRF_TOKEN_TEXT = ["csrf", "token", "admin"].join("-");

const adminActor = {
  user_id: "admin-1",
  username: "admin",
  display_name: "管理员一号",
  role: "admin",
};

const adminSession = {
  session_id: "session-1",
  expires_at: "2026-08-28T00:00:00+00:00",
  last_activity_at: "2026-08-27T16:00:00+00:00",
  csrf_token: CSRF_TOKEN_TEXT,
  actor: adminActor,
  auth_method: "password",
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

function blobResponse() {
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: new Headers({
      "X-Export-Total": "1",
      "X-Export-Returned": "1",
      "X-Export-Truncated": "false",
    }),
    blob: async () => new Blob(["id\n1"], { type: "text/csv" }),
  });
}

const accountsPage = {
  items: [
    {
      id: "user-1",
      username: "operator-1",
      display_name: "运营一号",
      role: "employee",
      is_active: true,
      available_credits: 18,
      reserved_credits: 2,
      active_token_count: 1,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const ordersPage = {
  items: [
    {
      id: "order-1",
      user_id: "user-1",
      username: "operator-1",
      display_name: "运营一号",
      order_no: "202608190001",
      status: "PENDING",
      amount_fen: 10000,
      credits: 10,
      channel: "alipay",
      provider_trade_no: null,
      created_at: "2026-08-19 10:00:00",
      paid_at: null,
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const transactionsPage = {
  items: [
    {
      id: "tx-1",
      user_id: "user-1",
      username: "operator-1",
      type: "CHARGE",
      available_delta: 10,
      reserved_delta: 0,
      recharge_order_id: "order-1",
      task_id: null,
      billing_round: null,
      created_at: "2026-08-19 10:01:00",
    },
  ],
  total: 1,
  limit: 50,
  offset: 0,
};

const reconciliation = {
  wallet_count: 1,
  wallet_mismatch_count: 0,
  paid_order_without_charge_count: 2,
  charge_without_paid_order_count: 1,
  pending_order_count: 1,
};

const settings = {
  providers: {
    metaso: { provider: "metaso", configured: false, config: {} },
    apilio: { provider: "apilio", configured: false, config: {} },
    cos: {
      provider: "cos",
      configured: true,
      config: {
        access_key_id: "********1234",
        secret_access_key: MASKED_STORAGE_SECRET,
        bucket: "private-materials",
        region: "ap-shanghai",
      },
    },
    deepseek: { provider: "deepseek", configured: false, config: {} },
  },
  runtime: {
    max_generation_count_per_batch: 4,
    max_concurrent_h3_tasks: 2,
    active_storage_provider: "cos",
  },
  billing: {
    internal_base_unit_price_fen: 1000,
    charged_unit_price_fen: 1000,
    oral_unit_price_fen: 1000,
    min_recharge_fen: 10000,
    recharge_step_fen: 1000,
  },
  zpay: {
    provider: "zpay",
    configured: true,
    config: {
      pid: "merchant-1",
      key: "********cret",
      enabled_channels: "alipay,wxpay",
    },
  },
  deployment: {
    gateway_url: "https://zpayz.cn/submit.php",
    notify_url: "https://internal.example/api/payments/zpay/notify",
    return_url: "https://internal.example/api/payments/zpay/return",
  },
};

function installFetch(options?: {
  session?: "valid" | "missing";
  failedTasks?: number;
  generationTotal?: number;
}) {
  const sessionState = options?.session ?? "missing";
  const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
    if (
      url.endsWith("/api/control/admin/session") &&
      (!requestInit?.method || requestInit.method === "GET")
    ) {
      if (sessionState === "valid") {
        return jsonResponse(adminSession);
      }
      return jsonResponse(
        {
          detail: {
            code: "ADMIN_SESSION_INVALID",
            message: "Admin session is missing, revoked or invalid.",
          },
        },
        401,
      );
    }
    if (url.endsWith("/api/control/admin/session/password")) {
      return jsonResponse(adminSession, 201);
    }
    if (url.endsWith("/api/control/admin/session/exchange")) {
      return jsonResponse(adminSession, 201);
    }
    if (
      url.endsWith("/api/control/admin/password") &&
      requestInit?.method === "PUT"
    ) {
      return jsonResponse(undefined, 204);
    }
    if (
      url.endsWith("/api/control/admin/session") &&
      requestInit?.method === "DELETE"
    ) {
      return jsonResponse(undefined, 204);
    }
    if (url.includes("/api/control/accounts?")) {
      return jsonResponse(accountsPage);
    }
    if (url.endsWith("/api/control/dashboard/summary")) {
      return jsonResponse({
        today: {
          generated: 0,
          succeeded: 0,
          failed: 0,
          online_devices: 0,
          active_customers: 0,
          recharge_fen: 0,
        },
        trend: [],
        todos: {
          pending_pairings: 0,
          failed_tasks_7d: options?.failedTasks ?? 0,
          reconciliation_problems: 0,
          expiring_codes_7d: 0,
        },
        device_slots: { bound: 0, total: 0 },
      });
    }
    if (url.includes("/api/control/recharge-orders?")) {
      return jsonResponse(ordersPage);
    }
    if (url.includes("/api/control/wallet-transactions?")) {
      return jsonResponse(transactionsPage);
    }
    if (url.includes("/api/control/customers?")) {
      return jsonResponse({
        items: [],
        total: 0,
        limit: 20,
        offset: 0,
      });
    }
    if (url.includes("/api/control/generation-records/summary")) {
      return jsonResponse({ total: 1, counts: [], failure_reasons: [] });
    }
    if (url.includes("/api/control/generation-records?")) {
      return jsonResponse({
        items: [
          {
            record_id: "first-frame-1",
            record_type: "FIRST_FRAME_IMAGE",
            operation: "GENERATE",
            user_id: "user-1",
            username: "customer-1",
            display_name: "客户一",
            project_id: "project-1",
            project_name: "演示项目",
            status: "SUCCEEDED",
            provider: "apilio",
            model: "gpt-image-2",
            provider_cost: null,
            provider_cost_status: "UNAVAILABLE",
            record_data_status: "VALID",
            charged_credits: 0,
            result_reference: "version-1",
            error_code: null,
            created_at: "2026-09-02T10:00:00Z",
            completed_at: "2026-09-02T10:01:00Z",
          },
        ],
        total: options?.generationTotal ?? 1,
        limit: 50,
        offset: 0,
      });
    }
    if (url.endsWith("/api/control/billing-reconciliation")) {
      return jsonResponse(reconciliation);
    }
    if (
      (url.endsWith("/api/control/settings") ||
        url.endsWith("/api/control/settings/customer-payments")) &&
      !requestInit?.method
    ) {
      return jsonResponse(settings);
    }
    if (url.endsWith("/api/control/settings/customer-payments/zpay")) {
      return jsonResponse(settings.zpay);
    }
    if (
      url.endsWith("/api/control/settings/billing") ||
      url.endsWith("/api/control/settings/customer-payments/billing")
    ) {
      return jsonResponse(settings.billing);
    }
    if (url.endsWith("/api/control/settings/h3-accounts")) {
      return jsonResponse({
        accounts: [],
        total_concurrency: 0,
        managed: false,
      });
    }
    if (url.endsWith("/api/control/settings/providers/apilio")) {
      return jsonResponse({
        provider: "apilio",
        configured: true,
        config: { api_key: MASKED_SERVICE_KEY },
      });
    }
    if (
      url.endsWith("/api/control/settings/providers/metaso/connection-test")
    ) {
      return jsonResponse({
        status: "configured_only",
        provider: "metaso",
        test_kind: "connection",
      });
    }
    if (url.endsWith("/api/control/settings/runtime")) {
      return jsonResponse(settings.runtime);
    }
    if (url.endsWith("/api/control/settings/queue-mode")) {
      if (requestInit?.method === "PATCH") {
        return jsonResponse({ fair_queue_enabled: true });
      }
      return jsonResponse({ fair_queue_enabled: false });
    }
    if (url.endsWith("/api/control/settings/recharge-packages")) {
      // 充值套餐管理：默认无档位（用例只验证页面可渲染）。
      return jsonResponse({ items: [] });
    }
    if (url.endsWith("/api/control/recharge-orders/202608190001/sync")) {
      return jsonResponse({ ...ordersPage.items[0], status: "PAID" });
    }
    if (
      new URL(url).pathname.endsWith("/api/control/recharge-orders.csv") ||
      new URL(url).pathname.endsWith("/api/control/wallet-transactions.csv")
    ) {
      return blobResponse();
    }
    if (url.includes("/api/control/devices?")) {
      return jsonResponse({ items: [], total: 0, limit: 100, offset: 0 });
    }
    if (url.includes("/api/control/customer-sessions/live")) {
      return jsonResponse({ items: [], total: 0, limit: 20, offset: 0 });
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test");
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );
  return fetchMock;
}

async function signInWithPassword() {
  fireEvent.change(await screen.findByLabelText("管理员账号"), {
    target: { value: "admin" },
  });
  fireEvent.change(screen.getByLabelText("管理员密码"), {
    target: { value: "Admin Login Passphrase 2026!" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录后台" }));
  await waitFor(() => {
    expect(
      screen.queryByRole("navigation", { name: "管理端导航" }) ??
        screen.queryByRole("button", { name: "展开导航" }),
    ).toBeTruthy();
  });
}

describe("AdminApp", () => {
  it("restores a recovery session to password setup without loading business pages", async () => {
    const fetchMock = vi.fn(() =>
      jsonResponse({ ...adminSession, auth_method: "exchange" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<AdminApp />);
    expect(await screen.findByLabelText("新管理员密码")).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "总览仪表盘" }),
    ).not.toBeInTheDocument();
    expect(fetchMock.mock.calls).toHaveLength(1);
  });
  beforeEach(() => {
    window.location.hash = "";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    window.location.hash = "";
  });

  it("七个管理模块地址均可恢复且未知地址回总览", () => {
    for (const tab of [
      "overview",
      "analytics",
      "funds",
      "customersMgmt",
      "generationRecords",
      "auditCenter",
      "systemSettings",
    ] as const) {
      expect(adminRouteFromHash(`#admin/${tab}`).tab).toBe(tab);
    }
    expect(adminRouteFromHash("#admin/unknown").tab).toBe("overview");
  });

  it("恢复拆解失败意图时同时带上类型与状态筛选", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/generationRecords?intent=analysisFailures",
    );
    const fetchMock = installFetch({ session: "valid" });
    render(<AdminApp />);

    expect(await screen.findByLabelText("生成类型")).toHaveValue("ANALYSIS");
    expect(screen.getByLabelText("生成状态")).toHaveValue("FAILED");
    expect(screen.getByLabelText("失败阶段")).toHaveValue("");
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("record_type=ANALYSIS"),
      ),
    ).toBe(true);
  });

  it("刷新和前进后退恢复模块及失败记录筛选意图", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/generationRecords?intent=failedGenerationRecords",
    );
    const fetchMock = installFetch({ session: "valid" });
    render(<AdminApp />);

    expect(
      await screen.findByRole("heading", { level: 1, name: "用户生成记录" }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("生成状态")).toHaveValue("FAILED");
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("status=FAILED"),
      ),
    ).toBe(true);

    window.history.pushState(null, "", "/admin#admin/funds");
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(
      await screen.findByRole("heading", { level: 1, name: "资金流水" }),
    ).toBeInTheDocument();
  });

  it("同一生成记录模块切换意图时同步清空筛选和分页", async () => {
    window.history.replaceState(
      null,
      "",
      "/admin#admin/generationRecords?intent=failedGenerationRecords",
    );
    const fetchMock = installFetch({
      session: "valid",
      generationTotal: 101,
    });
    render(<AdminApp />);

    expect(await screen.findByLabelText("生成状态")).toHaveValue("FAILED");
    fireEvent.change(screen.getByLabelText("生成账号"), {
      target: { value: "customer-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("offset=50&username=customer-1&status=FAILED"),
        ),
      ).toBe(true),
    );

    fireEvent.click(screen.getByRole("button", { name: "生成记录" }));
    expect(window.location.hash).toBe("#admin/generationRecords");
    expect(screen.getByLabelText("生成账号")).toHaveValue("");
    expect(screen.getByLabelText("生成状态")).toHaveValue("");
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith(
            "/api/control/generation-records?limit=50&offset=0",
          ),
        ),
      ).toBe(true),
    );

    await act(async () => window.history.back());
    await waitFor(() => expect(window.location.hash).toContain("intent="));
    expect(screen.getByLabelText("生成状态")).toHaveValue("FAILED");
    await act(async () => window.history.forward());
    await waitFor(() =>
      expect(window.location.hash).toBe("#admin/generationRecords"),
    );
    expect(screen.getByLabelText("生成状态")).toHaveValue("");
  });

  it("starts at the account-password gate before exposing control navigation", async () => {
    const fetchMock = installFetch();

    render(<AdminApp />);

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(screen.getByLabelText("管理员密码")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "管理端导航" })).toBeNull();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/control/accounts?"),
      ),
    ).toBe(false);
  });

  it("keeps order operations to sync and CSV export", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("button", { name: "资金流水" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值订单" }));

    expect(
      await screen.findByText(
        (_, element) => element?.textContent === "待支付订单 1",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) => element?.textContent === "已支付未入账 2",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        (_, element) => element?.textContent === "入账但订单未支付 1",
      ),
    ).toBeInTheDocument();

    // 查单同步先经"原因必填"确认（A4 写契约），再发请求。
    fireEvent.click(await screen.findByRole("button", { name: "查单同步" }));
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "客服反馈未到账" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认查单" }));
    await screen.findByText("订单 202608190001 状态已同步。");

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "导出充值订单 CSV" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "导出充值订单 CSV" }));
    expect(
      screen.queryByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeNull();

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith(
            "/api/control/recharge-orders/202608190001/sync",
          ),
        ),
      ).toBe(true),
    );
    expect(screen.queryByRole("button", { name: /补单|改余额/ })).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "额度流水" }));
    const walletExport = await screen.findByRole("button", {
      name: "导出账务流水 CSV",
    });
    await waitFor(() => expect(walletExport).toBeEnabled());
    fireEvent.click(walletExport);
    await waitFor(() => {
      for (const pathname of [
        "/api/control/recharge-orders.csv",
        "/api/control/wallet-transactions.csv",
      ]) {
        expect(
          fetchMock.mock.calls.some(
            ([url]) => new URL(String(url)).pathname === pathname,
          ),
        ).toBe(true);
      }
    });
  });

  it("saves ZPay and price settings while deployment URLs stay server-owned", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("button", { name: "系统设置" }));
    fireEvent.click(screen.getByRole("tab", { name: "支付与价格" }));
    expect(await screen.findByDisplayValue("merchant-1")).toBeInTheDocument();
    expect(screen.getByText("********cret")).toBeInTheDocument();
    expect(screen.queryByLabelText("网关地址")).toBeNull();
    expect(screen.queryByLabelText("异步回调地址")).toBeNull();
    expect(screen.queryByLabelText("同步返回地址")).toBeNull();
    expect(
      screen.getByText("支付接口地址由系统自动配置，无需填写。"),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("ZPay 商户 PID"), {
      target: { value: "merchant-2" },
    });
    fireEvent.change(screen.getByLabelText("新商户密钥"), {
      target: { value: "" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存 ZPay 设置" }));

    await screen.findByRole("dialog", { name: "保存 ZPay 支付设置" });
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认保存 ZPay 设置" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith(
              "/api/control/settings/customer-payments/zpay",
            ) && options?.method === "PATCH",
        ),
      ).toBe(true);
    });
    const zpayCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/control/settings/customer-payments/zpay") &&
        options?.method === "PATCH",
    );
    const zpayRequest = zpayCall?.[1] as RequestInit | undefined;
    expect(zpayRequest).toBeTruthy();
    expect(zpayCall?.[1]?.body).toBe(
      JSON.stringify({
        pid: "merchant-2",
        key: "",
        enabled_channels: ["alipay", "wxpay"],
        confirm: true,
        reason: "保存 ZPay 设置",
      }),
    );
    expect(
      new Headers(zpayRequest?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
    expect(String(zpayCall?.[1]?.body)).not.toContain("gateway_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("notify_url");
    expect(String(zpayCall?.[1]?.body)).not.toContain("return_url");
  });

  it("configures generic services through the production control plane", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);
    await signInWithPassword();

    fireEvent.click(screen.getByRole("button", { name: "系统设置" }));
    fireEvent.click(screen.getByRole("tab", { name: "服务配置" }));
    expect(
      await screen.findByRole("heading", { name: "视频生成 · 多账号" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "腾讯云存储" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("图像模型 API Key"), {
      target: { value: SERVICE_KEY_TEXT },
    });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" })[0]);

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          ([url, options]) =>
            String(url).endsWith("/api/control/settings/providers/apilio") &&
            options?.method === "PUT",
        ),
      ).toBe(true),
    );
    const saveCall = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/control/settings/providers/apilio") &&
        options?.method === "PUT",
    );
    const saveRequest = saveCall?.[1] as RequestInit | undefined;
    expect(saveRequest).toBeTruthy();
    expect(saveCall?.[1]?.body).toBe(
      JSON.stringify({
        // analysis_model 是可选项，留空表示回落到服务端默认模型；管理端与
        // 客户端共用 SettingsPanel 的表单定义，因此两处都会带上这个键。
        config: {
          api_key: SERVICE_KEY_TEXT,
          analysis_api_key: "",
          analysis_model: "",
        },
        confirm: true,
        reason: "更新 apilio 服务配置",
      }),
    );
    expect(
      new Headers(saveRequest?.headers).get("Idempotency-Key"),
    ).toBeTruthy();
    expect(screen.queryByText(/metaso|minimax|cos/i)).toBeNull();
  });

  it("opens customer management only after account-password login", async () => {
    installFetch();

    render(<AdminApp />);
    await signInWithPassword();
    fireEvent.click(screen.getByRole("button", { name: "客户管理" }));
    expect(
      screen.queryByRole("tab", { name: "激活码" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "客户管理" }),
    ).toBeInTheDocument();
  });

  it("opens the unified image and video generation records", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);
    fireEvent.click(await screen.findByRole("button", { name: "生成记录" }));

    expect(
      await screen.findByRole("heading", { level: 1, name: "用户生成记录" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("人物置换首帧")).toBeInTheDocument();
    expect(screen.getByText("上游未回传")).toBeInTheDocument();
  });

  it("opens failed generation records from the overview todo", async () => {
    const fetchMock = installFetch({ session: "valid", failedTasks: 1 });

    render(<AdminApp />);
    const failedTasks = await screen.findByText("失败任务待处理");
    fireEvent.click(
      failedTasks.closest("li")?.querySelector("button") as HTMLButtonElement,
    );

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes(
            "/api/control/generation-records?limit=50&offset=0&status=FAILED",
          ),
        ),
      ).toBe(true);
    });
    expect(screen.getByLabelText("生成状态")).toHaveValue("FAILED");
  });

  it("restores a writable session after refresh without another login", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("管理员密码")).toBeNull();
    expect(screen.getAllByText("管理员一号").length).toBeGreaterThan(0);
  });

  it("opens economics from the overview group tabs while retaining navigation context", async () => {
    installFetch({ session: "valid" });
    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });
    const groupTabs = screen.getByRole("tablist", { name: "运营概览快捷导航" });
    expect(groupTabs).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "总览仪表盘" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    fireEvent.click(screen.getByRole("tab", { name: "经营分析" }));
    expect(screen.getByRole("tab", { name: "成本明细" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "经营分析" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("renders the merged seven-item navigation with per-page tabs", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);
    await screen.findByRole("navigation", { name: "管理端导航" });

    for (const name of [
      "总览仪表盘",
      "经营分析",
      "资金流水",
      "客户管理",
      "生成记录",
      "审计中心",
      "系统设置",
    ]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }

    fireEvent.click(screen.getByRole("button", { name: "资金流水" }));
    expect(screen.getByRole("tab", { name: "充值订单" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "额度流水" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "经营分析" }));
    expect(screen.getByRole("tab", { name: "利润总览" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "成本明细" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "客户管理" }));
    expect(
      screen.queryByRole("tab", { name: "在线会话" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "激活码" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("当前模块")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "设备与会话" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "审计中心" }));
    expect(screen.getByRole("tab", { name: "审计日志" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "调账记录" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "系统设置" }));
    expect(screen.getByRole("tab", { name: "支付与价格" })).toBeInTheDocument();
    expect(
      screen.getByRole("tab", { name: "API 端点与价格" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "服务配置" })).toBeInTheDocument();
  });

  it("returns to the login gate and clears admin session state when a control 401 emits the shared expiry event", async () => {
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
    expect(getAdminCsrfToken()).toBe(CSRF_TOKEN_TEXT);

    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({ detail: "会话过期" }, 401)),
    );
    await act(async () => {
      await expect(getCustomerPricing()).rejects.toMatchObject({ status: 401 });
    });

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(screen.getByLabelText("管理员密码")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "管理端导航" })).toBeNull();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "会话已失效，请重新登录。",
    );
    expect(getAdminCsrfToken()).toBeNull();
  });

  it("removes the shared expiry listener when the admin shell unmounts", async () => {
    installFetch({ session: "valid" });
    const addSpy = vi.spyOn(window, "addEventListener");
    const removeSpy = vi.spyOn(window, "removeEventListener");

    const { unmount } = render(<AdminApp />);

    await screen.findByRole("navigation", { name: "管理端导航" });
    const sessionListener = addSpy.mock.calls.find(
      ([name]) => name === SESSION_EXPIRED_EVENT,
    )?.[1];
    expect(sessionListener).toBeTypeOf("function");

    unmount();

    expect(removeSpy).toHaveBeenCalledWith(
      SESSION_EXPIRED_EVENT,
      sessionListener,
    );
  });

  it("uses a one-time credential only to set or recover the password", async () => {
    const fetchMock = installFetch();
    render(<AdminApp />);

    fireEvent.click(
      await screen.findByRole("button", { name: "首次设置或找回密码" }),
    );
    fireEvent.change(screen.getByLabelText("一次性恢复凭据"), {
      target: { value: "ASX1.body.signature" },
    });
    fireEvent.click(screen.getByRole("button", { name: "验证恢复凭据" }));
    expect(await screen.findByLabelText("新管理员密码")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("新管理员密码"), {
      target: { value: "Recovered Admin Passphrase 2026!" },
    });
    fireEvent.change(screen.getByLabelText("确认新管理员密码"), {
      target: { value: "Recovered Admin Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存新密码" }));

    expect(await screen.findByLabelText("管理员账号")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) =>
          String(url).endsWith("/api/control/admin/password") &&
          options?.method === "PUT",
      ),
    ).toBe(true);
  });

  it("登录门提供密码可见性切换并回写状态", async () => {
    installFetch();

    render(<AdminApp />);

    expect(await screen.findByLabelText("管理员密码")).toHaveAttribute(
      "type",
      "password",
    );

    fireEvent.click(screen.getByRole("button", { name: "显示密码" }));
    expect(screen.getByLabelText("管理员密码")).toHaveAttribute("type", "text");
    const hideToggle = screen.getByRole("button", { name: "隐藏密码" });
    expect(hideToggle).toHaveAttribute("aria-pressed", "true");

    fireEvent.click(hideToggle);
    expect(screen.getByLabelText("管理员密码")).toHaveAttribute(
      "type",
      "password",
    );
    expect(screen.getByRole("button", { name: "显示密码" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("登录提交期间禁用按钮并阻止重复提交以保护登录限流预算", async () => {
    // 后端按 IP+账号双维度对每次登录尝试计数（admin_auth_routes
    // _spend_admin_password_budget）：前端双击等于白烧两份预算，可能把管理员
    // 锁在门外。提交期间必须禁用按钮，直到当前请求落地。
    let resolveLogin:
      | ((response: {
          ok: boolean;
          status: number;
          json: () => Promise<unknown>;
        }) => void)
      | undefined;
    const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
      if (
        url.endsWith("/api/control/admin/session") &&
        (!requestInit?.method || requestInit.method === "GET")
      ) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      if (url.endsWith("/api/control/admin/session/password")) {
        return new Promise((resolve) => {
          resolveLogin = resolve;
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AdminApp />);

    fireEvent.change(await screen.findByLabelText("管理员账号"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "Admin Login Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录后台" }));

    const pendingButton = await screen.findByRole("button", {
      name: "正在登录…",
    });
    expect(pendingButton).toBeDisabled();
    fireEvent.click(pendingButton);
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/api/control/admin/session/password"),
      ).length,
    ).toBe(1);

    resolveLogin?.({
      ok: true,
      status: 201,
      json: async () => adminSession,
    });
    expect(
      await screen.findByRole("navigation", { name: "管理端导航" }),
    ).toBeInTheDocument();
  });

  it("登录限流错误呈现中文提示而非服务端英文原文", async () => {
    const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
      if (
        url.endsWith("/api/control/admin/session") &&
        (!requestInit?.method || requestInit.method === "GET")
      ) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      if (url.endsWith("/api/control/admin/session/password")) {
        return jsonResponse(
          {
            detail: {
              code: "RATE_LIMITED",
              message:
                "Too many administrator sign-in attempts. Try again later.",
            },
          },
          429,
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AdminApp />);

    fireEvent.change(await screen.findByLabelText("管理员账号"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "Admin Login Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录后台" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("登录尝试过于频繁，请稍后再试。");
    expect(alert.textContent).not.toContain("Too many");
  });

  it("会话上下文变化错误呈现中文提示", async () => {
    const fetchMock = vi.fn((url: string, requestInit?: RequestInit) => {
      if (
        url.endsWith("/api/control/admin/session") &&
        (!requestInit?.method || requestInit.method === "GET")
      ) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_INVALID",
              message: "Admin session is missing, revoked or invalid.",
            },
          },
          401,
        );
      }
      if (url.endsWith("/api/control/admin/session/password")) {
        return jsonResponse(
          {
            detail: {
              code: "ADMIN_SESSION_CONTEXT_CHANGED",
              message:
                "Admin session network or browser context changed; sign in again.",
            },
          },
          401,
        );
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AdminApp />);

    fireEvent.change(await screen.findByLabelText("管理员账号"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("管理员密码"), {
      target: { value: "Admin Login Passphrase 2026!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录后台" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("检测到浏览器环境变化，请重新登录。");
    expect(alert.textContent).not.toContain("context changed");
  });

  it("恢复凭据表单提供格式提示并对粘贴空白给出忽略反馈", async () => {
    installFetch();

    render(<AdminApp />);

    fireEvent.click(
      await screen.findByRole("button", { name: "首次设置或找回密码" }),
    );

    expect(screen.getByText("凭据以 ASX1. 开头。")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("一次性恢复凭据"), {
      target: { value: "  ASX1.body.signature  " },
    });

    expect(screen.getByText("已自动忽略首尾空白。")).toBeInTheDocument();
  });

  it("keeps key order actions available on a narrow viewport", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 375,
    });
    installFetch();

    render(<AdminApp />);
    await signInWithPassword();
    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));
    fireEvent.click(screen.getByRole("button", { name: "资金流水" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值订单" }));

    expect(
      await screen.findByRole("button", { name: "查单同步" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "导出充值订单 CSV" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "额度流水" }));
    expect(
      await screen.findByRole("button", { name: "导出账务流水 CSV" }),
    ).toBeInTheDocument();
  });

  it("groups admin navigation and lets compact layouts collapse and reopen it", async () => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 390,
    });
    installFetch({ session: "valid" });

    render(<AdminApp />);

    expect(
      await screen.findByRole("button", { name: "展开导航" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: "客户管理" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "展开导航" }));

    expect(screen.getAllByText("运营概览").length).toBeGreaterThan(0);
    expect(screen.getAllByText("客户运营").length).toBeGreaterThan(0);
    expect(screen.getAllByText("系统治理").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("button", { name: "客户管理" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "关闭导航" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    fireEvent.click(screen.getByRole("button", { name: "客户管理" }));

    expect(
      await screen.findByRole("heading", { name: "客户管理" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "展开导航" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });
});
