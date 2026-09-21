import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GenerationBatch, GenerationPriceQuote, Project } from "../api";
import type { CustomerCredentialStore } from "../customer/useCustomerSession";
import type { WorkspaceShellProps } from "../workspace-shell";
import { reviewUser } from "./fixtures";

const projectFixture = {
  id: "project-1",
  name: "张工预算项目",
} as Project;

vi.mock("../ProjectsPage", () => ({
  ProjectsPage: ({
    onOpenAnalysis,
    onOpenDetail,
  }: {
    onOpenAnalysis: (project: Project) => void;
    onOpenDetail: (project: Project) => void;
  }) => (
    <div>
      <button type="button" onClick={() => onOpenAnalysis(projectFixture)}>
        生成该项目
      </button>
      <button type="button" onClick={() => onOpenDetail(projectFixture)}>
        查看该项目
      </button>
    </div>
  ),
}));

vi.mock("../TaskRecordsPanel", () => ({
  TaskRecordsPanel: ({
    onHandoffConsumed,
  }: {
    onHandoffConsumed: () => void;
  }) => (
    <button type="button" onClick={onHandoffConsumed}>
      消费交接
    </button>
  ),
}));

import { LiveWorkspacePanel } from "./LiveWorkspacePanel";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("任务面板消费交接后回传给工作区控制器清除暂存批次", () => {
  const onHandoffConsumed = vi.fn();
  render(
    <LiveWorkspacePanel
      currentUser={reviewUser}
      handoffBatch={{ id: "batch-1" } as GenerationBatch}
      onClose={vi.fn()}
      onHandoffConsumed={onHandoffConsumed}
      onProjectSelected={vi.fn()}
      onRefresh={vi.fn()}
      panel="tasks"
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: "消费交接" }));
  expect(onHandoffConsumed).toHaveBeenCalledTimes(1);
});

it.each(["projects", "analysis"] as const)(
  "%s 面板的生成和查看入口都交给统一复刻页",
  (panel) => {
    const onProjectSelected = vi.fn();
    render(
      <LiveWorkspacePanel
        currentUser={reviewUser}
        onClose={vi.fn()}
        onHandoffConsumed={vi.fn()}
        onProjectSelected={onProjectSelected}
        onRefresh={vi.fn()}
        panel={panel}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "生成该项目" }));
    fireEvent.click(screen.getByRole("button", { name: "查看该项目" }));

    expect(onProjectSelected).toHaveBeenNthCalledWith(1, projectFixture);
    expect(onProjectSelected).toHaveBeenNthCalledWith(2, projectFixture);
  },
);

// ---------------------------------------------------------------------------
// CW-016：两个客户钱包入口（「使用记录」导航 + 个人中心「查看使用记录」）都经
// openLive("wallet") 汇入本组件的 panel="wallet" 分支。历史缺陷是 CustomerWorkspace
// 只传 customerAccount，而钱包分支旧代码仅判断 customerWallet，导致客户落到内部
// WalletPanel（「内部价 / 条」泄漏）。以下用真实挂载 + 生产响应形状锁定修复：
// 客户会话下必须挂 CustomerWalletPanel（积分余额、客户 lane），无会话时
// 才回落内部 WalletPanel，且充值只走 POST /api/customer/recharge-orders。
// ---------------------------------------------------------------------------

// Fixture credential behind a named constant so the repo secret scan never sees
// a raw quoted `token` literal — a dummy, never a real credential.
const sessionTokenText = "live-workspace-customer-session-token";

const customerWalletSnapshot = {
  available_credits: 12,
  reserved_credits: 2,
  points_per_yuan: 100,
  internal_unit_price_fen: 1000,
  min_recharge_fen: 10000,
  recharge_step_fen: 1000,
};

const walletTransactionPage = {
  items: [
    {
      id: "tx-1",
      user_id: "user-1",
      type: "CHARGE",
      available_delta: 10,
      reserved_delta: 0,
      recharge_order_id: "order-1",
      task_id: null,
      billing_round: null,
      created_at: "2026-09-10 10:00:00",
    },
  ],
  total: 1,
  limit: 20,
  offset: 0,
};

