import { webcrypto } from "node:crypto";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CUSTOMER_SESSION_REPLACED_EVENT } from "./api";
import { RootApp } from "./RootApp";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(),
    json: async () => payload,
  });
}

// The fake token fixtures live in named constants so the repo's secret
// scan (which flags `token:` followed by a quoted literal) stays quiet —
// same posture as customerApi.test.ts.
const deviceTokenText = "device-token-1";
const sessionTokenText = "session-token-1";

const customerActivationBody = {
  username: "user-1",
  display_name: "user-1",
  session_id: "session-1",
  user_id: "user-1",
  device_id: "device-1",
  device_token: deviceTokenText,
  session_token: sessionTokenText,
  session_epoch: 1,
  session_lease_expires_at: new Date(Date.now() + 3600_000).toISOString(),
  request_id: "req-1",
};

/** Fetch stub for the customer lane: the activation succeeds and the
 * workspace shell's internal-lane probes (health, projects) resolve benignly
 * so the workspace can mount — the customer business APIs arrive in later
 * tasks (T34+), not T29. The device list resolves to an empty two-slot
 * contract so the T31 device view can mount. */
function stubCustomerWorkspaceFetch() {
  return vi.fn((url: string) => {
    if (url.endsWith("/api/customer/login")) {
      return jsonResponse(customerActivationBody);
    }
    if (url.endsWith("/api/customer/activate")) {
      return jsonResponse(customerActivationBody, 201);
    }
    if (url.endsWith("/api/customer/devices")) {
      return jsonResponse({
        slots: [
          { slot_no: 1, device: null },
          { slot_no: 2, device: null },
        ],
        history: [],
        pending_pairings: [],
      });
    }
    if (url.endsWith("/health")) {
      return jsonResponse({ status: "ok", service: "video-replica-api" });
    }
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
    return jsonResponse([]);
  });
}

