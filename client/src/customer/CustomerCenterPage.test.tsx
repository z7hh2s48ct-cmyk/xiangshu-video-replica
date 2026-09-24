import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { CustomerApiError, type CustomerProfile } from "../api";
import type { WorkspaceShellProps } from "../workspace-shell";
import { CustomerCenterPage } from "./CustomerCenterPage";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  initialize: vi.fn(),
  create: vi.fn(),
  rotate: vi.fn(),
  revoke: vi.fn(),
  revokeAll: vi.fn(),
  revokeAllSessions: vi.fn(),
  summary: vi.fn(),
  navigate: vi.fn(),
  notice: vi.fn(),
  transactions: vi.fn(),
  orders: vi.fn(),
  subAccounts: vi.fn(),
  exportCsv: vi.fn(),
  history: vi.fn(),
  passwordState: vi.fn(),
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
  customerRevokeAllApiKeys: mocks.revokeAll,
  customerRevokeAllSessions: mocks.revokeAllSessions,
  customerGetCenterSummary: mocks.summary,
  customerListWalletTransactions: mocks.transactions,
  customerListRechargeOrders: mocks.orders,
  customerListSubAccounts: mocks.subAccounts,
  customerExportWalletTransactionsCSV: mocks.exportCsv,
  customerListLoginHistory: mocks.history,
  // 账号设置里的「账号登录」卡片会读密码状态；不 mock 就会打真实网络。
  customerPasswordState: mocks.passwordState,
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
/** 母账号的账号资料：`/api/customer/profile` 对母账号的权威回答。 */
const masterProfile: CustomerProfile = {
  user_id: "alice-id",
  username: "alice",
  display_name: "Alice",
  joined_at: "2026-09-12T12:00:00Z",
  activation_code_masked: null,
  activation_status: null,
  activated_at: null,
  device_slots_used: 1,
  device_slots_total: null,
  account_type: "MASTER",
  parent_user_id: null,
  parent_display_name: null,
  monthly_quota_credits: null,
  quota_used_credits: null,
};

/** 子账号的账号资料：身份/母账号名/月度额度都在这一份响应里。 */
function subProfile(overrides: Partial<CustomerProfile> = {}): CustomerProfile {
  return {
    ...masterProfile,
    user_id: "bob-id",
    username: "bob",
    display_name: "Bob",
    account_type: "SUB",
    parent_user_id: "alice-id",
    parent_display_name: "总部机构",
    ...overrides,
  };
}

const subIdentity = {
  accountType: "SUB" as const,
  parentUserId: "alice-id",
  parentDisplayName: "总部机构",
};

function setup(
  overrides: {
    /** 显式传 null 表示「资料还没读到」；不传就是母账号资料。 */
    profile?: CustomerProfile | null;
    identity?: typeof subIdentity | null;
  } = {},
) {
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
  mocks.subAccounts.mockResolvedValue([]);
  mocks.history.mockResolvedValue({ items: [], total: 0 });
  mocks.revokeAllSessions.mockResolvedValue({ revoked_sessions: 1 });
  mocks.subAccounts.mockResolvedValue([]);
  mocks.passwordState.mockResolvedValue({
    user_id: "alice-id",
    username: "alice",
    has_password: true,
  });
  const account = {
    profile:
      overrides.profile === undefined ? masterProfile : overrides.profile,
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
    identity: overrides.identity ?? null,
    loadIdentity: async () => overrides.identity ?? null,
  } as unknown as NonNullable<WorkspaceShellProps["customerAccount"]>;
  return account;
}

/** 身份卡里的 dl 行：按 dt 取 dd。不靠 DOM 顺序，行序变化不会误伤断言。 */
function identityValue(root: HTMLElement, label: string): HTMLElement {
  const term = within(root).getByText(label);
  const value = term.parentElement?.querySelector("dd");
  if (!value) throw new Error(`身份卡没有「${label}」这一行的取值`);
  return value as HTMLElement;
}

test("renders real account points and seven focused tabs without reissuing an existing default", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  // 七 = 原六项 + 子账号管理（母账号可见）；计数变化是本轮新增能力的直接后果。
  expect(screen.getAllByRole("tab")).toHaveLength(7);
  expect(screen.getByRole("tab", { name: "接口价格" })).toBeVisible();
  expect(screen.getByText("alice-id")).toBeVisible();
  expect(mocks.initialize).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "返回主界面" }));
  expect(mocks.navigate).toHaveBeenCalledWith("workbench");
});