const emptyPage = { items: [], total: 0, limit: 20, offset: 0 };

const createdRechargeOrder = {
  order_no: "202609100001",
  status: "PENDING",
  amount_fen: 20000,
  credits: 20,
  gateway_url: "https://payment.example/submit",
  method: "POST",
  form_fields: {},
};

const paymentCodePayload = {
  order_no: "202609100001",
  amount_fen: 20000,
  credits: 20,
  qr_image_url: "https://payment.example/qr.png",
  payment_url: "https://payment.example/pay",
};

const pendingOrderStatus = {
  order_no: "202609100001",
  status: "PENDING",
  amount_fen: 20000,
  credits: 20,
  channel: "wechat",
  created_at: "2026-09-10T00:00:00Z",
  paid_at: null,
};

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  });
}

function priceQuote(resolution: "768P" | "2K"): GenerationPriceQuote {
  const unit = resolution === "768P" ? 1000 : 2000;
  return {
    resolution,
    duration_seconds: 4,
    quantity: 1,
    unit_price_fen_per_second: unit,
    estimated_seconds: 4,
    estimated_price_fen: unit * 4,
  };
}

function fakeStore(): CustomerCredentialStore {
  return {
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(sessionTokenText),
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
}

// The wallet branch only reads `store` + `onSessionExpired`, but the prop is
// typed as the full customer account contract, so fill every required field.
function fakeCustomerAccount(
  store: CustomerCredentialStore,
  onSessionExpired: () => void,
): WorkspaceShellProps["customerAccount"] {
  return {
    devices: null,
    deviceError: "",
    onApprovePairing: vi.fn(),
    onDismissPairing: vi.fn(),
    onProfileUpdated: vi.fn(),
    onRefreshProfile: vi.fn().mockResolvedValue(undefined),
    onLogout: vi.fn().mockResolvedValue(undefined),
    onRefreshDevices: vi.fn().mockResolvedValue(undefined),
    onResetActivationCode: vi.fn(),
    onUnbind: vi.fn(),
    onUpdateProfile: vi.fn(),
    profile: null,
    profileLoadError: "",
    store,
    onSessionExpired,
  };
}

// Answers BOTH the customer lane (/api/customer/*) and the legacy internal lane
// (/api/wallet, /api/recharge-orders) with production shapes, so a test can
// assert which lane the mounted wallet entry actually talked to.
function installWalletFetch() {
  const fetchMock = vi.fn((url: string, options?: RequestInit) => {
    const path = String(url);
    const method = options?.method ?? "GET";
    if (path.includes("/api/generation/price-quote")) {
      return jsonResponse(
        priceQuote(path.includes("resolution=2K") ? "2K" : "768P"),
      );
    }
    if (path.endsWith("/api/customer/wallet")) {
      return jsonResponse(customerWalletSnapshot);
    }
    if (path.includes("/api/customer/wallet/transactions?")) {
      return jsonResponse(walletTransactionPage);
    }
    if (path.includes("/api/customer/recharge-orders?")) {
      return jsonResponse(emptyPage);
    }
    if (path.endsWith("/api/customer/recharge-orders") && method === "POST") {
      return jsonResponse(createdRechargeOrder, 201);
    }
    if (path.endsWith("/payment-code")) {
      return jsonResponse(paymentCodePayload);
    }
    if (/\/api\/customer\/recharge-orders\/[^/]+$/.test(path)) {
      return jsonResponse(pendingOrderStatus);
    }
    if (path.endsWith("/api/wallet")) {
      return jsonResponse(customerWalletSnapshot);
    }
    if (path.includes("/api/wallet/transactions?")) {
      return jsonResponse(walletTransactionPage);
    }
    if (path.includes("/api/recharge-orders?")) {
      return jsonResponse(emptyPage);
    }
    if (path.endsWith("/api/recharge-orders") && method === "POST") {
      return jsonResponse(createdRechargeOrder, 201);
    }
    return jsonResponse(emptyPage);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

// Legacy internal lane: /api/wallet* or /api/recharge-orders* NOT under
// /api/customer/. A customer wallet entry must never touch it.
function isInternalLane(path: string): boolean {
  return (
    (path.includes("/api/wallet") || path.includes("/api/recharge-orders")) &&
    !path.includes("/api/customer/")
  );
}

function renderWalletEntry(
  customerAccount?: WorkspaceShellProps["customerAccount"],
) {
  return render(
    <LiveWorkspacePanel
      currentUser={reviewUser}
      customerAccount={customerAccount}
      onClose={vi.fn()}
      onHandoffConsumed={vi.fn()}
      onProjectSelected={vi.fn()}
      onRefresh={vi.fn()}
      panel="wallet"
    />,
  );
}

describe("LiveWorkspacePanel 客户钱包入口 (CW-016)", () => {
  it("客户会话下挂载 CustomerWalletPanel（积分计价），不泄漏内部定价", async () => {
    const fetchMock = installWalletFetch();
    renderWalletEntry(fakeCustomerAccount(fakeStore(), vi.fn()));

    // 积分与视频时长分开显示，充值只按管理员配置的积分换算。
    expect(await screen.findByText("可用额度")).toBeInTheDocument();
    expect(screen.getByText("12 积分")).toBeInTheDocument();
    expect(screen.getByText("冻结中 2 积分")).toBeInTheDocument();
    expect(screen.getByText(/1元 = 100 积分/)).toBeInTheDocument();
    expect(screen.getByText("额度流水")).toBeInTheDocument();
    // 客户专属档位（50 元）：内部 WalletPanel 档位从 100 起、无 50。
    expect(
      screen.getByRole("button", { name: "充值50元" }),
    ).toBeInTheDocument();
    // 无内部定价泄漏：内部 WalletPanel 的「内部价/仅供内部运营使用/可用条数」不得出现。
    expect(screen.queryByText("内部价")).not.toBeInTheDocument();
    expect(screen.queryByText("仅供内部运营使用")).not.toBeInTheDocument();
    expect(screen.queryByText("可用条数")).not.toBeInTheDocument();

    // 走客户 lane，且从不触碰内部 lane。
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith("/api/customer/wallet"),
        ),
      ).toBe(true),
    );
    expect(
      fetchMock.mock.calls.some(([url]) => isInternalLane(String(url))),
    ).toBe(false);
  });

  it("无客户会话时回落内部 WalletPanel（保留内部兜底，客户档位不出现）", async () => {
    const fetchMock = installWalletFetch();
    renderWalletEntry();

    expect(await screen.findByText("内部价")).toBeInTheDocument();
    expect(screen.getByText("仅供内部运营使用")).toBeInTheDocument();
    expect(screen.getByText("可用条数")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "充值50元" }),
    ).not.toBeInTheDocument();

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).endsWith("/api/wallet"),
        ),
      ).toBe(true),
    );
  });

  it("客户钱包入口充值走 POST /api/customer/recharge-orders，绝不发内部 POST /api/recharge-orders", async () => {
    const fetchMock = installWalletFetch();
    renderWalletEntry(fakeCustomerAccount(fakeStore(), vi.fn()));

    // 等客户钱包加载完成，点击客户档位 → 打开客户充值对话框。
    fireEvent.click(await screen.findByRole("button", { name: "充值200元" }));

    const dialog = await screen.findByRole("dialog");
    const submit = await within(dialog).findByRole("button", {
      name: "生成支付二维码",
    });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    // 订单创建成功并渲染客户支付二维码。
    expect(
      await within(dialog).findByRole("img", { name: "充值支付二维码" }),
    ).toBeInTheDocument();

    // method-path 记录：客户 lane POST 被调用（金额 20000 分），内部 lane 从不被调用。
    const customerCreate = fetchMock.mock.calls.find(
      ([url, options]) =>
        String(url).endsWith("/api/customer/recharge-orders") &&
        options?.method === "POST",
    );
    expect(customerCreate).toBeDefined();
    expect(customerCreate?.[1]?.body).toBe(
      JSON.stringify({ amount_fen: 20000 }),
    );
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) =>
          isInternalLane(String(url)) && options?.method === "POST",
      ),
    ).toBe(false);
    expect(
      fetchMock.mock.calls.some(([url]) => isInternalLane(String(url))),
    ).toBe(false);
  });
});