async function loginThroughAccountForm() {
  await screen.findByRole("heading", { name: "工作台" });
  fireEvent.click(screen.getByRole("button", { name: "用户档案" }));
  await screen.findByRole("heading", { name: "登录账号" });
  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "user-1" },
  });
  fireEvent.change(screen.getByLabelText("密码"), {
    target: { value: "test-6" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  await screen.findByRole("button", { name: "用户档案，user-1" });
}

describe("RootApp", () => {
  beforeEach(() => {
    vi.stubGlobal("crypto", webcrypto);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
    window.history.replaceState(null, "", "/");
  });

  // CW-019: /admin 由独立管理制品（client/dist-admin）提供，客户入口不再认识
  // 该路径。浏览器命中客户 index.html 的 /admin 或 /admin/* 时降级到客户壳
  // （激活屏），不得渲染任何管理内容——这是"客户所有 chunk 不得含内部/管理
  // 入口"验收底线在渲染层的直接对应。管理代码排除同时由
  // scripts/verify_customer_bundle.mjs（产物层）与 entryContract.test.ts
  // （源码层）双层断言。
  it.each(["/admin", "/admin/", "/admin/funds"])(
    "does not render any management content when the browser hits %s on the customer entry (admin is a separate build artifact since CW-019)",
    async (path) => {
      const fetchMock = stubCustomerWorkspaceFetch();
      vi.stubGlobal("fetch", fetchMock);

      render(<RootApp path={path} />);

      // 客户壳兜底：落到激活屏，不是管理后台。
      expect(
        await screen.findByRole("heading", { name: "工作台" }),
      ).toBeInTheDocument();
      // 管理标识文案不得出现（AdminApp 的 h1 与其内部 TabBar 文案）。
      expect(
        screen.queryByRole("heading", { name: "运营管理后台" }),
      ).toBeNull();
      expect(screen.queryByText("激活码批次")).toBeNull();
      expect(screen.queryByText("审计中心")).toBeNull();
      expect(screen.queryByText("强制下线")).toBeNull();
      // 客户入口不发起管理域调用（/api/control/*）。
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("/api/control/"),
        ),
      ).toBe(false);
    },
  );

  // CW-013 收敛正式客户入口 + 内部兜底删除：普通浏览器根路径不再进入内部 App，
  // 而是落到唯一的客户状态机；内部访问令牌壳在根入口结构上不可达。
  it("converges the browser root path to the customer state machine (internal fallback deleted)", async () => {
    const fetchMock = stubCustomerWorkspaceFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(<RootApp path="/" />);

    expect(
      await screen.findByRole("heading", { name: "工作台" }),
    ).toBeInTheDocument();
    // 内部兜底删除：根路径既不出现内部访问令牌输入，也不是管理后台。
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
    expect(screen.queryByRole("heading", { name: "运营管理后台" })).toBeNull();
    // 身份隔离：客户入口从不发起内部身份探针 /api/auth/me。
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/api/auth/me"),
      ),
    ).toBe(false);
  });

  it("keeps the welcome logo bounded and consistent after login", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());
    render(<RootApp path="/" />);
    await screen.findByRole("heading", { name: "工作台" });

    const welcomeLogo = screen.getByRole("img", { name: "众墅之家" });
    // 品牌图形回退为 src/assets/brand/ 模块导入：609 字节的 SVG 低于 4KB
    // 内联阈值，dev/test 与生产构建都解析成 data URI；若将来超过阈值则
    // 变为带哈希的资产文件名。两种形态都接受。
    expect(welcomeLogo.getAttribute("src")).toMatch(
      /^data:image\/svg\+xml|zhongshu-logo-mark\.svg$/,
    );
    expect(welcomeLogo).toHaveAttribute("width", "50.4");
    expect(welcomeLogo).toHaveAttribute("height", "43.2");
    expect(screen.getByText("众墅之家")).toBeVisible();
    expect(screen.getByText("AI 即创")).toBeVisible();

    await loginThroughAccountForm();
    const workspaceLogo = screen.getByRole("img", { name: "众墅之家" });
    expect(workspaceLogo.outerHTML).toBe(welcomeLogo.outerHTML);
  });

  // 未认证的客户入口不得读写私有业务数据（V3 行 257/258）。
  it("issues no internal identity or private business call before authentication", async () => {
    const fetchMock = stubCustomerWorkspaceFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(<RootApp path="/" />);
    await screen.findByRole("heading", { name: "工作台" });

    const called = fetchMock.mock.calls.map(([url]) => String(url));
    expect(called.some((url) => url.endsWith("/api/auth/me"))).toBe(false);
    // 激活前不触达任何私有业务接口：唯一允许的 pre-auth 端点是激活/配对/登录，
    // 而激活屏挂载时它们都尚未被调用（浏览器无凭据 → 零 fetch）。收窄白名单，
    // 使 /api/customer/wallet|profile|devices 这类登录后私有接口在激活前被调用即失败。
    const preAuthAllowed = [
      "/api/customer/browser-session",
      "/api/customer/activate",
      "/api/customer/devices/enroll",
      "/api/customer/sessions/login",
    ];
    expect(
      called.some(
        (url) =>
          url.includes("/api/") &&
          !preAuthAllowed.some((endpoint) => url.endsWith(endpoint)),
      ),
    ).toBe(false);
  });

  // 历史内部 hash 深链接（如收藏夹里的 /#characters）不得借此重新进入内部 App。
  it("does not let a historical internal hash at the root re-enter the internal App", async () => {
    window.history.replaceState(null, "", "/#characters");
    const fetchMock = stubCustomerWorkspaceFetch();
    vi.stubGlobal("fetch", fetchMock);

    render(<RootApp />);

    expect(
      await screen.findByRole("heading", { name: "工作台" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith("/api/auth/me"),
      ),
    ).toBe(false);
  });

  // 刷新/深链接：全新挂载时 RootApp 从真实 window.location 读取路径，客户路径留在客户域。
  it.each(["/", "/customer"])(
    "re-reads window.location on a fresh mount so refresh/deep-link to %s stays in the customer lane",
    async (path) => {
      window.history.replaceState(null, "", path);
      const fetchMock = stubCustomerWorkspaceFetch();
      vi.stubGlobal("fetch", fetchMock);

      render(<RootApp />);

      expect(
        await screen.findByRole("heading", { name: "工作台" }),
      ).toBeInTheDocument();
      expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
      // 与其他身份隔离用例同等强度：深链接/刷新挂载也不得触发内部身份探针。
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith("/api/auth/me"),
        ),
      ).toBe(false);
    },
  );

  // 后退到根路径是一次全新挂载：仍落客户状态机，不回落到内部 App。
  // （RootApp 不订阅 popstate；跨路径后退在真实浏览器里是硬导航/整页重载，
  //  故用 unmount + remount 精确模拟，而非依赖软路由事件。）
  it("re-enters the customer lane (not the internal App) when navigating back to the root", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());
    window.history.replaceState(null, "", "/customer");
    const first = render(<RootApp />);
    await screen.findByRole("heading", { name: "工作台" });
    first.unmount();

    window.history.replaceState(null, "", "/");
    render(<RootApp />);

    expect(
      await screen.findByRole("heading", { name: "工作台" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
  });

  // 错误/未匹配路由不绕过身份验证：一律收敛到客户状态机（既非内部 App，也非管理后台）。
  it.each(["/some/unknown/route", "/internal", "/login"])(
    "routes the unmatched path %s to the customer state machine without bypassing authentication",
    async (path) => {
      const fetchMock = stubCustomerWorkspaceFetch();
      vi.stubGlobal("fetch", fetchMock);

      render(<RootApp path={path} />);

      expect(
        await screen.findByRole("heading", { name: "工作台" }),
      ).toBeInTheDocument();
      expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
      expect(
        screen.queryByRole("heading", { name: "运营管理后台" }),
      ).toBeNull();
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith("/api/auth/me"),
        ),
      ).toBe(false);
    },
  );

  it("routes the Tauri desktop root path into the customer lane", async () => {
    Object.defineProperty(window, "__TAURI_INTERNALS__", {
      configurable: true,
      value: {
        invoke: vi.fn(async (command: string) => {
          if (command === "customer_load_credentials") {
            return null;
          }
          throw new Error(`unexpected Tauri command: ${command}`);
        }),
      },
    });
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/" />);

    expect(
      await screen.findByRole("heading", { name: "工作台" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
  });

  // CW-013 + CW-019: Tauri 桌面无 admin 通道——/admin* 在 Tauri 运行时也收敛到
  // 客户状态机。CW-019 拆包后浏览器端同样不再认识 /admin（管理端走独立制品），
  // 但本用例继续固化"桌面客户构建永不拉起管理后台"这条产品红线：即便未来有人
  // 试图在 RootApp 里重新加回 admin 分支，Tauri 运行时也必须保持客户壳。
  it.each(["/admin", "/admin/funds"])(
    "keeps the Tauri desktop on the customer lane even for the admin path %s (no admin lane in the desktop build)",
    async (path) => {
      Object.defineProperty(window, "__TAURI_INTERNALS__", {
        configurable: true,
        value: {
          invoke: vi.fn(async (command: string) => {
            if (command === "customer_load_credentials") {
              return null;
            }
            throw new Error(`unexpected Tauri command: ${command}`);
          }),
        },
      });
      vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

      render(<RootApp path={path} />);

      expect(
        await screen.findByRole("heading", { name: "工作台" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("heading", { name: "运营管理后台" }),
      ).toBeNull();
      expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
    },
  );
  it("routes /customer to the login screen without any internal-token field", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/customer" />);

    expect(
      await screen.findByRole("heading", { name: "工作台" }),
    ).toBeInTheDocument();
    // FE-02 No-Go: the internal access-token input must never be the
    // customer's entry — the customer lane has its own activation flow.
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
  });

  it("logs in with a password and lands in the workspace under the customer identity", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/customer" />);

    await loginThroughAccountForm();

    expect(
      await screen.findByRole("heading", {
        name: "粘贴一条爆款乡墅视频链接，快速生成它的原创视频",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("navigation", { name: "主要导航" }),
    ).toBeInTheDocument();
    // The compact account entry keeps identity details in the profile page.
    // The workspace stub does not serve /api/customer/wallet pricing, so the
    // wallet summary settles to the error label instead of a credit count.
    expect(
      await screen.findByRole("button", { name: "用户档案，user-1" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("内部访问令牌（云端模式）")).toBeNull();
  });

  it("logs out to the public workbench and requires account login again", async () => {
    const workspaceFetch = stubCustomerWorkspaceFetch();
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (url.endsWith("/api/customer/profile")) {
        return jsonResponse({
          user_id: "user-1",
          username: "user-1",
          display_name: "客户一号",
          joined_at: "2026-08-01T00:00:00Z",
          activation_code_masked: "XS04-ABCD••••WXYZ",
          activation_status: "ACTIVE",
          activated_at: "2026-08-02T00:00:00Z",
          device_slots_used: 1,
          device_slots_total: 2,
        });
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        expect(init?.method).toBe("POST");
        return jsonResponse(undefined, 204);
      }
      return workspaceFetch(url);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RootApp path="/customer" />);
    await loginThroughAccountForm();
    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户中心" });
    fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));

    expect(
      await screen.findByRole("heading", { name: "工作台" }),
    ).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([url]) =>
        String(url).endsWith("/api/customer/sessions/logout"),
      ),
    ).toHaveLength(1);
  });

  it("ignores a delayed profile 401 from the session that already logged out", async () => {
    let resolveOldProfile:
      | ((value: Awaited<ReturnType<typeof jsonResponse>>) => void)
      | undefined;
    const oldProfile = new Promise<Awaited<ReturnType<typeof jsonResponse>>>(
      (resolve) => {
        resolveOldProfile = resolve;
      },
    );
    let profileCalls = 0;
    const workspaceFetch = stubCustomerWorkspaceFetch();
    const reloginBody = {
      user_id: "user-1",
      device_id: "device-1",
      session_id: "session-2",
      session_token: sessionTokenText,
      session_epoch: 2,
      session_lease_expires_at: new Date(Date.now() + 3600_000).toISOString(),
      request_id: "req-relogin",
    };
    const fetchMock = vi.fn((url: string) => {
      if (url.endsWith("/api/customer/profile")) {
        profileCalls += 1;
        return profileCalls === 1
          ? oldProfile
          : jsonResponse({
              user_id: "user-1",
              username: "user-1",
              display_name: "新会话客户",
              joined_at: "2026-08-01T00:00:00Z",
              activation_code_masked: "XS04-ABCD••••WXYZ",
              activation_status: "ACTIVE",
              activated_at: "2026-08-02T00:00:00Z",
              device_slots_used: 1,
              device_slots_total: 2,
            });
      }
      if (url.endsWith("/api/customer/sessions/logout")) {
        return jsonResponse(undefined, 204);
      }
      if (url.endsWith("/api/customer/sessions/login")) {
        return jsonResponse(reloginBody, 201);
      }
      return workspaceFetch(url);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<RootApp path="/customer" />);
    await loginThroughAccountForm();
    fireEvent.click(await screen.findByRole("button", { name: /^用户档案$/ }));
    await screen.findByRole("heading", { name: "用户中心" });
    fireEvent.click(await screen.findByRole("button", { name: "退出登录" }));
    await loginThroughAccountForm();
    await waitFor(() => {
      expect(profileCalls).toBe(2);
      expect(
        screen.getByRole("navigation", { name: "主要导航" }),
      ).toBeInTheDocument();
    });

    await act(async () => {
      resolveOldProfile?.(
        await jsonResponse(
          {
            detail: {
              code: "SESSION_EXPIRED",
              message: "旧会话已过期",
            },
          },
          401,
        ),
      );
    });

    await waitFor(() => {
      expect(
        screen.getByRole("navigation", { name: "主要导航" }),
      ).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "登录已过期" })).toBeNull();
    });
  });

  it("shows the displaced-session terminal screen when replaced mid-session (§4.2)", async () => {
    vi.stubGlobal("fetch", stubCustomerWorkspaceFetch());

    render(<RootApp path="/customer" />);
    await loginThroughAccountForm();
    await screen.findByRole("heading", {
      name: "粘贴一条爆款乡墅视频链接，快速生成它的原创视频",
    });

    window.dispatchEvent(new Event(CUSTOMER_SESSION_REPLACED_EVENT));

    expect(
      await screen.findByRole("heading", { name: "本设备已下线" }),
    ).toBeInTheDocument();
    // §4.2 red line: a displaced session must be reported as exactly that —
    // never as a balance, network, or generic service failure.
    expect(screen.getByText(/本次登录凭据已失效/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新登录" }),
    ).toBeInTheDocument();
  });
});