test("桌面端多一个「设备管理」页签，进入时按需读取设备", async () => {
  const account = setup();
  const onRefreshDevices = vi.fn().mockResolvedValue(undefined);
  // 桌面端判据是 store.devicePlatform() !== "browser"；设备数据此处刻意留空，
  // 用来验证页签自己会把第一次读取补上（工作区只在明确动作时拉设备）。
  const desktop = {
    ...account,
    devices: null,
    onRefreshDevices,
    store: {
      devicePlatform: () => "windows",
      loadSessionToken: async () => "s",
    },
  } as unknown as NonNullable<WorkspaceShellProps["customerAccount"]>;
  render(<CustomerCenterPage account={desktop} />);
  await screen.findByText("125");

  // 八 = 六项基础 + 设备管理（桌面端）+ 子账号管理（母账号）。两者来自不同分支，
  // 合并后各自 +1。
  expect(screen.getAllByRole("tab")).toHaveLength(8);
  fireEvent.click(screen.getByRole("tab", { name: "设备管理" }));

  await waitFor(() => expect(onRefreshDevices).toHaveBeenCalledTimes(1));
  expect(screen.getByText("正在读取设备信息…")).toBeVisible();
});

test("web 端（devicePlatform 为 browser）不出现设备管理页签", async () => {
  const account = setup();
  const web = {
    ...account,
    store: {
      devicePlatform: () => "browser",
      loadSessionToken: async () => "s",
    },
  } as unknown as NonNullable<WorkspaceShellProps["customerAccount"]>;
  render(<CustomerCenterPage account={web} />);
  await screen.findByText("125");

  // 七 = 六项基础 + 子账号管理（母账号）；设备管理在 web 端被平台过滤掉，
  // 所以比桌面端少的那一项正是它。
  expect(screen.getAllByRole("tab")).toHaveLength(7);
  expect(screen.queryByRole("tab", { name: "设备管理" })).toBeNull();
});

test("消费记录：子账号下拉把筛选传给后端，汇总开关拉聚合摘要", async () => {
  const account = setup();
  mocks.subAccounts.mockResolvedValue([
    { id: "sub-1", username: "zhangsan", display_name: "张三" },
  ]);
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));

  // 子账号下拉出现后，选中它应带上 sub_account_id
  const subSelect = await screen.findByLabelText("子账号");
  fireEvent.change(subSelect, { target: { value: "sub-1" } });
  await waitFor(() =>
    expect(mocks.transactions).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({
        filters: expect.objectContaining({ sub_account_id: "sub-1" }),
      }),
    ),
  );

  // 打开汇总：请求带 group_by_sub_account，并渲染摘要表
  mocks.transactions.mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
    sub_account_summary: [
      {
        sub_account_id: "sub-1",
        sub_account_name: "张三",
        debit_total: 200,
        credit_total: 25,
        transaction_count: 3,
      },
    ],
  });
  fireEvent.click(screen.getByRole("checkbox", { name: "按子账号汇总" }));

  await waitFor(() =>
    expect(mocks.transactions).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({
        filters: expect.objectContaining({ group_by_sub_account: "true" }),
      }),
    ),
  );
  // 「张三」同时出现在筛选下拉与摘要表里，用 getAllByText 而不是 getByText
  expect(await screen.findByText("200 积分")).toBeVisible();
  expect(screen.getByText("25 积分")).toBeVisible();
  expect(screen.getAllByText("张三").length).toBeGreaterThan(1);
  expect(screen.getByText("3")).toBeVisible();
});

test("消费记录：导出 CSV 带上当前筛选，清除筛选只在有筛选时出现", async () => {
  const account = setup();
  mocks.exportCsv.mockResolvedValue({
    filename: "wallet-transactions-202609.csv",
    text: "时间,类型\r\n2026-09-20 10:00:00+08,SETTLE\r\n",
  });
  // jsdom 没有实现 createObjectURL
  const createObjectURL = vi.fn(() => "blob:e2e");
  const revokeObjectURL = vi.fn();
  Object.assign(URL, { createObjectURL, revokeObjectURL });
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));

  // 没有任何筛选时不显示「清除筛选」
  expect(screen.queryByRole("button", { name: "清除筛选" })).toBeNull();

  fireEvent.change(screen.getByLabelText("流水类型"), {
    target: { value: "SETTLE" },
  });
  const clear = await screen.findByRole("button", { name: "清除筛选" });

  fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
  await waitFor(() =>
    expect(mocks.exportCsv).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ transaction_type: "SETTLE" }),
    ),
  );

  // 清除筛选：回到无筛选态并重取
  fireEvent.click(clear);
  await waitFor(() =>
    expect(mocks.transactions).toHaveBeenLastCalledWith(
      expect.anything(),
      expect.objectContaining({ filters: {} }),
    ),
  );
});

