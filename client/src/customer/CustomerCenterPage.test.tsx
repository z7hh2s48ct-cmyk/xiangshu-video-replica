import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { CustomerApiError } from "../api";
import type { WorkspaceShellProps } from "../workspace-shell";
import { CustomerCenterPage } from "./CustomerCenterPage";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  initialize: vi.fn(),
  create: vi.fn(),
  rotate: vi.fn(),
  revoke: vi.fn(),
  summary: vi.fn(),
  navigate: vi.fn(),
  notice: vi.fn(),
  transactions: vi.fn(),
  orders: vi.fn(),
}));
vi.mock("../studio/context", () => ({
  useStudio: () => ({
    navigate: mocks.navigate,
    notify: mocks.notice,
    user: { username: "alice", display_name: "Alice" },
  }),
}));
vi.mock("../studio/MainPages", () => ({
  PublishAccountsPanel: () => <div>发布账号真实面板</div>,
}));
vi.mock("../studio/live", () => ({ loadPublishAccounts: async () => [] }));
vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  customerListApiKeys: mocks.list,
  customerInitializeDefaultApiKey: mocks.initialize,
  customerCreateApiKey: mocks.create,
  customerRotateApiKey: mocks.rotate,
  customerRevokeApiKey: mocks.revoke,
  customerGetCenterSummary: mocks.summary,
  customerListWalletTransactions: mocks.transactions,
  customerListRechargeOrders: mocks.orders,
  getStudioNotificationPreferences: async () => ({ enabled: true }),
}));

const token = {
  id: "key-1",
  token_group_id: "key-1",
  credential_version: 1,
  key_prefix: "ABCDEFGH",
  label: "默认 Token",
  scopes: ["wallet"],
  created_at: "2026-09-12T12:00:00Z",
  last_used_at: null,
  revoked_at: null,
  is_default: true,
  total_consumed_credits: 0,
};
function setup() {
  vi.clearAllMocks();
  mocks.list.mockResolvedValue({ items: [token], total: 1 });
  mocks.summary.mockResolvedValue({
    user_id: "alice-id",
    available_credits: 125,
    reserved_credits: 10,
    total_consumed_credits: 22,
    active_tokens: 1,
  });
  mocks.transactions.mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
  });
  mocks.orders.mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  const account = {
    profile: {
      user_id: "alice-id",
      username: "alice",
      display_name: "Alice",
      joined_at: "2026-09-12T12:00:00Z",
      activation_code_masked: null,
      activation_status: null,
      activated_at: null,
      device_slots_used: 1,
      device_slots_total: null,
    },
    profileLoadError: "",
    devices: { slots: [], pending_pairings: [] },
    deviceError: "",
    store: { loadSessionToken: async () => "ephemeral-test-session" },
    onSessionExpired: vi.fn(),
    onRefreshProfile: vi.fn().mockResolvedValue(undefined),
    onRefreshDevices: vi.fn(),
    onLogout: vi.fn(),
    onUpdateProfile: vi.fn(),
    onProfileUpdated: vi.fn(),
    onUnbind: vi.fn(),
  } as unknown as NonNullable<WorkspaceShellProps["customerAccount"]>;
  return account;
}

test("renders real account points and six focused tabs without reissuing an existing default", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  expect(screen.getAllByRole("tab")).toHaveLength(6);
  expect(screen.getByRole("tab", { name: "接口价格" })).toBeVisible();
  expect(screen.getByText("alice-id")).toBeVisible();
  expect(mocks.initialize).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "返回主界面" }));
  expect(mocks.navigate).toHaveBeenCalledWith("workbench");
});

test("uses the same gold logo and brand names as the studio", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  expect(screen.getByRole("img", { name: "众墅之家" })).toHaveAttribute(
    "src",
    "/studio/logo-mark.svg",
  );
  expect(screen.getByText("众墅之家")).toBeVisible();
  expect(screen.getByText("AI 即创")).toBeVisible();
});

test("opening account records refreshes a balance changed by an administrator", async () => {
  const account = setup();
  render(<CustomerCenterPage account={account} />);
  expect(await screen.findByText("125")).toBeVisible();
  mocks.summary.mockResolvedValue({
    user_id: "alice-id",
    available_credits: 175,
    reserved_credits: 10,
    total_consumed_credits: 22,
    active_tokens: 1,
  });
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));
  expect(await screen.findByText("175")).toBeVisible();
  expect(screen.queryByText("125")).toBeNull();
});

test("filters the actual image, transcription and link services shown in the ledger", async () => {
  render(<CustomerCenterPage account={setup()} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));
  for (const [value, label] of [
    ["character", "人物形象及任务图片"],
    ["asr", "语音转写"],
    ["link_resolution", "链接解析"],
  ]) {
    expect(screen.getByRole("option", { name: label })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("业务"), { target: { value } });
    await waitFor(() =>
      expect(mocks.transactions).toHaveBeenLastCalledWith(
        expect.anything(),
        expect.objectContaining({
          filters: expect.objectContaining({ business: value }),
        }),
      ),
    );
  }
});

test("opens the pricing basis of a settled row inside the records tab", async () => {
  const account = setup();
  mocks.transactions.mockResolvedValue({
    items: [
      {
        id: "ledger-priced",
        type: "SETTLE",
        created_at: "2026-09-13T00:00:00Z",
        available_delta: 0,
        reserved_delta: -3,
        billing_round: 1,
        credit_price_version: 3,
        service: "asr",
        service_name: "语音转写",
        pricing: {
          service: "asr",
          version: 3,
          unit: "second",
          units: "3.000000",
          unit_credits: "2.000000",
          unit_rounding: "ceil",
          discount_basis_points: 9500,
          consumption_rounding: "floor",
          credits: 5,
          enabled: true,
        },
      },
    ],
    total: 1,
    limit: 20,
    offset: 0,
  });
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));

  const summary = await screen.findByText("计费依据 · 费率 V3");
  expect(summary.closest("details")?.open).toBe(false);
  // 折叠态下 jest-dom 不认为内容可见，存在性由文档断言。
  expect(
    screen.getByText("单价：2 积分/秒，不足 1 秒按 1 秒计"),
  ).toBeInTheDocument();
  expect(screen.getByText("预扣上限：5 积分")).toBeInTheDocument();
  expect(screen.getByText("价格 V3")).toBeVisible();
});

test("folds one billing cycle into a single expandable entry in the records table", async () => {
  const account = setup();
  mocks.transactions.mockResolvedValue({
    items: [
      {
        id: "ledger-release",
        type: "RELEASE",
        created_at: "2026-09-13T00:00:02Z",
        available_delta: 2,
        reserved_delta: -2,
        billing_operation_id: "op-1",
        pair_state: "SETTLED",
        service: "asr",
        service_name: "语音转写",
      },
      {
        id: "ledger-settle",
        type: "SETTLE",
        created_at: "2026-09-13T00:00:02Z",
        available_delta: 0,
        reserved_delta: -3,
        billing_operation_id: "op-1",
        pair_state: "SETTLED",
        service: "asr",
        service_name: "语音转写",
      },
      {
        id: "ledger-reserve",
        type: "RESERVE",
        created_at: "2026-09-13T00:00:01Z",
        available_delta: -5,
        reserved_delta: 5,
        billing_operation_id: "op-1",
        pair_state: "SETTLED",
        service: "asr",
        service_name: "语音转写",
      },
    ],
    total: 3,
    limit: 20,
    offset: 0,
  });
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));

  // 折叠：三笔只剩一行摘要，净变化是整组的合计。
  const summary = await screen.findByText("预扣 5 → 实扣 3 → 退回 2");
  expect(summary).toBeVisible();
  // 「任务预扣」在筛选下拉里也有同名选项，断言限定在表格内。
  const table = summary.closest("table") as HTMLElement;
  expect(within(table).queryByText("任务预扣")).toBeNull();
  expect(screen.getByText("-3 积分")).toBeVisible();
  expect(screen.getByText("0 积分")).toBeVisible();

  fireEvent.click(screen.getByRole("button", { name: /查看 3 笔明细/ }));
  expect(await within(table).findByText("任务预扣")).toBeVisible();
  expect(screen.getByText("+5 积分")).toBeVisible();
  expect(screen.getByText("-2 积分")).toBeVisible();
});