test("消费记录：业务筛选按类分组，不再平铺 12 项", async () => {
  render(<CustomerCenterPage account={setup()} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));

  const business = screen.getByLabelText("业务");
  const groups = business.querySelectorAll("optgroup");
  expect(groups.length).toBeGreaterThan(1);
  expect(
    Array.from(groups).map((group) => group.getAttribute("label")),
  ).toContain("数字人");
});

test("发布账号页签给出上下文说明与跳转（P1#11）", async () => {
  render(<CustomerCenterPage account={setup()} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "发布账号" }));

  expect(screen.getByText(/这里绑定的是各平台的登录状态/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "去发布成片" }));
  expect(mocks.navigate).toHaveBeenCalledWith("publishing");
});

test("待结算与累计消费从首屏挪进消费记录页签（P1#6）", async () => {
  render(<CustomerCenterPage account={setup()} />);
  await screen.findByText("125");

  // 首屏（Token 管理）不再出现这两个数字
  expect(screen.queryByRole("region", { name: "流水总额" })).toBeNull();

  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));
  const totals = await screen.findByRole("region", { name: "流水总额" });
  expect(totals).toHaveTextContent("待结算");
  expect(totals).toHaveTextContent("累计消费");
  // 数字本身来自 summary（reserved 10 / consumed 22）
  expect(totals).toHaveTextContent("10");
  expect(totals).toHaveTextContent("22");
});

test("Token 卡片化并显示消费占比（机会点 2 / P1#5）", async () => {
  const account = setup();
  mocks.list.mockResolvedValue({
    items: [
      {
        ...token,
        id: "key-a",
        label: "主力 Token",
        total_consumed_credits: 300,
      },
      {
        ...token,
        id: "key-b",
        label: "备用 Token",
        is_default: false,
        total_consumed_credits: 100,
      },
    ],
    total: 2,
  });
  render(<CustomerCenterPage account={account} />);

  expect(await screen.findByText("主力 Token")).toBeVisible();
  // 占比以可访问名暴露（300/400 = 75%）
  expect(screen.getByRole("img", { name: "占账号累计消费 75%" })).toBeVisible();
  expect(screen.getByRole("img", { name: "占账号累计消费 25%" })).toBeVisible();
  // 卡片保留原有操作与状态文案
  expect(screen.getAllByRole("button", { name: /更新/ }).length).toBe(2);
  expect(screen.getAllByRole("button", { name: /撤销/ }).length).toBe(2);
});

test("没有 Token 时给出空态引导与创建入口（机会点 2）", async () => {
  const account = setup();
  // 页面在「没有默认 Token」时会自动初始化一枚；这里让初始化成功但列表仍为空，
  // 才能走到「加载完成且确实没有 Token」的空态分支（否则会落到错误分支）。
  mocks.initialize.mockResolvedValue({
    ...token,
    id: "key-new",
    plaintext: "one-time-secret",
  });
  mocks.list.mockResolvedValue({ items: [], total: 0 });
  render(<CustomerCenterPage account={account} />);

  expect(await screen.findByText("还没有 Token")).toBeVisible();
  // 首访引导的第一步也提到同一件事，用 getAllByText 而不是 getByText
  expect(
    screen.getAllByText(/完整值只在创建时显示一次/).length,
  ).toBeGreaterThanOrEqual(1);
  // 头部与空态里各有一个创建入口
  expect(
    screen.getAllByRole("button", { name: /创建第一个 Token|新建 Token/ })
      .length,
  ).toBeGreaterThanOrEqual(2);
});