test.each([
  ["oral-1", null, "oral-oral-1", "oral_task", "oral-1"],
  [null, "batch-1", "batch-1", "generation_batch", "batch-1"],
])(
  "opens the exact ledger task and returns to profile (%s)",
  async (oral, batch, selected, kind, backend) => {
    const account = setup();
    mocks.transactions.mockResolvedValue({
      items: [
        {
          id: "ledger-1",
          type: "SETTLE",
          created_at: "2026-09-13T00:00:00Z",
          available_delta: 0,
          reserved_delta: -42,
          oral_task_id: oral,
          generation_batch_id: batch,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    render(<CustomerCenterPage account={account} />);
    await screen.findByText("125");
    fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看任务" }));
    expect(mocks.navigate).toHaveBeenCalledWith("task-detail", {
      selectedTaskId: selected,
      selectedTaskKind: kind,
      selectedTaskBackendId: backend,
      returnTo: "profile",
    });
  },
);

test("creates a Token through the API and clears its one-time secret on close", async () => {
  const account = setup();
  mocks.create.mockResolvedValue({
    ...token,
    id: "key-2",
    label: "工作电脑",
    plaintext: "one-time-test-value",
  });
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("button", { name: "新建 Token" }));
  fireEvent.change(screen.getByLabelText("Token 名称"), {
    target: { value: "工作电脑" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认新建" }));
  expect(await screen.findByDisplayValue("one-time-test-value")).toBeVisible();
  await waitFor(() =>
    expect(mocks.create).toHaveBeenCalledWith(
      expect.objectContaining({ kind: "session" }),
      "工作电脑",
      expect.any(String),
    ),
  );
  fireEvent.click(screen.getByRole("button", { name: "已保存，关闭" }));
  expect(
    screen.queryByDisplayValue("one-time-test-value"),
  ).not.toBeInTheDocument();
});

test("failed summary stays unknown and retry loads the real balance", async () => {
  const account = setup();
  mocks.summary.mockRejectedValueOnce(new Error("账号服务暂不可用"));
  render(<CustomerCenterPage account={account} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "账号服务暂不可用",
  );
  expect(screen.queryByText("125")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "重试加载账号" }));
  expect(await screen.findByText("125")).toBeVisible();
});

test("a revoked default is not re-created and order errors are not shown as empty history", async () => {
  const account = setup();
  mocks.list.mockResolvedValue({
    items: [{ ...token, revoked_at: "2026-09-12T13:00:00Z" }],
    total: 1,
  });
  mocks.orders.mockRejectedValueOnce(new Error("充值记录暂不可用"));
  render(<CustomerCenterPage account={account} />);
  expect(await screen.findByText("已撤销")).toBeVisible();
  expect(mocks.initialize).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("tab", { name: "充值记录" }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "充值记录暂不可用",
  );
  expect(screen.queryByText("暂无充值记录")).toBeNull();
});

test("each function has one destination and account settings contain no device section", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  expect(screen.queryByRole("tab", { name: "账号概览" })).toBeNull();
  expect(screen.getByRole("tab", { name: "Token 管理" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));
  expect(screen.queryByRole("button", { name: "新建 Token" })).toBeNull();
  expect(screen.queryByText("登录设备")).toBeNull();
  expect(screen.queryByRole("button", { name: /设备/ })).toBeNull();
  expect(screen.getByRole("switch", { name: "任务通知" })).toBeVisible();
});

test("expired default recovery reloads existing credentials instead of looping on the expired key", async () => {
  const account = setup();
  mocks.list.mockResolvedValueOnce({ items: [], total: 0 });
  mocks.initialize.mockRejectedValue(
    new CustomerApiError({
      message: "恢复窗口已结束，请刷新列表后重试。",
      status: 409,
      code: "TOKEN_RETRY_EXPIRED",
    }),
  );
  render(<CustomerCenterPage account={account} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("恢复窗口已结束");
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(await screen.findByText("默认 Token")).toBeVisible();
  expect(mocks.initialize).toHaveBeenCalledTimes(1);
});