test("数字键 1..N 直达页签（P2#17）", async () => {
  render(<CustomerCenterPage account={setup()} />);
  await screen.findByText("125");

  fireEvent.keyDown(screen.getByRole("tab", { name: "Token 管理" }), {
    key: "2",
  });
  expect(screen.getByRole("tab", { name: "消费记录" })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  // 超出页签数量的数字键不做事，也不会把焦点弄丢
  fireEvent.keyDown(screen.getByRole("tab", { name: "消费记录" }), {
    key: "9",
  });
  expect(screen.getByRole("tab", { name: "消费记录" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
});

test("显示名称给字数提示（P2#19）", async () => {
  render(<CustomerCenterPage account={setup()} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));

  // 初始是资料里的昵称长度，输入后跟着变
  expect(await screen.findByText("5/50")).toBeVisible();
  fireEvent.change(screen.getByLabelText("显示名称"), {
    target: { value: "Alice 的账号" },
  });
  expect(screen.getByText("9/50")).toBeVisible();
});

test("术语去技术化：Token 表说「第 N 次更新」、来源下拉说「早期版本消费」", async () => {
  render(<CustomerCenterPage account={setup()} />);
  await screen.findByText("125");

  // Token 表不再裸露「凭据版本」（audit-7 类术语泄漏）
  expect(screen.queryByText(/凭据版本/)).toBeNull();
  expect(screen.getByText(/第 1 次更新/)).toBeVisible();

  // 消费来源下拉：把「历史来源未记录」换成客户能懂的「早期版本消费」
  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));
  expect(
    screen.getByRole("option", { name: "早期版本消费" }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "历史来源未记录" })).toBeNull();
});

test("母账号能进入子账号管理页签，读到的是真实子账号接口", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  fireEvent.click(screen.getByRole("tab", { name: "子账号管理" }));
  expect(
    await screen.findByRole("region", { name: "子账号管理" }),
  ).toBeVisible();
  expect(mocks.subAccounts).toHaveBeenCalled();
});

test("子账号看不到子账号管理页签（服务端对 SUB 一律 403）", async () => {
  render(<CustomerCenterPage account={setup({ profile: subProfile() })} />);
  expect(await screen.findByText("125")).toBeVisible();
  expect(screen.queryByRole("tab", { name: "子账号管理" })).toBeNull();
  expect(mocks.subAccounts).not.toHaveBeenCalled();
});

test("SUB_ADMIN 仍能看到子账号管理页签（服务端放行，且它自己算子账号）", async () => {
  render(
    <CustomerCenterPage
      account={setup({ profile: subProfile({ account_type: "SUB_ADMIN" }) })}
    />,
  );
  expect(await screen.findByText("125")).toBeVisible();
  // 判定按「是不是普通 SUB」，不是「是不是 MASTER」：SUB_ADMIN 有母账号，
  // 但服务端确实放行它的子账号管理接口，藏掉入口才是丢能力。
  expect(screen.getByRole("tab", { name: "子账号管理" })).toBeVisible();
  const card = screen.getByRole("region", { name: "账号身份" });
  expect(within(card).getByText("子账号")).toBeVisible();
  expect(identityValue(card, "本月额度")).toBeVisible();
});

test("身份徽章区分母/子账号并给出所属母账号", async () => {
  const master = render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  const masterCard = screen.getByRole("region", { name: "账号身份" });
  expect(within(masterCard).getByText("母账号")).toBeVisible();
  expect(within(masterCard).queryByText(/所属母账号/)).toBeNull();
  master.unmount();

  render(<CustomerCenterPage account={setup({ profile: subProfile() })} />);
  expect(await screen.findByText("125")).toBeVisible();
  const subCard = screen.getByRole("region", { name: "账号身份" });
  expect(within(subCard).getByText("子账号")).toBeVisible();
  expect(within(subCard).getByText(/所属母账号：总部机构/)).toBeVisible();
});

test("身份未知时不冒充母账号：profile 未到时用缓存身份兜底", async () => {
  render(
    <CustomerCenterPage
      account={setup({ profile: null, identity: subIdentity })}
    />,
  );
  expect(await screen.findByText("125")).toBeVisible();
  const card = screen.getByRole("region", { name: "账号身份" });
  expect(within(card).getByText("子账号")).toBeVisible();
  // 资料没到：额度、设备都显示读取中，不拿 0 台 / 0 积分顶替。
  expect(identityValue(card, "本月额度")).toHaveTextContent("读取中");
  expect(identityValue(card, "本月已用")).toHaveTextContent("读取中");
  expect(identityValue(card, "本月剩余")).toHaveTextContent("读取中");
  expect(identityValue(card, "已绑定设备")).toHaveTextContent("读取中");
});

test("子账号的月度额度读数面覆盖设限 / 不限 / 已用尽三态", async () => {
  const capped = render(
    <CustomerCenterPage
      account={setup({
        profile: subProfile({
          monthly_quota_credits: 5000,
          quota_used_credits: 1800,
        }),
      })}
    />,
  );
  expect(await screen.findByText("125")).toBeVisible();
  let card = screen.getByRole("region", { name: "账号身份" });
  expect(identityValue(card, "本月额度")).toHaveTextContent("5,000 积分");
  expect(identityValue(card, "本月已用")).toHaveTextContent("1,800 积分");
  expect(identityValue(card, "本月剩余")).toHaveTextContent("3,200 积分");
  expect(identityValue(card, "本月剩余")).toHaveTextContent("36%");
  capped.unmount();

  // 上限为 null = 不限（服务端对没有额度行的子账号就回 null）。
  render(
    <CustomerCenterPage
      account={setup({
        profile: subProfile({
          monthly_quota_credits: null,
          quota_used_credits: 320,
        }),
      })}
    />,
  );
  expect(await screen.findByText("125")).toBeVisible();
  card = screen.getByRole("region", { name: "账号身份" });
  expect(identityValue(card, "本月额度")).toHaveTextContent("不限");
  expect(identityValue(card, "本月已用")).toHaveTextContent("320 积分");
  expect(identityValue(card, "本月剩余")).toHaveTextContent("不限");
});

test("额度超限时剩余钳到 0 并标出已用尽，不给出负数", async () => {
  render(
    <CustomerCenterPage
      account={setup({
        profile: subProfile({
          monthly_quota_credits: 1000,
          quota_used_credits: 1200,
        }),
      })}
    />,
  );
  expect(await screen.findByText("125")).toBeVisible();
  const card = screen.getByRole("region", { name: "账号身份" });
  expect(identityValue(card, "本月剩余")).toHaveTextContent("0 积分");
  expect(identityValue(card, "本月剩余")).toHaveTextContent("已用尽");
});

test("母账号不出现额度读数（自己持有钱包，永不被限额）", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  const card = screen.getByRole("region", { name: "账号身份" });
  expect(within(card).queryByText("本月额度")).toBeNull();
  expect(identityValue(card, "已绑定设备")).toHaveTextContent("1 台");
});

test("uses the same gold logo and brand names as the studio", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  // 品牌图形为 src/assets/brand/ 模块导入：小 SVG 内联为 data URI，
  // 超阈值时是哈希文件名，两种形态都接受。
  expect(
    screen.getByRole("img", { name: "众墅之家" }).getAttribute("src"),
  ).toMatch(/^data:image\/svg\+xml|zhongshu-logo-mark\.svg$/);
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
  // 文案与子账号权限矩阵同源（permissionViz.BUSINESS_FEATURES）：同一个业务在客户
  // 界面里只能有一个名字，所以这里不是「视频分析 / 语音转写 / 人物形象及任务图片」。
  for (const [value, label] of [
    ["character", "人物形象"],
    ["asr", "语音识别"],
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

test("进入消费记录先显示加载态，而不是「暂无积分流水」（评审 #6）", async () => {
  const account = setup();
  // 请求一直悬着：加载态期间界面不能宣称「暂无积分流水」。
  mocks.transactions.mockReturnValue(new Promise(() => {}));
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");

  fireEvent.click(screen.getByRole("tab", { name: "消费记录" }));

  expect(screen.getByText("正在读取记录…")).toBeVisible();
  expect(
    screen.queryByText("暂无积分流水，开始创作后会在这里记录。"),
  ).toBeNull();
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

test("错误按类给提示：5xx 说稍后重试，401 说重新登录（P1#8）", async () => {
  const account = setup();
  mocks.summary.mockRejectedValueOnce(
    new CustomerApiError({ message: "服务暂时不可用", status: 503 }),
  );
  const { unmount } = render(<CustomerCenterPage account={account} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "平台这边出了点问题",
  );
  expect(screen.getByRole("alert")).toHaveTextContent("服务暂时不可用");
  unmount();

  const second = setup();
  mocks.summary.mockRejectedValueOnce(
    new CustomerApiError({
      message: "session required",
      status: 401,
      code: "SESSION_REQUIRED",
    }),
  );
  render(<CustomerCenterPage account={second} />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "登录状态已失效，请重新登录",
  );
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

test("each function has one destination and account settings contain no device management", async () => {
  render(<CustomerCenterPage account={setup()} />);
  expect(await screen.findByText("125")).toBeVisible();
  expect(screen.queryByRole("tab", { name: "账号概览" })).toBeNull();
  expect(screen.getByRole("tab", { name: "Token 管理" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));
  expect(screen.queryByRole("button", { name: "新建 Token" })).toBeNull();
  // 设备管理（槽位列表、解绑）不在这里；「退出所有设备」是会话自救动作，不是
  // 设备管理，所以断言按能力而不是按「设备」这两个字（CW-062 B4）。
  expect(screen.queryByText("登录设备")).toBeNull();
  expect(screen.queryByRole("button", { name: /解绑/ })).toBeNull();
  expect(screen.getByRole("switch", { name: "任务通知" })).toBeVisible();
  expect(screen.getByRole("heading", { name: /账号安全/ })).toBeVisible();
});

test("security card offers the three self-rescue levers and the login history", async () => {
  const account = setup();
  mocks.history.mockResolvedValue({
    items: [
      {
        occurred_at: "2026-09-22T08:00:00+00:00",
        event: "LOGIN",
        device_name: "办公室台式机",
        platform: "windows",
        reason: null,
      },
    ],
    total: 3,
  });
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));

  expect(await screen.findByText(/办公室台式机/)).toBeVisible();
  expect(screen.getByText(/共 3 条记录/)).toBeVisible();
  expect(screen.getByRole("button", { name: "修改密码" })).toBeVisible();
  expect(screen.getByRole("button", { name: "退出所有设备" })).toBeVisible();
  expect(screen.getByRole("button", { name: "撤销全部 Token" })).toBeVisible();
});

test("撤销全部 Token needs the acknowledgement and then reports the count", async () => {
  const account = setup();
  mocks.revokeAll.mockResolvedValue({ revoked: 1 });
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));

  fireEvent.click(screen.getByRole("button", { name: "撤销全部 Token" }));
  const dialog = await screen.findByRole("dialog", {
    name: "撤销全部 Token？",
  });
  // 没勾「我已知晓」之前，确认按钮不产生任何调用。
  fireEvent.click(
    within(dialog).getByRole("button", { name: "撤销全部 Token" }),
  );
  expect(mocks.revokeAll).not.toHaveBeenCalled();
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "请先勾选确认操作",
  );
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(
    within(dialog).getByRole("button", { name: "撤销全部 Token" }),
  );
  await waitFor(() => expect(mocks.revokeAll).toHaveBeenCalledTimes(1));
  expect(await screen.findByText("已撤销 1 枚 Token。")).toBeVisible();
});

test("Token 列表没读到时，撤销全部 Token 不报数字（不拿 0 冒充）", async () => {
  const account = setup();
  mocks.list.mockRejectedValue(new Error("Token 列表暂不可用"));
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));

  expect(
    await screen.findByText("已发出的 Token 会立即失效，程序调用会被拒绝。"),
  ).toBeVisible();
});

test("退出所有设备 lands on the expired terminal with the reason notice (preflight P2-2)", async () => {
  const account = setup();
  render(<CustomerCenterPage account={account} />);
  await screen.findByText("125");
  fireEvent.click(screen.getByRole("tab", { name: "账号设置" }));

  fireEvent.click(screen.getByRole("button", { name: "退出所有设备" }));
  const dialog = await screen.findByRole("dialog", { name: "退出所有设备？" });
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(within(dialog).getByRole("button", { name: "退出所有设备" }));
  // P2-2：会话已被服务端撤销——本地过期并携带说明到终屏，不再发注定 401
  // 的二次 logout（旧实现 onLogout 的 EXPIRED 事件会抢先切屏吞掉说明）。
  await waitFor(() =>
    expect(account.onSessionExpired).toHaveBeenCalledTimes(1),
  );
  expect(account.onLogout).not.toHaveBeenCalled();
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

test("403 on default initialization is remembered: no replay on reload (preflight P2-6)", async () => {
  // 上线前检查 P2-6：allow_api_keys=false 的子账号列表恒无 default key、
  // 初始化恒 403——本次挂载内不得重放注定失败的 POST。
  const account = setup();
  mocks.list.mockResolvedValue({ items: [], total: 0 });
  mocks.initialize.mockRejectedValue(
    new CustomerApiError({
      message: "权限受限：该子账号不允许创建 API Token。",
      status: 403,
      code: "API_KEYS_NOT_ALLOWED",
    }),
  );
  render(<CustomerCenterPage account={account} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("权限受限");
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  // 重新加载后列表重读（list 第二次被调），但 initialize 不再重放。
  await waitFor(() =>
    expect(mocks.list.mock.calls.length).toBeGreaterThanOrEqual(2),
  );
  expect(mocks.initialize).toHaveBeenCalledTimes(1);
});
