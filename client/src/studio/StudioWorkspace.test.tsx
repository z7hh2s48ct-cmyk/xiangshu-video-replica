import { readFileSync } from "node:fs";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createReviewData, createReviewState, reviewUser } from "./fixtures";
import { StudioWorkspace } from "./StudioWorkspace";
import { createState } from "./state";

async function findEnabledButton(name: string, container?: HTMLElement) {
  const queries = container ? within(container) : screen;
  const button = await queries.findByRole("button", { name });
  await waitFor(() => expect(button).toBeEnabled());
  return button;
}

// LEFTOVER-ON-OPEN：云端草稿不再自动回填，回填入口是首屏这条提示上的按钮。
const RESTORE = "恢复上次内容";
const DISCARD = "放弃";

/** 落在提示窗口内的草稿时间（TTL 见 StudioWorkspace 的 DRAFT_PROMPT_MAX_AGE_MS）。 */
function freshUpdatedAt() {
  return new Date(Date.now() - 60_000).toISOString();
}

async function acceptRestorePrompt() {
  fireEvent.click(await screen.findByRole("button", { name: RESTORE }));
}

const live = vi.hoisted(() => ({
  CREATION_KIND_LABELS: {
    replica: "视频复刻",
    independent: "独立创作",
    replacement: "人物置换",
  },
  loadStudioData: vi.fn(),
  readAudioDuration: vi.fn(async (): Promise<number> => 42),
  studioAssetFromMaterial: vi.fn((item: Record<string, unknown>) => ({
    id: item.asset_id,
    assetId: item.asset_id,
    materialId: item.id,
    name: item.title,
    kind: item.media_type,
    duration: "00:42",
    group: item.group,
    source: "我的上传",
    saved: item.saved,
    allowedUses: item.allowed_uses,
  })),
  loadViralVideos: vi.fn(async () => ({
    videos: [] as ReturnType<typeof createReviewData>["videos"],
    errors: [],
  })),
  loadPersonAssets: vi.fn(),
  loadProjectDraft: vi.fn(),
  reloadTasks: vi.fn(async (): Promise<unknown[]> => []),
  reloadStats: vi.fn(async (): Promise<unknown> => null),
  // C7 云端草稿：默认无草稿/空列表，具体用例再覆盖。
  loadCloudDraft: vi.fn(async (): Promise<unknown> => undefined),
  discardCloudDraft: vi.fn(async (): Promise<void> => {}),
  loadDraftMaterials: vi.fn(async () => ({
    assets: [] as unknown[],
    unavailableIds: [] as string[],
  })),
  loadSavedScriptList: vi.fn(async (): Promise<unknown[]> => []),
  loadLatestScriptFromUpload: vi.fn(async (): Promise<unknown> => null),
  persistCloudDraft: vi.fn(async (_draft: unknown): Promise<void> => {}),
  persistSavedScript: vi.fn(
    async (_script: unknown, _sourceProjectId?: string): Promise<void> => {},
  ),
  publishScriptVersion: vi.fn(
    async (_projectId: string, _text: string): Promise<boolean> => true,
  ),
  extractScriptFromUpload: vi.fn(
    async (
      _projectId: string,
      _assetId: string,
    ): Promise<{ text: string }> => ({
      text: "",
    }),
  ),
  extractViralCopy: vi.fn(),
}));
vi.mock("./live", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...live,
}));

// 视频生成（C2）：只覆盖新引入的四个 api 出口，其余保持原模块行为，
// 避免既有用例（不触发这些函数）受 mock 影响。
const api = vi.hoisted(() => ({
  createViralImportTask: vi.fn(),
  completeMaterialUpload: vi.fn(),
  createMaterialUploadIntent: vi.fn(),
  customerGetWallet: vi.fn(),
  getWallet: vi.fn(),
  getIndependentCapabilities: vi.fn(
    async (): Promise<{
      extended_modes_enabled: boolean;
      t2v_enabled: boolean;
      i2v_enabled: boolean;
      r2v_enabled: boolean;
      last_frame_enabled: boolean;
      max_reference_images: number;
      max_reference_videos: number;
      max_reference_audios: number;
      max_quantity: number;
    }> => ({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    }),
  ),
  getGenerationPriceQuote: vi.fn(
    async (): Promise<{
      resolution: string;
      duration_seconds: number;
      quantity: number;
      unit_price_fen_per_second: number;
      estimated_seconds: number;
      estimated_price_fen: number;
    }> => ({
      resolution: "768P",
      duration_seconds: 8,
      quantity: 1,
      unit_price_fen_per_second: 120,
      estimated_seconds: 8,
      estimated_price_fen: 960,
    }),
  ),
  getSettings: vi.fn(async () => ({
    providers: {
      hifly: { provider: "hifly", configured: false, config: {} },
    },
    runtime: {
      max_generation_count_per_batch: 5,
      max_concurrent_h3_tasks: 2,
      active_storage_provider: "local",
    },
    billing: {
      internal_base_unit_price_fen: 1000,
      charged_unit_price_fen: 1000,
      oral_unit_price_fen: 1800,
      min_recharge_fen: 10000,
      recharge_step_fen: 1000,
    },
  })),
  uploadMaterial: vi.fn(),
  getOralPrice: vi.fn(async () => ({ unit_price_fen: 500 })),
  createOralTask: vi.fn(),
  createIndependentVideoTask: vi.fn(),
  listUserSavedPrompts: vi.fn(async (): Promise<unknown[]> => []),
  listMaterials: vi.fn(),
  getAssetDownloadUrl: vi.fn(),
  getLatestScriptRewriteTask: vi.fn(async (): Promise<unknown> => null),
  getLatestProjectShotCards: vi.fn(async () => null),
  getLatestProjectAnalysis: vi.fn(async () => null),
  getLatestGenerationPrompt: vi.fn(async () => ({
    version: null,
    stale: false,
  })),
  getLatestScriptVersion: vi.fn(async () => ({ version: null, stale: false })),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...api,
}));

const livePanel = vi.hoisted(() => ({
  props: vi.fn(),
  project: {
    id: "project-1",
    owner_user_id: "review-user",
    name: "张工预算项目",
    status: "ACTIVE",
    reference_asset_id: "asset-1",
    reference_upload_status: "READY",
    analysis_status: "READY",
  },
}));
vi.mock("./LiveWorkspacePanel", () => ({
  LiveWorkspacePanel: (props: {
    handoffBatch?: { id: string } | null;
    onClose: () => void;
    onHandoffConsumed?: () => void;
    onProjectSelected: (project: typeof livePanel.project) => void;
    characterIdentityId?: string;
    panel?: string;
  }) => {
    livePanel.props(props);
    return (
      <section aria-label="模拟已有功能工作区">
        <button
          type="button"
          onClick={() => props.onProjectSelected(livePanel.project)}
        >
          生成测试项目
        </button>
        <button
          type="button"
          onClick={() => props.onProjectSelected(livePanel.project)}
        >
          查看测试项目
        </button>
        {props.handoffBatch ? (
          <span>存在交接批次</span>
        ) : (
          <span>没有交接批次</span>
        )}
        {props.onHandoffConsumed ? (
          <button type="button" onClick={props.onHandoffConsumed}>
            消费交接批次
          </button>
        ) : null}
        <button type="button" onClick={props.onClose}>
          返回新工作台
        </button>
      </section>
    );
  },
}));

describe("V1.4 workspace integration", () => {
  // 假 token 放命名常量，避免密钥扫描把 token: 后跟引号字面量误判为真实凭据
  // （与 RootApp.test.tsx 的既定惯例一致）。
  const olderSessionTokenText = "older-token";
  const newerSessionTokenText = "newer-token";
  const customerStore = (token: string | null) => ({
    loadDeviceCredentialToken: vi.fn().mockResolvedValue(null),
    loadSessionToken: vi.fn().mockResolvedValue(token),
    saveActivation: vi.fn().mockResolvedValue(undefined),
    saveSessionToken: vi.fn().mockResolvedValue(undefined),
    clearSessionToken: vi.fn().mockResolvedValue(undefined),
    clearAllCredentials: vi.fn().mockResolvedValue(undefined),
    deviceInstanceId: vi.fn().mockResolvedValue("device-1"),
    devicePlatform: () => "windows",
    // CW-062：身份缓存不参与这些用例的断言，给出满足接口的最小桩。
    loadIdentity: async () => null,
    // 「记住密码」在这些用例里不参与断言，给出满足接口的最小桩。
    loadRememberedLogin: async () => null,
    saveRememberedLogin: async () => {},
    clearRememberedLogin: async () => {},
  });

  const customerAccount = (store: ReturnType<typeof customerStore>) => ({
    devices: null,
    deviceError: "",
    onApprovePairing: vi.fn(),
    onDismissPairing: vi.fn(),
    onProfileUpdated: vi.fn(),
    onRefreshProfile: vi.fn().mockResolvedValue(undefined),
    onLogout: vi.fn().mockResolvedValue(undefined),
    onRefreshDevices: vi.fn().mockResolvedValue(undefined),
    onUnbind: vi.fn(),
    onUpdateProfile: vi.fn(),
    profile: {
      user_id: "customer-a",
      username: "customer-a",
      display_name: "客户甲",
      joined_at: "2026-09-01T08:00:00Z",
      activation_code_masked: null,
      activation_status: "ACTIVE",
      activated_at: "2026-09-01T08:00:00Z",
      device_slots_used: 1,
      device_slots_total: 2,
      // CW-062：母账号身份（无 parent）。
      account_type: "MASTER",
      parent_user_id: null,
      parent_display_name: null,
    },
    profileLoadError: "",
    store,
    onSessionExpired: vi.fn(),
  });

  it("账户页读取真实钱包，并把零余额明确显示为 0 积分", async () => {
    api.customerGetWallet.mockResolvedValue({
      available_credits: 0,
      reserved_credits: 0,
      internal_unit_price_fen: 100,
      min_recharge_fen: 100,
      recharge_step_fen: 100,
    });
    const account = customerAccount(customerStore("session-token"));
    render(
      <StudioWorkspace
        currentUser={{
          id: "customer-a",
          username: "customer-a",
          display_name: "客户甲",
          role: "customer",
        }}
        customerWallet={account}
        initialState={createState("profile")}
      />,
    );

    expect(await screen.findByText("0 积分")).toBeInTheDocument();
    expect(screen.getByText("0 积分")).toBeInTheDocument();
    const accountButton = screen.getByRole("button", {
      name: "用户档案，customer-a",
    });
    expect(accountButton).toHaveTextContent("customer-a");
    expect(accountButton).not.toHaveTextContent("积分");
  });

  it("内部账户页沿用已有钱包接口显示积分", async () => {
    api.getWallet.mockResolvedValue({
      available_credits: 21,
      reserved_credits: 0,
      internal_unit_price_fen: 100,
      min_recharge_fen: 100,
      recharge_step_fen: 100,
    });
    render(
      <StudioWorkspace
        currentUser={{
          id: "employee-a",
          username: "employee-a",
          display_name: "员工甲",
          role: "employee",
        }}
        initialState={createState("profile")}
      />,
    );

    expect(await screen.findByText("21 积分")).toBeInTheDocument();
    expect(api.getWallet).toHaveBeenCalledOnce();
  });

  it("任务轮询同步钱包，后台结算后账户页不需要重新登录", async () => {
    const intervals = vi.spyOn(window, "setInterval");
    api.getWallet.mockResolvedValue({
      available_credits: 21,
      reserved_credits: 0,
    });
    render(
      <StudioWorkspace
        currentUser={{ ...reviewUser, role: "employee" }}
        initialState={createState("profile")}
      />,
    );
    await screen.findByText("21 积分");
    api.getWallet.mockResolvedValue({
      available_credits: 16,
      reserved_credits: 0,
    });
    const callback = intervals.mock.calls.find(
      ([, delay]) => delay === 20_000,
    )?.[0];
    if (typeof callback !== "function")
      throw new Error("task polling was not scheduled");
    await act(async () => {
      callback();
    });
    expect(await screen.findByText("16 积分")).toBeVisible();
  });

  it("切换账号后忽略旧钱包的迟到响应", async () => {
    let resolveOlder!: (value: unknown) => void;
    const olderWallet = new Promise((resolve) => {
      resolveOlder = resolve;
    });
    api.customerGetWallet
      .mockReturnValueOnce(olderWallet)
      .mockResolvedValueOnce({
        available_credits: 8,
        reserved_credits: 0,
        internal_unit_price_fen: 100,
        min_recharge_fen: 100,
        recharge_step_fen: 100,
      });
    const olderAccount = customerAccount(customerStore(olderSessionTokenText));
    const newerAccount = customerAccount(customerStore(newerSessionTokenText));
    newerAccount.profile = { ...newerAccount.profile, user_id: "customer-b" };
    const view = render(
      <StudioWorkspace
        currentUser={{
          id: "customer-a",
          username: "customer-a",
          display_name: "客户甲",
          role: "customer",
        }}
        customerWallet={olderAccount}
        initialState={createState("profile")}
      />,
    );
    await waitFor(() =>
      expect(api.customerGetWallet).toHaveBeenCalledWith({
        kind: "session",
        token: olderSessionTokenText,
      }),
    );
    view.rerender(
      <StudioWorkspace
        currentUser={{
          id: "customer-b",
          username: "customer-b",
          display_name: "客户乙",
          role: "customer",
        }}
        customerWallet={newerAccount}
        initialState={createState("profile")}
      />,
    );

    expect(await screen.findByText("8 积分")).toBeInTheDocument();
    resolveOlder({
      available_credits: 99,
      reserved_credits: 0,
      internal_unit_price_fen: 100,
      min_recharge_fen: 100,
      recharge_step_fen: 100,
    });
    await act(async () => Promise.resolve());
    expect(await screen.findByText("8 积分")).toBeInTheDocument();
  });

  it("账户页钱包读取失败与未知余额明确区分", async () => {
    api.customerGetWallet
      .mockRejectedValueOnce(new Error("wallet offline"))
      .mockResolvedValueOnce({
        available_credits: 12,
        reserved_credits: 0,
        internal_unit_price_fen: 100,
        min_recharge_fen: 100,
        recharge_step_fen: 100,
      });
    const account = customerAccount(customerStore("session-token"));
    render(
      <StudioWorkspace
        currentUser={{
          id: "customer-a",
          username: "customer-a",
          display_name: "客户甲",
          role: "customer",
        }}
        customerWallet={account}
        initialState={createState("profile")}
      />,
    );

    await waitFor(() => expect(api.customerGetWallet).toHaveBeenCalledOnce());
    expect(await screen.findByText("读取失败")).toBeInTheDocument();
    expect(screen.getByText("读取失败")).toBeInTheDocument();
  });

  it("账户资料失败入口透传既有资料刷新操作", async () => {
    api.customerGetWallet.mockResolvedValue({
      available_credits: 5,
      reserved_credits: 0,
      internal_unit_price_fen: 100,
      min_recharge_fen: 100,
      recharge_step_fen: 100,
    });
    const account = customerAccount(customerStore("session-token"));
    account.profileLoadError = "账号资料加载失败，请稍后重试。";
    render(
      <StudioWorkspace
        currentUser={{
          id: "customer-a",
          username: "customer-a",
          display_name: "客户甲",
          role: "customer",
        }}
        customerAccount={account}
        initialState={createState("profile")}
      />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "重试加载账号" }),
    );
    expect(account.onRefreshProfile).toHaveBeenCalledOnce();
  });

  it("退出后的空会话不读取钱包，并转交既有会话失效处理", async () => {
    const account = customerAccount(customerStore(null));
    render(
      <StudioWorkspace
        currentUser={{
          id: "customer-a",
          username: "customer-a",
          display_name: "客户甲",
          role: "customer",
        }}
        customerAccount={account}
        initialState={createState("workbench")}
      />,
    );

    await waitFor(() =>
      expect(account.onSessionExpired).toHaveBeenCalledOnce(),
    );
    expect(api.customerGetWallet).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "用户档案，customer-a" }),
    ).toBeInTheDocument();
  });
  it("展开导航后可直接关闭并恢复入口焦点，无需切换当前业务", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );
    const toggle = screen.getByRole("button", { name: "展开导航" });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name: "关闭导航" }));
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveFocus();
    expect(screen.getByRole("heading", { name: /工作台/ })).toBeInTheDocument();
  });

  it("桌面端默认展开侧边栏，可通过顶栏按钮收起并写入记忆", () => {
    const { container } = render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );
    const shell = container.querySelector(".studio-shell");
    expect(shell?.className).not.toContain("studio-shell--sidebar-collapsed");
    const toggle = screen.getByRole("button", { name: "收起侧边栏" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(shell?.className).toContain("studio-shell--sidebar-collapsed");
    expect(screen.getByRole("button", { name: "展开侧边栏" })).toBe(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(window.localStorage.getItem("studio.sidebar.collapsed")).toBe("1");
  });

  it("收起状态跨会话记忆，可再次展开并清除记忆", () => {
    window.localStorage.setItem("studio.sidebar.collapsed", "1");
    const { container } = render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );
    const shell = container.querySelector(".studio-shell");
    expect(shell?.className).toContain("studio-shell--sidebar-collapsed");
    const toggle = screen.getByRole("button", { name: "展开侧边栏" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(shell?.className).not.toContain("studio-shell--sidebar-collapsed");
    expect(screen.getByRole("button", { name: "收起侧边栏" })).toBe(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(window.localStorage.getItem("studio.sidebar.collapsed")).toBe("0");
  });

  it("保持共享壳层尺寸稳定，避免路由切换时 Logo 和标题跳动", () => {
    const { container } = render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );
    const shell = container.querySelector(".studio-shell");
    const main = container.querySelector(".studio-main");
    const sidebar = container.querySelector(".studio-sidebar");

    expect(shell?.className).not.toContain("studio-route-");
    expect(main).toHaveClass("studio-route-workbench");
    expect(sidebar?.closest("[class*='studio-route-']")).toBeNull();

    fireEvent.click(
      within(screen.getByRole("navigation", { name: "主要导航" })).getByRole(
        "button",
        { name: /任务中心/ },
      ),
    );

    expect(shell?.className).not.toContain("studio-route-");
    expect(main).toHaveClass("studio-route-tasks");
    expect(sidebar?.closest("[class*='studio-route-']")).toBeNull();
  });

  it("所有工作区共用可切换的侧边栏：默认展开、收起态为 88px 图标栏", () => {
    const studioStyles = readFileSync("src/studio/studio.css", "utf8");

    // 默认展开：基础变量保持 224px，不再按屏宽强制收窄
    expect(studioStyles).toMatch(
      /\.studio-shell\s*\{\s*[^}]*--studio-sidebar:\s*224px;/,
    );
    // 收起态由 class 驱动（顶栏开关 + localStorage 记忆），仅桌面端生效
    expect(studioStyles).toMatch(
      /@media \(min-width: 801px\)[\s\S]*?\.studio-shell--sidebar-collapsed\s*\{\s*--studio-sidebar:\s*88px;/,
    );
    expect(studioStyles).toContain(
      ".studio-shell--sidebar-collapsed .studio-sidebar",
    );
    expect(studioStyles).not.toContain(
      ".studio-shell--creation .studio-sidebar",
    );
  });

  it("视频创作页仅在侧边栏显示一次品牌，页头保留通用操作", () => {
    const { container } = render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("replica")}
      />,
    );

    const topbar = container.querySelector(".studio-topbar");
    expect(topbar).not.toBeNull();
    expect(within(topbar as HTMLElement).queryByAltText("众墅之家")).toBeNull();
    expect(screen.getAllByAltText("众墅之家")).toHaveLength(1);
    expect(
      within(topbar as HTMLElement).getByRole("textbox", {
        name: "搜索工作区",
      }),
    ).toBeInTheDocument();
    expect(
      within(topbar as HTMLElement).getByRole("button", {
        name: "用户档案",
      }),
    ).toBeInTheDocument();
  });

  it("左下角与右上角使用同一个账号头像", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );

    const accountAvatar = screen
      .getByRole("button", { name: "用户档案，review" })
      .querySelector("img");
    const topAvatar = screen
      .getByRole("button", { name: "用户档案" })
      .querySelector("img");

    expect(accountAvatar).not.toBeNull();
    expect(accountAvatar?.getAttribute("src")).toBe(
      topAvatar?.getAttribute("src"),
    );
  });

  it("publishing accounts stay in account settings, not the publishing editor", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("profile")}
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "发布账号" }));
    expect(
      screen.getByRole("heading", { name: "发布账号管理" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "用户档案" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "发布管理" }),
    ).not.toBeInTheDocument();
  });
  beforeEach(() => {
    // F-06 本地草稿会跨用例残留（防抖写入 localStorage），逐用例隔离
    window.localStorage.clear();
    vi.clearAllMocks();
    live.loadProjectDraft.mockReset();
    api.createViralImportTask.mockReset();
    live.loadViralVideos.mockResolvedValue({ videos: [], errors: [] });
    api.customerGetWallet.mockReset();
    api.getWallet.mockReset();
    api.getWallet.mockRejectedValue(new Error("internal wallet unavailable"));
    live.loadStudioData.mockResolvedValue({
      people: [],
      assets: [],
      materials: [],
      videos: [],
      tasks: [],
      projects: [],
      errors: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    });
    live.loadPersonAssets.mockResolvedValue({ assets: [], errors: [] });
    api.listMaterials.mockResolvedValue({
      items: [],
      page: 1,
      page_size: 12,
      total: 0,
    });
    api.getAssetDownloadUrl.mockImplementation(async (assetId: string) => ({
      url: `https://signed.example/${assetId}.mp3`,
    }));
    window.history.replaceState(null, "", "/#studio/workbench");
  });

  it("重新进入形象照片页读取新场景，不能永久复用首次空列表", async () => {
    const data = createReviewData();
    live.loadStudioData.mockResolvedValue({
      ...data,
      assets: data.assets.filter((asset) => asset.composite),
    });
    live.loadPersonAssets
      .mockResolvedValueOnce({ assets: [], errors: [], loaded: 0, total: 0 })
      .mockResolvedValue({
        assets: [
          {
            id: "new-scene",
            personId: "zhang",
            name: "新完成办公室场景",
            kind: "image",
            source: "人物库场景造型",
            group: "场景形象照",
            saved: true,
          },
        ],
        errors: [],
        loaded: 1,
        total: 1,
      });
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("person-photos")}
      />,
    );
    await waitFor(() => expect(live.loadPersonAssets).toHaveBeenCalledTimes(1));
    fireEvent.click(await screen.findByRole("tab", { name: "IP 定位" }));
    fireEvent.click(await screen.findByRole("tab", { name: "形象照片" }));
    expect(
      await screen.findByText("新完成办公室场景", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(live.loadPersonAssets).toHaveBeenCalledTimes(2);
  });
  it("同账号资料刷新不重置已载入场景与媒体预览", async () => {
    const data = createReviewData();
    live.loadStudioData.mockResolvedValue({
      ...data,
      assets: data.assets.filter((asset) => asset.composite),
    });
    live.loadPersonAssets.mockResolvedValue({
      assets: [
        {
          id: "scene-after-refresh",
          personId: "zhang",
          name: "联调场景",
          kind: "image",
          source: "人物库场景造型",
          group: "场景形象照",
          saved: true,
        },
      ],
      errors: [],
      loaded: 1,
      total: 1,
    });
    const state = createReviewState("person-photos");
    state.draft.imageId = "scene-after-refresh";
    const view = render(
      <StudioWorkspace currentUser={reviewUser} initialState={state} />,
    );
    expect(
      await screen.findByText("联调场景", { selector: "strong" }),
    ).toBeVisible();
    view.rerender(
      <StudioWorkspace currentUser={{ ...reviewUser }} initialState={state} />,
    );
    expect(live.loadStudioData).toHaveBeenCalledTimes(1);
    expect(live.loadPersonAssets).toHaveBeenCalledTimes(1);
    expect(
      await screen.findByText("联调场景", { selector: "strong" }),
    ).toBeVisible();
  });
  it("renders the approved navigation order and keeps review data isolated", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("workbench")}
      />,
    );
    const nav = within(screen.getByRole("navigation", { name: "主要导航" }));
    expect(nav.getAllByRole("button").map((node) => node.textContent)).toEqual(
      expect.arrayContaining(["爆款视频", "文案工坊", "视频创作"]),
    );
    expect(screen.getByText("粘贴一条爆款乡墅视频链接，")).toBeInTheDocument();
    expect(screen.getByText("快速生成它的原创视频")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "用户档案，review" }),
    ).toBeInTheDocument();
    expect(live.loadStudioData).not.toHaveBeenCalled();
    expect(nav.queryByRole("button", { name: "系统设置" })).toBeNull();
    fireEvent.click(nav.getByRole("button", { name: "文案工坊" }));
    fireEvent.click(screen.getByRole("button", { name: "用于数字人口播" }));
    expect(screen.getByText("张工本人音色 V1")).toBeInTheDocument();
    expect(screen.getByText(/去文案工坊修改/)).toBeInTheDocument();
  });
  it("任务详情与人物子页把对象和返回位置写入可恢复 URL", async () => {
    const reviewData = createReviewData();
    reviewData.tasks = [
      {
        ...reviewData.tasks[0],
        id: "batch-deep",
        backendKind: "generation_batch",
        backendId: "batch-deep",
        batchId: "batch-deep",
        status: "completed",
      },
    ];
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={reviewData}
        initialState={createReviewState("tasks")}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /查看详情|查看结果/ }));
    expect(window.location.hash).toBe(
      "#studio/task-detail/generation_batch/batch-deep?returnTo=tasks",
    );

    window.history.pushState(
      null,
      "",
      "#studio/person-voices?person=zhang&returnTo=oral",
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
    expect(
      await screen.findByRole("heading", { name: "张工" }),
    ).toBeInTheDocument();
    expect(screen.getByText("← 返回创作")).toBeInTheDocument();
  });
  it("素材选择器已加载的尾帧预览在选中后保留到创作页面", async () => {
    const material = {
      id: "tail-cloud",
      assetId: "tail-cloud",
      name: "云端尾帧",
      kind: "image" as const,
      source: "我的上传",
      group: "尾帧素材",
      saved: true,
    };
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      assets: [],
      materials: [material],
    });
    api.getAssetDownloadUrl.mockResolvedValue({
      url: "https://signed.example/tail.png",
    });
    const state = createState("video");
    render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
    await waitFor(() => expect(live.loadStudioData).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "添加尾帧" }));
    fireEvent.click(screen.getByRole("button", { name: "从素材库选择" }));
    expect(
      await screen.findByRole("img", { name: "云端尾帧" }),
    ).toHaveAttribute("src", "https://signed.example/tail.png");
    fireEvent.click(screen.getByRole("button", { name: /云端尾帧.*我的上传/ }));
    expect(await screen.findByRole("img", { name: "尾帧" })).toHaveAttribute(
      "src",
      "https://signed.example/tail.png",
    );
  });

  it("管理员可以从新版工作区进入服务设置", async () => {
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    render(
      <StudioWorkspace
        currentUser={{ ...reviewUser, role: "admin" }}
        initialState={createReviewState("workbench")}
      />,
    );

    const nav = within(screen.getByRole("navigation", { name: "主要导航" }));
    fireEvent.click(nav.getByRole("button", { name: "系统设置" }));

    expect(
      await screen.findByRole("region", { name: "服务设置" }),
    ).toBeInTheDocument();
    expect(api.getSettings).toHaveBeenCalledTimes(1);
    expect(window.location.hash).toBe("#studio/settings");
  });
  it("普通导航清除旧详情返回位置且每个详情入口写入当前来源", () => {
    const reviewData = createReviewData();
    reviewData.tasks = [
      {
        ...reviewData.tasks[0],
        id: "batch-review-1",
        backendKind: "generation_batch",
        backendId: "batch-review-1",
        batchId: "batch-review-1",
        status: "completed",
      },
      {
        ...reviewData.tasks[0],
        id: "batch-review-2",
        backendKind: "generation_batch",
        backendId: "batch-review-2",
        batchId: "batch-review-2",
        status: "completed",
      },
    ];
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={reviewData}
        initialState={createReviewState("analytics")}
      />,
    );

    fireEvent.click(screen.getAllByRole("button", { name: "查看视频" })[0]);
    expect(window.location.hash).toContain("returnTo=analytics");
    fireEvent.click(screen.getByRole("button", { name: "任务中心" }));
    expect(window.location.hash).toBe("#studio/tasks");
    fireEvent.click(screen.getAllByRole("button", { name: "查看结果" })[1]);
    expect(window.location.hash).toBe(
      "#studio/task-detail/generation_batch/batch-review-2?returnTo=tasks",
    );
  });
  it("changing IP invalidates the previous person's voice and avatar", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更换 IP" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: /李总/ }),
    );
    expect(screen.queryByText("张工本人音色 V1")).not.toBeInTheDocument();
    expect(screen.getByText("待确认终稿")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成口播视频" })).toBeDisabled();
  });
  it("cancelling a picker leaves the original draft intact", () => {
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        reviewData={createReviewData()}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "更换 IP" }));
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "关闭" }),
    );
    expect(screen.getByText("张工本人音色 V1")).toBeInTheDocument();
  });

  it("loads real data without falling back to review examples", async () => {
    live.loadStudioData.mockResolvedValue({
      people: [],
      assets: [],
      videos: [],
      tasks: [],
      projects: [],
      errors: ["人物库暂不可用"],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    });
    render(<StudioWorkspace currentUser={reviewUser} />);
    await waitFor(() => expect(live.loadStudioData).toHaveBeenCalled());
    expect(screen.getByText(/人物库暂不可用/)).toBeInTheDocument();
    expect(screen.queryByText("张工")).not.toBeInTheDocument();
    expect(screen.queryByText(/示例审核/)).not.toBeInTheDocument();
  });

  it.each(["生成测试项目", "查看测试项目"])(
    "%s 导入后进入同一个复刻页并展开项目流程",
    async (entry) => {
      const imported = createReviewState("workbench").draft;
      imported.projectId = livePanel.project.id;
      live.loadStudioData.mockResolvedValue({
        ...createReviewData(),
        projects: [livePanel.project],
        loading: false,
      });
      live.loadProjectDraft.mockResolvedValue({ draft: imported, errors: [] });
      render(<StudioWorkspace currentUser={reviewUser} />);

      fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
      fireEvent.click(screen.getByRole("button", { name: entry }));

      // 拆解控制头部右侧显示当前项目名（复刻页曾整个丢掉项目名）。
      expect(await screen.findByText("拆解控制")).toBeInTheDocument();
      expect(screen.getByText("张工预算项目")).toBeInTheDocument();
      expect(screen.queryByText("先导入参考视频")).not.toBeInTheDocument();
      expect(
        screen.queryByLabelText("模拟已有功能工作区"),
      ).not.toBeInTheDocument();
      expect(window.location.hash).toBe("#studio/replica");
    },
  );

  it("项目导入失败时留在列表、保留草稿并可重试", async () => {
    const initialState = createState("workbench");
    initialState.draft.script.text = "不要覆盖的草稿";
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      projects: [livePanel.project],
      loading: false,
    });
    const importedAfterRetry = createReviewState("replica").draft;
    importedAfterRetry.projectId = livePanel.project.id;
    live.loadProjectDraft
      .mockRejectedValueOnce(new Error("暂时失败"))
      .mockResolvedValueOnce({
        draft: importedAfterRetry,
        errors: [],
      });
    render(
      <StudioWorkspace currentUser={reviewUser} initialState={initialState} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    fireEvent.click(screen.getByRole("button", { name: "生成测试项目" }));

    expect(await screen.findByText("暂时失败")).toBeInTheDocument();
    expect(screen.getByLabelText("模拟已有功能工作区")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回新工作台" }));
    fireEvent.click(screen.getByRole("button", { name: "文案工坊" }));
    expect(screen.getByDisplayValue("不要覆盖的草稿")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "工作台" }));
    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    fireEvent.click(screen.getByRole("button", { name: "生成测试项目" }));
    await waitFor(() => expect(live.loadProjectDraft).toHaveBeenCalledTimes(2));
    // 重试成功后同样以「拆解控制」作为复刻页已按项目展开的标记。
    expect(await screen.findByText("拆解控制")).toBeInTheDocument();
  });

  it("关闭项目列表后忽略迟到的导入结果且不覆盖草稿", async () => {
    const imported = createReviewState("workbench").draft;
    imported.projectId = livePanel.project.id;
    imported.script.text = "迟到结果";
    let resolveImport:
      | ((value: { draft: typeof imported; errors: string[] }) => void)
      | undefined;
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    live.loadProjectDraft.mockReturnValue(
      new Promise((resolve) => {
        resolveImport = resolve;
      }),
    );
    const initialState = createState("workbench");
    initialState.draft.script.text = "当前草稿";
    render(
      <StudioWorkspace currentUser={reviewUser} initialState={initialState} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
    fireEvent.click(screen.getByRole("button", { name: "生成测试项目" }));
    await waitFor(() => expect(live.loadProjectDraft).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "返回新工作台" }));
    await act(async () => {
      resolveImport?.({ draft: imported, errors: [] });
    });

    expect(
      screen.queryByText("来源视频 · 张工预算项目"),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "文案工坊" }));
    expect(screen.getByDisplayValue("当前草稿")).toBeInTheDocument();
  });

  it("workbench metric cards show real platform stats", async () => {
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
      stats: {
        today_completed: 5,
        running: 2,
        queued: 1,
        needs_attention: 4,
        total_completed: 42,
      },
    });
    render(<StudioWorkspace currentUser={reviewUser} />);

    await waitFor(() => expect(screen.getByText("5")).toBeInTheDocument());
    // 队列 = running + queued（3）；待处理来自统计而非 20 条切片。
    expect(screen.getByText("4")).toBeInTheDocument();
  });

  it("polls generation task progress silently while the workspace is open", async () => {
    vi.useFakeTimers();
    const runningTask = {
      id: "batch-9",
      batchId: "batch-9",
      title: "乡墅批次一",
      type: "视频生成" as const,
      status: "running" as const,
      progress: 45,
      submitted: "2026-09-06T09:00:00Z",
    };
    live.loadStudioData.mockResolvedValue({
      people: [],
      assets: [],
      videos: [],
      projects: [],
      errors: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
      tasks: [runningTask],
    });
    live.reloadTasks.mockResolvedValue([
      { ...runningTask, status: "completed" as const, progress: 100 },
    ]);

    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("tasks")}
      />,
    );
    await vi.waitFor(() =>
      expect(screen.getByText("生成中 45%")).toBeInTheDocument(),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });

    expect(live.reloadTasks).toHaveBeenCalledWith(reviewUser);
    // "已完成" appears as both the filter tab and the refreshed row status.
    expect(screen.getAllByText("已完成")).toHaveLength(2);
    expect(screen.queryByText("生成中 45%")).not.toBeInTheDocument();
  });

  describe("C7 云端草稿", () => {
    // 前面的轮询用例开启了 fake timers 且不恢复；本组用例的 waitFor 依赖
    // 真实 setTimeout，先显式切回，防止用例间定时器状态泄漏。
    beforeEach(() => {
      vi.useRealTimers();
      window.localStorage.clear();
    });

    const emptyStudioData = {
      people: [],
      assets: [],
      videos: [],
      projects: [],
      tasks: [],
      errors: [],
      loading: false,
      stats: null,
      analytics7: null,
      analytics30: null,
    };

    function restoredDraft() {
      const draft = createState("copy").draft;
      draft.script.title = "云端恢复的标题";
      draft.script.text = "云端恢复的文案内容";
      draft.script.confirmed = true;
      return draft;
    }

    async function openCopyPage() {
      fireEvent.click(screen.getByRole("button", { name: "文案工坊" }));
      await act(async () => {
        await Promise.resolve();
      });
      const manual = screen.queryByRole("button", { name: "手动写稿" });
      if (manual && !(manual as HTMLButtonElement).disabled)
        fireEvent.click(manual);
      await waitFor(() =>
        expect(screen.getByLabelText("二创文案")).toBeInTheDocument(),
      );
    }

    it("点「恢复上次内容」后才回填云端草稿，我的文案列表照常加载", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue({
        draft: restoredDraft(),
        updatedAt: freshUpdatedAt(),
      });
      live.loadSavedScriptList.mockResolvedValue([
        {
          id: "saved-1",
          title: "已保存文案",
          original: "",
          text: "已保存的文本",
          version: 2,
          confirmed: false,
        },
      ]);
      render(<StudioWorkspace currentUser={reviewUser} />);

      fireEvent.click(await screen.findByRole("button", { name: RESTORE }));
      await openCopyPage();
      // 恢复是异步 setState：等值到位，而不是等 textarea 出现。
      await waitFor(() =>
        expect(
          (screen.getByLabelText("二创文案") as HTMLTextAreaElement).value,
        ).toBe("云端恢复的文案内容"),
      );
      expect(screen.getByText("终稿 V1")).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
      expect(screen.getByText("已保存文案")).toBeInTheDocument();
      // 未做任何编辑时不触发自动保存。
      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(live.loadDraftMaterials).toHaveBeenCalledWith(
        expect.objectContaining({
          script: expect.objectContaining({ text: "云端恢复的文案内容" }),
        }),
      );
      // 恢复后横幅收起，不再挡着工作区。
      expect(
        screen.queryByRole("button", { name: RESTORE }),
      ).not.toBeInTheDocument();
    });

    describe("LEFTOVER-ON-OPEN 恢复闸门", () => {
      it("有云端草稿时首屏保持空白，只给出恢复提示", async () => {
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        live.loadCloudDraft.mockResolvedValue({
          draft: restoredDraft(),
          updatedAt: freshUpdatedAt(),
        });
        render(<StudioWorkspace currentUser={reviewUser} />);

        await screen.findByRole("button", { name: RESTORE });
        await openCopyPage();
        expect(screen.getByLabelText("二创文案")).toHaveValue("");
        // 没恢复就不该去解析旧草稿引用的素材。
        expect(live.loadDraftMaterials).not.toHaveBeenCalled();
      });

      it("点「放弃」删除云端草稿并清掉本地未保存文本", async () => {
        window.localStorage.setItem(
          `generation.localDraft/script/${encodeURIComponent(reviewUser.id)}/proj-a`,
          JSON.stringify({ text: "上次的口播稿" }),
        );
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        live.loadCloudDraft.mockResolvedValue({
          draft: restoredDraft(),
          updatedAt: freshUpdatedAt(),
        });
        live.discardCloudDraft.mockResolvedValue(undefined);
        render(<StudioWorkspace currentUser={reviewUser} />);

        fireEvent.click(await screen.findByRole("button", { name: DISCARD }));

        await waitFor(() =>
          expect(live.discardCloudDraft).toHaveBeenCalledTimes(1),
        );
        expect(
          window.localStorage.getItem(
            `generation.localDraft/script/${encodeURIComponent(reviewUser.id)}/proj-a`,
          ),
        ).toBeNull();
        await waitFor(() =>
          expect(
            screen.queryByRole("button", { name: DISCARD }),
          ).not.toBeInTheDocument(),
        );
        await openCopyPage();
        expect(screen.getByLabelText("二创文案")).toHaveValue("");
      });

      it("放弃失败时保留提示并说明未清除，避免静默失败", async () => {
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        live.loadCloudDraft.mockResolvedValue({
          draft: restoredDraft(),
          updatedAt: freshUpdatedAt(),
        });
        live.discardCloudDraft.mockRejectedValue(new Error("boom"));
        render(<StudioWorkspace currentUser={reviewUser} />);

        fireEvent.click(await screen.findByRole("button", { name: DISCARD }));

        await screen.findByText(/未能清除上次内容/);
        expect(
          screen.getByRole("button", { name: DISCARD }),
        ).toBeInTheDocument();
      });

      it("超过 7 天的旧草稿不再打扰，也不擅自删除用户内容", async () => {
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        live.loadCloudDraft.mockResolvedValue({
          draft: restoredDraft(),
          updatedAt: new Date(
            Date.now() - 8 * 24 * 60 * 60 * 1000,
          ).toISOString(),
        });
        render(<StudioWorkspace currentUser={reviewUser} />);

        await openCopyPage();
        expect(
          screen.queryByRole("button", { name: RESTORE }),
        ).not.toBeInTheDocument();
        expect(live.discardCloudDraft).not.toHaveBeenCalled();
      });

      it("审计员可恢复查看但不能放弃（服务端同样拒写）", async () => {
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        live.loadCloudDraft.mockResolvedValue({
          draft: restoredDraft(),
          updatedAt: freshUpdatedAt(),
        });
        render(
          <StudioWorkspace
            currentUser={{ ...reviewUser, role: "auditor" as const }}
          />,
        );

        expect(
          await screen.findByRole("button", { name: RESTORE }),
        ).toBeEnabled();
        expect(screen.getByRole("button", { name: DISCARD })).toBeDisabled();
      });

      it("「从空白创作开始」同时清掉云端草稿，下次打开不再回来", async () => {
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        live.loadCloudDraft.mockResolvedValue(undefined);
        live.discardCloudDraft.mockResolvedValue(undefined);
        render(<StudioWorkspace currentUser={reviewUser} />);

        fireEvent.click(await findEnabledButton("新建创作"));
        fireEvent.click(
          await screen.findByRole("button", { name: "从空白创作开始" }),
        );

        await waitFor(() =>
          expect(live.discardCloudDraft).toHaveBeenCalledTimes(1),
        );
      });

      it("原地换账号时收掉上一个账号的提示，改读新账号的草稿", async () => {
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        const firstDraft = restoredDraft();
        firstDraft.script.text = "甲的上次内容";
        live.loadCloudDraft.mockResolvedValue({
          draft: firstDraft,
          updatedAt: freshUpdatedAt(),
        });
        const view = render(<StudioWorkspace currentUser={reviewUser} />);
        await screen.findByRole("button", { name: RESTORE });

        live.loadCloudDraft.mockResolvedValue(undefined);
        view.rerender(
          <StudioWorkspace currentUser={{ ...reviewUser, id: "user-2" }} />,
        );

        await waitFor(() =>
          expect(
            screen.queryByRole("button", { name: RESTORE }),
          ).not.toBeInTheDocument(),
        );
        expect(live.loadCloudDraft).toHaveBeenCalledTimes(2);
      });
    });

    it("编辑二创文案后防抖自动保存到云端", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);

      // 用真实定时器完成渲染与导航（waitFor 依赖真实 setTimeout）。
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "新的二创内容" },
        });
        expect(live.persistCloudDraft).not.toHaveBeenCalled();

        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        const savedDraft = live.persistCloudDraft.mock.calls[0][0] as {
          script: { text: string; confirmed: boolean };
        };
        expect(savedDraft.script.text).toBe("新的二创内容");
        expect(savedDraft.script.confirmed).toBe(false);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存已在途时显式保存排队补写终态草稿", async () => {
      let resolveAutosave: (() => void) | undefined;
      live.persistCloudDraft.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveAutosave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "自动保存与显式保存共用正文" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());
        expect(live.persistSavedScript).toHaveBeenCalledTimes(1);
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        await act(async () => resolveAutosave?.());
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(screen.getAllByText(/已保存到我的文案/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存进行中连续两次显式保存仍发送最新终态草稿", async () => {
      let resolveAutosave: (() => void) | undefined;
      let resolveFinalDraft: (() => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveAutosave = resolve;
          }),
        )
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveFinalDraft = resolve;
          }),
        );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-double-save";
      state.draft.sourceId = "source-double-save";
      state.draft.sourceAssetId = "asset-double-save";
      state.draft.ipId = "identity-double-save";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
      await openCopyPage();

      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "连续保存的终态正文" },
      });
      await waitFor(
        () => expect(live.persistCloudDraft).toHaveBeenCalledTimes(1),
        { timeout: 3_000 },
      );
      const autosaveDraft = live.persistCloudDraft.mock.calls[0]?.[0] as
        | ReturnType<typeof restoredDraft>
        | undefined;

      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      await waitFor(() =>
        expect(live.persistSavedScript).toHaveBeenCalledTimes(2),
      );
      expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

      await act(async () => resolveAutosave?.());
      await waitFor(() =>
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2),
      );
      expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
        projectId: "project-double-save",
        sourceId: "source-double-save",
        sourceAssetId: "asset-double-save",
        quoteRevision: autosaveDraft?.quoteRevision,
        scriptEdited: false,
        script: {
          ipId: "identity-double-save",
          sourceProjectId: "project-double-save",
          sourceKind: "project",
        },
      });

      await act(async () => resolveFinalDraft?.());
      expect(await screen.findAllByText(/换设备登录也能找回/)).toHaveLength(1);
    });

    it("同范围自动保存已成功时显式保存补写终态草稿", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "已经自动保存的正文" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());
        expect(live.persistSavedScript).toHaveBeenCalledTimes(1);
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(screen.getAllByText(/已保存到我的文案/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存后显式保存补齐终态来源且重挂载无需再次保存", async () => {
      let storedDraft: ReturnType<typeof restoredDraft> | undefined;
      live.persistCloudDraft.mockImplementation(async (draft) => {
        storedDraft = structuredClone(
          draft as ReturnType<typeof restoredDraft>,
        );
      });
      live.loadCloudDraft.mockImplementation(async () =>
        storedDraft
          ? {
              draft: structuredClone(storedDraft),
              updatedAt: freshUpdatedAt(),
            }
          : undefined,
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-source";
      state.draft.sourceId = "source-video";
      state.draft.sourceAssetId = "source-asset";
      state.draft.ipId = "identity-source";
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "需要补齐来源的正文" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(storedDraft).toMatchObject({
          projectId: "project-source",
          sourceId: "source-video",
          sourceAssetId: "source-asset",
          scriptEdited: false,
          script: {
            ipId: "identity-source",
            sourceProjectId: "project-source",
            sourceKind: "project",
          },
        });

        vi.useRealTimers();
        view.unmount();
        render(<StudioWorkspace currentUser={reviewUser} />);
        await acceptRestorePrompt();
        await openCopyPage();
        await waitFor(() =>
          expect(
            (screen.getByLabelText("二创文案") as HTMLTextAreaElement).value,
          ).toBe("需要补齐来源的正文"),
        );
        expect(live.loadDraftMaterials).toHaveBeenLastCalledWith(
          expect.objectContaining({
            projectId: "project-source",
            sourceId: "source-video",
            sourceAssetId: "source-asset",
            scriptEdited: false,
          }),
        );
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
      } finally {
        vi.useRealTimers();
      }
    });

    it("账号切换期间串行写入并只保留尚未开始的最新草稿", async () => {
      let resolveFirst: (() => void) | undefined;
      let resolveSecond: (() => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveFirst = resolve;
          }),
        )
        .mockReturnValueOnce(
          new Promise<void>((resolve) => {
            resolveSecond = resolve;
          }),
        );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const accountA = { ...reviewUser, id: "account-a" };
      const accountB = { ...reviewUser, id: "account-b" };
      const view = render(<StudioWorkspace currentUser={accountA} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A第一版" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        view.rerender(<StudioWorkspace currentUser={accountB} />);
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号B待写版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);

        view.rerender(<StudioWorkspace currentUser={accountA} />);
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A最终版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
          resolveFirst?.();
          await Promise.resolve();
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
          script: { text: "账号A最终版本" },
        });
        expect(
          live.persistCloudDraft.mock.calls.some(
            ([draft]) =>
              (draft as ReturnType<typeof restoredDraft>).script.text ===
              "账号B待写版本",
          ),
        ).toBe(false);
        await act(async () => resolveSecond?.());
      } finally {
        vi.useRealTimers();
      }
    });

    it("旧账号写入失败后仍发送当前账号排队的草稿", async () => {
      let rejectA: ((cause: Error) => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((_resolve, reject) => {
            rejectA = reject;
          }),
        )
        .mockResolvedValueOnce(undefined);
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const accountA = { ...reviewUser, id: "failed-account-a" };
      const accountB = { ...reviewUser, id: "current-account-b" };
      const view = render(<StudioWorkspace currentUser={accountA} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A失败版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        view.rerender(<StudioWorkspace currentUser={accountB} />);
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号B当前版本" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
          rejectA?.(new Error("account A offline"));
          await Promise.resolve();
        });

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
          script: { text: "账号B当前版本" },
        });
      } finally {
        vi.useRealTimers();
      }
    });

    it("旧自动保存失败后仍发送排队的确认终稿", async () => {
      let rejectAutosave: ((cause: Error) => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((_resolve, reject) => {
            rejectAutosave = reject;
          }),
        )
        .mockResolvedValueOnce(undefined);
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      render(<StudioWorkspace currentUser={reviewUser} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "等待确认的终稿" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        fireEvent.click(screen.getByRole("button", { name: "确认终稿" }));
        await act(async () => {
          rejectAutosave?.(new Error("autosave offline"));
          await Promise.resolve();
        });

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(live.persistCloudDraft.mock.calls[1]?.[0]).toMatchObject({
          script: { text: "等待确认的终稿", confirmed: true },
        });
      } finally {
        vi.useRealTimers();
      }
    });

    it("复用的自动保存失败时提示草稿待同步并允许显式重试", async () => {
      let rejectAutosave: ((cause: Error) => void) | undefined;
      live.persistCloudDraft
        .mockReturnValueOnce(
          new Promise<void>((_resolve, reject) => {
            rejectAutosave = reject;
          }),
        )
        .mockRejectedValueOnce(new Error("final draft offline"))
        .mockResolvedValueOnce(undefined);
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-retry";
      state.draft.sourceId = "source-retry";
      state.draft.sourceAssetId = "asset-retry";
      state.draft.ipId = "identity-retry";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "首次同步失败后重试" },
        });
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => rejectAutosave?.(new Error("offline")));

        expect(live.persistCloudDraft).toHaveBeenCalledTimes(2);
        expect(
          screen.getByText(/版本已保存，但云端草稿同步失败/),
        ).toBeInTheDocument();
        expect(
          screen.queryByText(/换设备登录也能找回/),
        ).not.toBeInTheDocument();
        expect(
          screen.getByRole("button", { name: "生成二创文案" }),
        ).toBeDisabled();

        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(3);
        expect(live.persistCloudDraft.mock.calls[2]?.[0]).toMatchObject({
          projectId: "project-retry",
          sourceId: "source-retry",
          sourceAssetId: "asset-retry",
          scriptEdited: false,
          script: {
            ipId: "identity-retry",
            sourceProjectId: "project-retry",
            sourceKind: "project",
          },
        });
        expect(screen.getAllByText(/换设备登录也能找回/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("自动保存定时器等待期间账号A到B再回A时不使用迟到认证写入", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const accountA = { ...reviewUser, id: "account-a" };
      const accountB = { ...reviewUser, id: "account-b" };
      const view = render(<StudioWorkspace currentUser={accountA} />);
      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "账号A的待保存内容" },
        });
        view.rerender(<StudioWorkspace currentUser={accountB} />);
        view.rerender(<StudioWorkspace currentUser={accountA} />);

        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).not.toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    });

    it("切换为审计员会取消已排程自动保存且旧回调仍二次拒绝", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const auditor = {
        id: reviewUser.id,
        username: reviewUser.username,
        display_name: "审计员",
        role: "auditor" as const,
      };
      const view = render(<StudioWorkspace currentUser={reviewUser} />);

      await openCopyPage();
      const timeoutSpy = vi.spyOn(window, "setTimeout");
      const clearTimeoutSpy = vi.spyOn(window, "clearTimeout");
      try {
        fireEvent.change(screen.getByLabelText("二创文案"), {
          target: { value: "切换角色前的待保存内容" },
        });
        const scheduledSave = timeoutSpy.mock.calls.find(
          ([, delay]) => delay === 2_000,
        )?.[0] as (() => void) | undefined;
        expect(scheduledSave).toBeTypeOf("function");

        view.rerender(<StudioWorkspace currentUser={auditor} />);
        expect(clearTimeoutSpy).toHaveBeenCalled();
        scheduledSave?.();
        await Promise.resolve();

        expect(live.persistCloudDraft).not.toHaveBeenCalled();
      } finally {
        timeoutSpy.mockRestore();
        clearTimeoutSpy.mockRestore();
      }
    });

    it("保存版本写入云端我的文案", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "要保存的文案";
      state.draft.scriptEdited = true;
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      await waitFor(() => expect(live.persistSavedScript).toHaveBeenCalled());
      const [script] = live.persistSavedScript.mock.calls[0] as unknown as [
        { text: string },
      ];
      expect(script.text).toBe("要保存的文案");
      expect(live.persistSavedScript).toHaveBeenCalledWith(
        expect.objectContaining({
          ipId: "identity-1",
          sourceProjectId: "project-1",
        }),
        "project-1",
        "identity-1",
      );
      expect(await screen.findByText(/已保存到我的文案/)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "生成二创文案" }),
      ).toBeEnabled();
      expect(live.persistCloudDraft).toHaveBeenCalledWith(
        expect.objectContaining({
          projectId: "project-1",
          ipId: "identity-1",
          scriptEdited: false,
        }),
      );
    });

    it("StrictMode下慢云写成功后仅写一次草稿并通知一次", async () => {
      let resolveCloud: (() => void) | undefined;
      live.persistCloudDraft.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveCloud = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "只保存一次的文案";
      state.draft.scriptEdited = true;
      render(
        <StrictMode>
          <StudioWorkspace currentUser={reviewUser} initialState={state} />
        </StrictMode>,
      );

      await openCopyPage();
      vi.useFakeTimers();
      try {
        fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
        await act(async () => Promise.resolve());
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
          resolveCloud?.();
          await Promise.resolve();
          await vi.advanceTimersByTimeAsync(2_000);
        });
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
        expect(screen.getAllByText(/已保存到我的文案/)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it("显式草稿云写等待期间卸载后保持静默且不追加写入", async () => {
      let resolveCloud: (() => void) | undefined;
      live.persistCloudDraft.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveCloud = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.script.text = "卸载前的显式保存";
      state.draft.scriptEdited = true;
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      await waitFor(() =>
        expect(live.persistCloudDraft).toHaveBeenCalledTimes(1),
      );
      view.unmount();
      await act(async () => resolveCloud?.());
      expect(live.persistCloudDraft).toHaveBeenCalledTimes(1);
    });

    it.each(["video", "reference"] as const)(
      "%s 视频草稿直接保存云端而不创建空文案",
      async (page) => {
        live.loadStudioData.mockResolvedValue({
          ...emptyStudioData,
          materials: [],
        });
        live.loadCloudDraft.mockResolvedValue(undefined);
        const state = createState(page);
        state.draft.prompt = "按参考人物生成工地讲解视频";
        state.draft.script.text = "";
        render(
          <StudioWorkspace currentUser={reviewUser} initialState={state} />,
        );
        await screen.findByRole("button", { name: "保存草稿" });
        fireEvent.click(screen.getByRole("button", { name: "保存草稿" }));
        expect(
          await screen.findByText(
            "视频草稿已保存到云端，可继续编辑或换设备恢复。",
          ),
        ).toBeInTheDocument();
        expect(live.persistSavedScript).not.toHaveBeenCalled();
        expect(live.persistCloudDraft.mock.calls[0]?.[0]).toEqual(
          expect.objectContaining({ prompt: state.draft.prompt }),
        );
      },
    );

    it("版本保存失败时仍保持未保存门禁", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.persistSavedScript.mockRejectedValueOnce(new Error("save failed"));
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "asset-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "仍未保存的文案";
      state.draft.scriptEdited = true;
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));

      await waitFor(() =>
        expect(screen.getByText(/云端保存失败/)).toBeInTheDocument(),
      );
      expect(
        screen.getByRole("button", { name: "生成二创文案" }),
      ).toBeEnabled();
      expect(screen.queryByText(/先保存当前编辑/)).not.toBeInTheDocument();
    });

    it("保存等待期间账号A到B再回A时不写草稿、不更新列表且保持静默", async () => {
      let resolveSave: (() => void) | undefined;
      live.persistSavedScript.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "source-1";
      state.draft.sourceAssetId = "source-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "账号隔离正文";
      state.draft.scriptEdited = true;
      const accountA = { ...reviewUser, id: "account-a" };
      const accountB = { ...reviewUser, id: "account-b" };
      const view = render(
        <StudioWorkspace currentUser={accountA} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));

      view.rerender(
        <StudioWorkspace currentUser={accountB} initialState={state} />,
      );
      view.rerender(
        <StudioWorkspace currentUser={accountA} initialState={state} />,
      );
      await act(async () => resolveSave?.());

      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(screen.queryByText(/已保存到我的文案/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
      expect(screen.queryByText("账号隔离正文")).not.toBeInTheDocument();
    });

    it("保存等待期间正文A到B再回A或卸载时不发后续cloud写入", async () => {
      let resolveSave: (() => void) | undefined;
      live.persistSavedScript.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "source-1";
      state.draft.sourceAssetId = "source-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "正文A";
      state.draft.scriptEdited = true;
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));
      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "正文B" },
      });
      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "正文A" },
      });
      view.unmount();
      await act(async () => resolveSave?.());

      expect(live.persistCloudDraft).not.toHaveBeenCalled();
    });

    it("保存等待期间完整草稿被替换时即使脚本字段相同也不回填", async () => {
      let resolveSave: (() => void) | undefined;
      live.persistSavedScript.mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveSave = resolve;
        }),
      );
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.sourceId = "source-1";
      state.draft.ipId = "identity-1";
      state.draft.script.text = "局部字段保持不变";
      state.draft.scriptEdited = true;
      state.draft.prompt = "旧提示词";
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );
      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "保存版本" }));

      state.draft.prompt = "替换后的另一份完整草稿";
      view.rerender(
        <StudioWorkspace
          currentUser={{ ...reviewUser }}
          initialState={state}
        />,
      );
      await act(async () => resolveSave?.());

      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(screen.queryByText(/已保存到我的文案/)).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "我的文案" }));
      expect(screen.queryByText("局部字段保持不变")).not.toBeInTheDocument();
    });

    it("审计员可读取文案但保存和确认均不发写请求", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue({
        draft: restoredDraft(),
        updatedAt: freshUpdatedAt(),
      });
      const auditor = {
        id: "auditor-1",
        username: "auditor-1",
        display_name: "审计员",
        role: "auditor" as const,
      };
      render(<StudioWorkspace currentUser={auditor} />);

      fireEvent.click(await screen.findByRole("button", { name: RESTORE }));
      await openCopyPage();
      await waitFor(() =>
        expect(screen.getByLabelText("二创文案")).toHaveValue(
          "云端恢复的文案内容",
        ),
      );
      expect(screen.getByLabelText("二创文案")).toBeDisabled();
      expect(screen.getByRole("button", { name: "保存版本" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "确认终稿" })).toBeDisabled();
      vi.useFakeTimers();
      try {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000);
        });
      } finally {
        vi.useRealTimers();
      }
      expect(live.persistSavedScript).not.toHaveBeenCalled();
      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(live.publishScriptVersion).not.toHaveBeenCalled();
    });

    it("切换为审计员会关闭已打开的人物选择器", async () => {
      live.loadStudioData.mockResolvedValue({
        ...emptyStudioData,
        materials: [],
      });
      live.loadCloudDraft.mockResolvedValue(undefined);
      const auditor = {
        ...reviewUser,
        display_name: "审计员",
        role: "auditor" as const,
      };
      const view = render(<StudioWorkspace currentUser={reviewUser} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "更换人物" }));
      expect(
        await screen.findByRole("dialog", { name: "选择人物 IP" }),
      ).toBeInTheDocument();

      view.rerender(<StudioWorkspace currentUser={auditor} />);

      await waitFor(() =>
        expect(
          screen.queryByRole("dialog", { name: "选择人物 IP" }),
        ).not.toBeInTheDocument(),
      );
    });

    it("确认终稿立即持久化并带 projectId 时发布到项目脚本版本", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.projectId = "project-1";
      state.draft.script.text = "终稿内容";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "确认终稿" }));
      await waitFor(() =>
        expect(live.publishScriptVersion).toHaveBeenCalledWith(
          "project-1",
          "终稿内容",
        ),
      );
      expect(live.persistCloudDraft).toHaveBeenCalled();
      const savedDraft = live.persistCloudDraft.mock.calls[0][0] as unknown as {
        script: { confirmed: boolean };
      };
      expect(savedDraft.script.confirmed).toBe(true);
    });

    it("确认终稿无项目来源时不发布脚本版本", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("copy");
      state.draft.script.text = "无项目终稿";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      await openCopyPage();
      fireEvent.click(screen.getByRole("button", { name: "确认终稿" }));
      await waitFor(() => expect(live.persistCloudDraft).toHaveBeenCalled());
      expect(live.publishScriptVersion).not.toHaveBeenCalled();
    });

    it.each([false, true])(
      "首页爆款提取使用刚导入的项目素材（已有旧来源：%s）",
      async (hasOldSource) => {
        const source = {
          ...createReviewData().videos[0],
          id: "fresh-viral",
          title: "新导入的爆款",
          homepageFeatured: true,
          platformKey: "douyin" as const,
          nativeId: "fresh-native",
        };
        live.loadStudioData.mockResolvedValue({
          ...emptyStudioData,
          videos: [source],
        });
        live.loadViralVideos.mockResolvedValue({
          videos: [source],
          errors: [],
        });
        live.loadCloudDraft.mockResolvedValue(undefined);
        api.createViralImportTask.mockResolvedValue({
          taskId: "fresh-import",
          status: "SUCCEEDED",
          projectId: "fresh-project",
          sourceAssetId: "fresh-asset",
          canTranscribe: true,
        });
        live.extractScriptFromUpload.mockResolvedValue({
          text: "新来源的提取结果",
        });
        const state = createState("workbench");
        if (hasOldSource) {
          state.draft.projectId = "old-project";
          state.draft.sourceAssetId = "old-asset";
        }
        render(
          <StudioWorkspace currentUser={reviewUser} initialState={state} />,
        );
        fireEvent.click(
          await screen.findByRole("button", { name: "提取文案：新导入的爆款" }),
        );
        await waitFor(() =>
          expect(live.extractScriptFromUpload).toHaveBeenCalledWith(
            "fresh-project",
            "fresh-asset",
          ),
        );
        expect(api.createViralImportTask).toHaveBeenCalledWith(
          "douyin",
          "fresh-native",
          "copy",
          expect.any(String),
        );
        expect(await screen.findByLabelText("来源原文")).toHaveValue(
          "新来源的提取结果",
        );
        expect(live.extractScriptFromUpload).toHaveBeenCalledTimes(1);
      },
    );

    it("提取文案后往返视频复刻不会被空项目版本覆盖", async () => {
      live.loadStudioData.mockResolvedValue({
        ...emptyStudioData,
        projects: [livePanel.project],
      });
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.extractScriptFromUpload.mockResolvedValue({
        text: "需要保留的未保存转写原文",
      });
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
      fireEvent.click(screen.getByRole("button", { name: /^提取文案$/ }));
      expect(await screen.findByLabelText("来源原文")).toHaveValue(
        "需要保留的未保存转写原文",
      );
      fireEvent.click(screen.getByRole("button", { name: /^视频创作$/ }));
      await waitFor(() =>
        expect(api.getLatestScriptVersion).toHaveBeenCalledWith("project-1"),
      );
      await act(async () => {});
      fireEvent.click(screen.getByRole("button", { name: /^文案工坊$/ }));
      expect(await screen.findByLabelText("来源原文")).toHaveValue(
        "需要保留的未保存转写原文",
      );
      expect(screen.queryByText("尚未提取文案")).not.toBeInTheDocument();
      expect(live.extractScriptFromUpload).toHaveBeenCalledTimes(1);
    });

    it("提取文案成功后回填草稿并跳转文案工坊", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.extractScriptFromUpload.mockResolvedValue({
        text: "提取出的乡墅口播原文",
      });
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(live.extractScriptFromUpload).toHaveBeenCalledWith(
          "project-1",
          "asset-1",
        ),
      );
      await waitFor(() =>
        expect(screen.getByLabelText("来源原文")).toBeInTheDocument(),
      );
      expect(screen.getByText(/文案已提取/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("tab", { name: "文案改写" }));
      expect(
        (screen.getByLabelText("来源原文") as HTMLTextAreaElement).value,
      ).toBe("提取出的乡墅口播原文");
    });

    it.each(["success", "failure"])(
      "文案提取 %s 后刷新结算或退款余额",
      async (outcome) => {
        live.loadStudioData.mockResolvedValue(emptyStudioData);
        live.loadCloudDraft.mockResolvedValue(undefined);
        api.customerGetWallet.mockResolvedValue({ available_credits: 351 });
        let finish!: () => void;
        live.extractScriptFromUpload.mockReturnValue(
          new Promise((resolve, reject) => {
            finish = () =>
              outcome === "success"
                ? resolve({ text: "已完成转写" })
                : reject(new Error("转写失败"));
          }),
        );
        const state = createState("workbench");
        state.draft.projectId = "project-1";
        state.draft.sourceAssetId = "asset-1";
        render(
          <StudioWorkspace
            currentUser={{ ...reviewUser, id: "customer-a" }}
            customerWallet={customerAccount(customerStore("session-token"))}
            initialState={state}
          />,
        );
        await waitFor(() => expect(api.customerGetWallet).toHaveBeenCalled());
        fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
        api.customerGetWallet.mockResolvedValue({
          available_credits: outcome === "success" ? 353 : 377,
        });
        await act(async () => finish());
        fireEvent.click(
          screen.getByRole("button", { name: "用户档案，review" }),
        );
        expect(
          await screen.findByText(`${outcome === "success" ? 353 : 377} 积分`),
        ).toBeInTheDocument();
      },
    );

    it("同一账号提取期间切换为审计员会丢弃迟到成功回调", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      let resolveExtraction!: (value: { text: string }) => void;
      live.extractScriptFromUpload.mockReturnValue(
        new Promise((resolve) => {
          resolveExtraction = resolve;
        }),
      );
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(live.extractScriptFromUpload).toHaveBeenCalledWith(
          "project-1",
          "asset-1",
        ),
      );
      view.rerender(
        <StudioWorkspace
          currentUser={{
            id: reviewUser.id,
            username: reviewUser.username,
            display_name: "审计员",
            role: "auditor",
          }}
          initialState={state}
        />,
      );
      await act(async () => {
        resolveExtraction({ text: "不应写入的迟到转写" });
        await Promise.resolve();
      });

      expect(screen.queryByLabelText("二创文案")).toBeNull();
      expect(live.persistCloudDraft).not.toHaveBeenCalled();
    });

    it("同一账号提取期间切换为审计员会静默丢弃迟到失败", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      let rejectExtraction!: (cause: Error) => void;
      live.extractScriptFromUpload.mockReturnValue(
        new Promise((_resolve, reject) => {
          rejectExtraction = reject;
        }),
      );
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      const view = render(
        <StudioWorkspace currentUser={reviewUser} initialState={state} />,
      );

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(live.extractScriptFromUpload).toHaveBeenCalledWith(
          "project-1",
          "asset-1",
        ),
      );
      view.rerender(
        <StudioWorkspace
          currentUser={{
            ...reviewUser,
            display_name: "审计员",
            role: "auditor",
          }}
          initialState={state}
        />,
      );
      await act(async () => {
        rejectExtraction(new Error("不应显示的迟到错误"));
        await Promise.resolve();
      });

      expect(screen.queryByText("不应显示的迟到错误")).toBeNull();
      expect(screen.queryByText(/文案提取失败/)).toBeNull();
      expect(live.persistCloudDraft).not.toHaveBeenCalled();
    });

    it("重新进入工作区时恢复同一来源资产已完成的提取任务", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.loadLatestScriptFromUpload.mockResolvedValueOnce({
        id: "asr-restored",
        status: "SUCCEEDED",
        result: { text: "后台已经完成的转写文案" },
        sourceAssetId: "asset-1",
      });
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      expect(
        await screen.findByText(/文案提取已完成.*恢复到当前草稿/),
      ).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "文案工坊" }));
      expect(screen.getByLabelText("来源原文")).toHaveValue(
        "后台已经完成的转写文案",
      );
    });

    it("恢复任务长时间停在 RUNNING 时停止轮询并放行手动提取", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.loadLatestScriptFromUpload.mockResolvedValue({
        id: "asr-stuck",
        status: "RUNNING",
        sourceAssetId: "asset-1",
      });
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      // 挂载前就切假定时器：恢复链路的 2s 重试定时器必须是假定时器，
      // 否则 advanceTimersByTimeAsync 推不动真实队列里的旧定时器。
      vi.useFakeTimers();
      try {
        render(
          <StudioWorkspace currentUser={reviewUser} initialState={state} />,
        );
        await act(async () => {
          await vi.advanceTimersByTimeAsync(0);
        });
        expect(screen.getByText(/已恢复上次文案提取任务/)).toBeInTheDocument();

        // 上限 2s × 150 = 5 分钟：跨过后轮询必须停（不再打接口）、
        // extractingRef 必须复位、用户得到明确提示而不是静默锁死。
        await act(async () => {
          await vi.advanceTimersByTimeAsync(2_000 * 150);
        });
        expect(
          screen.getByText(/长时间未完成，已停止自动等待/),
        ).toBeInTheDocument();
        const callsWhenGaveUp =
          live.loadLatestScriptFromUpload.mock.calls.length;
        expect(callsWhenGaveUp).toBe(150);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(10_000);
        });
        expect(live.loadLatestScriptFromUpload.mock.calls.length).toBe(
          callsWhenGaveUp,
        );
      } finally {
        vi.useRealTimers();
      }

      // extractingRef 已复位：手动提取恢复可用，不再被静默吞掉。
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(live.extractScriptFromUpload).toHaveBeenCalledWith(
          "project-1",
          "asset-1",
        ),
      );
    });

    it("提取进行中再次点击提取文案给出提示而非静默吞掉", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.extractScriptFromUpload.mockReturnValue(
        new Promise(() => {
          // 永不落定，模拟提取在途。
        }),
      );
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(live.extractScriptFromUpload).toHaveBeenCalled(),
      );
      // 提交后已自动进入文案工坊：回到工作台再次点击，验证占用提示仍然给出。
      fireEvent.click(screen.getByRole("button", { name: "工作台" }));
      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      expect(
        screen.getByText(/文案提取任务进行中，请等待当前任务完成/),
      ).toBeInTheDocument();
      expect(live.extractScriptFromUpload).toHaveBeenCalledTimes(1);
    });

    it("提取文案失败时保留工作区并提示服务端错误", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      live.extractScriptFromUpload.mockRejectedValue(
        new Error("语音转写服务返回错误（HTTP 500）"),
      );
      const state = createState("workbench");
      state.draft.projectId = "project-1";
      state.draft.sourceAssetId = "asset-1";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      await waitFor(() =>
        expect(screen.getByText(/语音转写服务返回错误/)).toBeInTheDocument(),
      );
      expect(screen.queryByLabelText("二创文案")).not.toBeInTheDocument();
    });

    it("有项目来源但资产缺失时提取文案提示先上传", async () => {
      live.loadStudioData.mockResolvedValue(emptyStudioData);
      live.loadCloudDraft.mockResolvedValue(undefined);
      const state = createState("workbench");
      state.draft.sourceId = "proj-9";
      state.draft.projectId = "proj-9";
      render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);

      fireEvent.click(screen.getByRole("button", { name: "提取文案" }));
      expect(screen.getByText(/请先上传视频来源/)).toBeInTheDocument();
      expect(live.extractScriptFromUpload).not.toHaveBeenCalled();
    });

    it("审核示例模式不触发任何云端草稿接口", async () => {
      render(
        <StudioWorkspace
          currentUser={reviewUser}
          reviewData={createReviewData()}
          initialState={createReviewState("copy")}
        />,
      );
      await openCopyPage();
      fireEvent.change(screen.getByLabelText("二创文案"), {
        target: { value: "审核模式编辑" },
      });
      await waitFor(() =>
        expect(
          screen.getByLabelText("二创文案") as HTMLTextAreaElement,
        ).toHaveValue("审核模式编辑"),
      );
      expect(live.loadCloudDraft).not.toHaveBeenCalled();
      expect(live.persistCloudDraft).not.toHaveBeenCalled();
      expect(live.loadSavedScriptList).not.toHaveBeenCalled();
    });
  });
});

describe("数字人口播提交", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.history.replaceState(null, "", "/#studio/workbench");
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    live.loadPersonAssets.mockResolvedValue({ assets: [], errors: [] });
    live.loadCloudDraft.mockResolvedValue(undefined);
    live.loadDraftMaterials.mockResolvedValue({
      assets: [],
      unavailableIds: [],
    });
    live.loadSavedScriptList.mockResolvedValue([]);
    api.createOralTask.mockReset();
    api.createMaterialUploadIntent.mockReset();
    api.uploadMaterial.mockReset();
    api.completeMaterialUpload.mockReset();
  });

  it("字幕参数进入请求，并发点击单飞且网络重试复用幂等键", async () => {
    const state = createReviewState("oral");
    // 复刻准备只约束 AI 视频，不能阻止独立的数字人口播提交。
    state.draft.replicaPreparationPending = true;
    state.draft.style = "standard";
    state.draft.subtitles = true;
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    let rejectFirst!: (reason: Error) => void;
    api.createOralTask.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectFirst = reject;
      }),
    );

    render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "生成口播视频" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "生成口播视频" }));
    const submit = await findEnabledButton("确认费用并提交");
    fireEvent.click(submit);
    fireEvent.click(submit);

    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledTimes(1));
    const firstRequest = api.createOralTask.mock.calls[0][0];
    expect(firstRequest.subtitle).toEqual(
      expect.objectContaining({ st_show: true }),
    );

    await act(async () => rejectFirst(new Error("network timeout")));
    api.createOralTask.mockResolvedValueOnce({
      id: "oral-task-1",
      status: "FAILED",
      estimated_cost_fen: 100,
      replayed: true,
    });
    fireEvent.click(submit);

    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledTimes(2));
    expect(api.createOralTask.mock.calls[1][0].idempotencyKey).toBe(
      firstRequest.idempotencyKey,
    );
    await screen.findByText("口播任务提交未成功，请核对素材后重试。");

    api.createOralTask.mockResolvedValueOnce({
      id: "oral-task-2",
      status: "QUEUED",
      estimated_cost_fen: 100,
      replayed: false,
    });
    fireEvent.click(screen.getByRole("button", { name: "生成口播视频" }));
    fireEvent.click(await findEnabledButton("确认费用并提交"));

    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledTimes(3));
    expect(api.createOralTask.mock.calls[2][0].idempotencyKey).not.toBe(
      firstRequest.idempotencyKey,
    );
  });

  it("积分不足时打开钱包侧栏，不让用户自己找充值入口", async () => {
    const state = createReviewState("oral");
    state.draft.style = "standard";
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    livePanel.props.mockClear();
    api.createOralTask.mockRejectedValueOnce(
      Object.assign(new Error("积分不足，本次需要 14 积分。"), {
        code: "INSUFFICIENT_CREDITS",
      }),
    );

    render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "生成口播视频" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "生成口播视频" }));
    fireEvent.click(await findEnabledButton("确认费用并提交"));

    // 服务端文案原样透传，用户看得到还差多少。
    await screen.findByText("积分不足，本次需要 14 积分。");
    await waitFor(() =>
      expect(livePanel.props).toHaveBeenCalledWith(
        expect.objectContaining({ panel: "wallet" }),
      ),
    );
  });

  it("其他提交失败不打开钱包侧栏", async () => {
    const state = createReviewState("oral");
    state.draft.style = "standard";
    live.loadStudioData.mockResolvedValue({
      ...createReviewData(),
      loading: false,
    });
    livePanel.props.mockClear();
    api.createOralTask.mockRejectedValueOnce(new Error("网络连接中断"));

    render(<StudioWorkspace currentUser={reviewUser} initialState={state} />);
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "生成口播视频" }),
      ).toBeEnabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "生成口播视频" }));
    fireEvent.click(await findEnabledButton("确认费用并提交"));

    await screen.findByText("网络连接中断");
    expect(livePanel.props).not.toHaveBeenCalledWith(
      expect.objectContaining({ panel: "wallet" }),
    );
  });
});

describe("视频生成（C2 独立创作）", () => {
  it("参考选择器排除供应商直链条目并保留已归档资产", async () => {
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      materials: [
        {
          id: "generation:task-direct",
          name: "尚未归档的视频",
          kind: "video",
          group: "任务结果",
          source: "生成结果",
          saved: false,
          delivery: "direct",
          allowedUses: [],
        },
        {
          id: "stored-video",
          assetId: "stored-video",
          name: "已经保存的视频",
          kind: "video",
          group: "任务结果",
          source: "生成结果",
          saved: true,
          allowedUses: ["reference"],
        },
      ],
    });
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("reference")}
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: /从素材库选择/ }),
    );
    expect(
      await screen.findByRole("button", { name: /已经保存的视频/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /尚未归档的视频/ }),
    ).not.toBeInTheDocument();
  });

  it("视频提交失败在确认弹窗内显示，不误报未扣费", async () => {
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    api.createIndependentVideoTask.mockRejectedValueOnce(
      new Error("素材已失效，请重新选择"),
    );
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "乡墅庭院" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));
    const dialog = await screen.findByRole("dialog", {
      name: "生成确认 · 视频生成",
    });
    await screen.findByText(/9\.60 元/);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "确认费用并提交" }),
    );
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "素材已失效，请重新选择",
    );
    expect(
      within(dialog).queryByText("尚未提交 · 未扣费"),
    ).not.toBeInTheDocument();
    expect(api.createIndependentVideoTask).toHaveBeenCalledOnce();
  });

  it("非法草稿时长钳位到 15 秒提交，不再静默回落 8 秒（preflight P1-4）", async () => {
    // 上线前检查 P1-4：云草稿恢复链不校验 duration，旧客户端写入的超界值
    // 到达独立创作页后，提交/报价必须按 4–15 钳位（20 → 15），与复刻路径
    // normalizeCustomerDuration 同口径；旧行为「非法 → 悄悄换 8」会让
    // 时长下拉显示值与实际生成时长不一致。
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    api.getGenerationPriceQuote.mockReset();
    api.getGenerationPriceQuote.mockResolvedValue({
      resolution: "768P",
      duration_seconds: 15,
      quantity: 1,
      unit_price_fen_per_second: 120,
      estimated_seconds: 15,
      estimated_price_fen: 1800,
    });
    const base = createState();
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={{
          ...createState("reference"),
          draft: { ...base.draft, duration: 20 },
        }}
      />,
    );
    await openVideoPage();
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "乡墅庭院" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));
    const dialog = await screen.findByRole("dialog", {
      name: "生成确认 · 视频生成",
    });
    await screen.findByText(/18\.00 元/);
    fireEvent.click(
      within(dialog).getByRole("button", { name: "确认费用并提交" }),
    );
    await waitFor(() =>
      expect(api.createIndependentVideoTask).toHaveBeenCalledOnce(),
    );
    const call = api.createIndependentVideoTask.mock.calls[0]?.[0] as
      | {
          output_duration_seconds?: number;
        }
      | undefined;
    expect(call?.output_duration_seconds).toBe(15);
    const quoteCalls = api.getGenerationPriceQuote.mock
      .calls as unknown as Array<[{ duration_seconds?: number }]>;
    expect(quoteCalls.some(([input]) => input.duration_seconds === 15)).toBe(
      true,
    );
  });
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useRealTimers();
    api.getWallet.mockReset();
    api.getWallet.mockRejectedValue(new Error("internal wallet unavailable"));
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    api.getGenerationPriceQuote.mockResolvedValue({
      resolution: "768P",
      duration_seconds: 8,
      quantity: 1,
      unit_price_fen_per_second: 120,
      estimated_seconds: 8,
      estimated_price_fen: 960,
    });
    // vi.clearAllMocks() only clears call records; leftover mock*ValueOnce
    // queues from earlier tests would leak into the next one, so reset the
    // oral mocks before reinstalling their permanent defaults.
    api.getOralPrice.mockReset();
    api.getOralPrice.mockResolvedValue({ unit_price_fen: 500 });
    api.createOralTask.mockReset();
    live.loadCloudDraft.mockResolvedValue(undefined);
    live.loadPersonAssets.mockResolvedValue({ assets: [], errors: [] });
    live.loadDraftMaterials.mockResolvedValue({
      assets: [],
      unavailableIds: [],
    });
  });

  const emptyStudioData = {
    people: [],
    assets: [],
    materials: [],
    videos: [],
    projects: [],
    tasks: [],
    errors: [],
    loading: false,
    stats: null,
    analytics7: null,
    analytics30: null,
  };

  async function openVideoPage() {
    // 侧边栏「视频创作」进入复刻页签组，再切到「视频生成」。
    fireEvent.click(screen.getByRole("button", { name: "视频创作" }));
    fireEvent.click(screen.getByRole("tab", { name: "AI 视频" }));
    await waitFor(() =>
      expect(screen.getByLabelText("提示词")).toBeInTheDocument(),
    );
  }

  it("T2V 未开放时提前禁用提交并提示可选用图生视频", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: false,
      t2v_enabled: false,
      i2v_enabled: true,
      r2v_enabled: false,
      last_frame_enabled: false,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();

    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "山间别墅延时" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));

    expect(
      await screen.findByText("文生视频当前未开放，可添加首帧使用图生视频。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    expect(screen.queryByText("生成确认 · 视频生成")).toBeNull();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("参考素材选择器展示图片/视频/音频并按每类上限分别阻止", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 2,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const imageA = {
      id: "image-a",
      name: "外立面 A.jpg",
      kind: "image" as const,
      group: "参考素材",
      source: "素材库",
      saved: true,
    };
    const imageB = { ...imageA, id: "image-b", name: "外立面 B.jpg" };
    const imageC = { ...imageA, id: "image-c", name: "外立面 C.jpg" };
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [
        imageA,
        imageB,
        imageC,
        { ...imageA, id: "video-a", name: "运镜.mp4", kind: "video" },
        { ...imageA, id: "audio-a", name: "环境声.wav", kind: "audio" },
      ],
    });
    const initial = createState("reference");
    initial.draft.prompt = "参考外立面生成";
    initial.draft.referenceIds = ["image-a", "image-b"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    const pickerButton = await screen.findByRole("button", {
      name: /从素材库选择/,
    });
    fireEvent.click(pickerButton);
    const picker = screen.getByRole("dialog", { name: "选择参考素材" });
    // 已选的两张图片不再出现，未选图片与视频/音频都可选
    expect(
      within(picker).queryByRole("button", { name: /外立面 A/ }),
    ).toBeNull();
    expect(
      within(picker).queryByRole("button", { name: /外立面 B/ }),
    ).toBeNull();
    expect(
      within(picker).getByRole("button", { name: /外立面 C/ }),
    ).toBeInTheDocument();
    expect(
      within(picker).getByRole("button", { name: /运镜/ }),
    ).toBeInTheDocument();
    expect(
      within(picker).getByRole("button", { name: /环境声/ }),
    ).toBeInTheDocument();
    expect(
      picker.querySelector(".studio-picker-grid--reference"),
    ).not.toBeNull();

    fireEvent.click(within(picker).getByRole("button", { name: "声音" }));
    expect(
      within(picker).getByRole("button", { name: /环境声/ }),
    ).toBeInTheDocument();
    expect(within(picker).queryByRole("button", { name: /运镜/ })).toBeNull();
    fireEvent.change(within(picker).getByLabelText("搜索素材文件名"), {
      target: { value: "不存在" },
    });
    expect(within(picker).getByText("没有符合条件的素材")).toBeInTheDocument();
    fireEvent.change(within(picker).getByLabelText("搜索素材文件名"), {
      target: { value: "" },
    });
    fireEvent.click(within(picker).getByRole("button", { name: "全部" }));

    // 图片已达每类上限（2/2），再选图片被拒并提示
    fireEvent.click(within(picker).getByRole("button", { name: /外立面 C/ }));
    expect(
      await screen.findByText("当前最多选择 2 张参考图。"),
    ).toBeInTheDocument();

    // 视频仍有额度，选择后按类更新计数
    fireEvent.click(within(picker).getByRole("button", { name: /运镜/ }));
    expect(
      await screen.findByText("参考图 2/2 · 视频 1/3 · 音频 0/3"),
    ).toBeInTheDocument();
  });

  it("参考素材选择器拦截时长超过 15 秒的视频与音频", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 8,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const base = {
      id: "image-a",
      name: "外立面 A.jpg",
      kind: "image" as const,
      group: "参考素材",
      source: "素材库",
      saved: true,
    };
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [
        {
          ...base,
          id: "video-long",
          name: "长运镜.mp4",
          kind: "video",
          durationSeconds: 20,
        },
        {
          ...base,
          id: "audio-long",
          name: "长环境声.mp3",
          kind: "audio",
          durationSeconds: 30,
        },
        {
          ...base,
          id: "video-ok",
          name: "短运镜.mp4",
          kind: "video",
          durationSeconds: 10,
        },
      ],
    });
    const initial = createState("reference");
    initial.draft.prompt = "参考外立面生成";
    initial.draft.referenceIds = [];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    fireEvent.click(
      await screen.findByRole("button", { name: /从素材库选择/ }),
    );
    const picker = screen.getByRole("dialog", { name: "选择参考素材" });

    // 超过 15 秒的视频被拦截、不加入参考
    fireEvent.click(within(picker).getByRole("button", { name: /长运镜/ }));
    expect(
      await screen.findByText("参考视频时长不能超过 15 秒，请裁剪后再选取。"),
    ).toBeInTheDocument();

    // 超过 15 秒的音频被拦截
    fireEvent.click(within(picker).getByRole("button", { name: /长环境声/ }));
    expect(
      await screen.findByText("参考音频时长不能超过 15 秒，请裁剪后再选取。"),
    ).toBeInTheDocument();

    // 合规视频（10 秒）可正常选取
    fireEvent.click(within(picker).getByRole("button", { name: /短运镜/ }));
    expect(
      await screen.findByText("参考图 0/8 · 视频 1/3 · 音频 0/3"),
    ).toBeInTheDocument();
  });

  it("图片素材选择器只签当前六条且下一页只增加一条", async () => {
    const materials = Array.from({ length: 7 }, (_, index) => ({
      id: `material-${index + 1}`,
      assetId: `material-${index + 1}`,
      name: `云端素材 ${index + 1}.jpg`,
      kind: "image" as const,
      group: "素材库",
      source: "素材库",
      saved: true,
    }));
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      materials,
    });
    api.getAssetDownloadUrl.mockImplementation((assetId: string) =>
      Promise.resolve({ url: `https://storage.test/${assetId}` }),
    );
    const initial = createState("reference");
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    fireEvent.click(
      await screen.findByRole("button", { name: /从素材库选择/ }),
    );
    const picker = screen.getByRole("dialog", { name: "选择参考素材" });
    await waitFor(() =>
      expect(api.getAssetDownloadUrl).toHaveBeenCalledTimes(6),
    );
    expect(within(picker).getByAltText("云端素材 1.jpg")).toHaveAttribute(
      "src",
      "https://storage.test/material-1",
    );
    expect(within(picker).queryByText("云端素材 7.jpg")).toBeNull();

    fireEvent.click(within(picker).getByRole("button", { name: "下一页素材" }));
    await waitFor(() =>
      expect(api.getAssetDownloadUrl).toHaveBeenCalledTimes(7),
    );
    expect(within(picker).getByAltText("云端素材 7.jpg")).toHaveAttribute(
      "src",
      "https://storage.test/material-7",
    );
    expect(within(picker).queryByText("云端素材 1.jpg")).toBeNull();
  });

  it("参考生视频能力关闭时禁止进入提交确认", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: false,
      t2v_enabled: false,
      i2v_enabled: true,
      r2v_enabled: false,
      last_frame_enabled: false,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const image = (id: string) => ({
      id,
      name: `${id}.jpg`,
      kind: "image" as const,
      group: "参考素材",
      source: "素材库",
      saved: true,
    });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [image("image-a")],
    });
    const initial = createState("reference");
    initial.draft.prompt = "旧草稿";
    initial.draft.referenceIds = ["image-a"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    expect(
      await screen.findByText("参考生视频当前未开放，请等待能力开启后再提交。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    expect(screen.queryByText("生成确认 · 视频生成")).toBeNull();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("旧草稿参考图超出能力上限时要求整理并禁止提交", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 2,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const image = (id: string) => ({
      id,
      name: `${id}.jpg`,
      kind: "image" as const,
      group: "参考素材",
      source: "素材库",
      saved: true,
    });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [image("image-a"), image("image-b"), image("image-c")],
    });
    const initial = createState("reference");
    initial.draft.prompt = "旧草稿";
    initial.draft.referenceIds = ["image-a", "image-b", "image-c"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    expect(
      await screen.findByText("当前最多选择 2 张参考图，旧草稿已超出 1 张。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "整理参考素材" })).toBeEnabled();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("云端草稿参考图解析完成前禁止误整理合法引用", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const restored = createState("reference").draft;
    restored.prompt = "恢复草稿";
    restored.referenceIds = ["image-restored"];
    live.loadCloudDraft.mockResolvedValue({
      draft: restored,
      updatedAt: freshUpdatedAt(),
    });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    let resolveMaterials:
      | ((value: { assets: unknown[]; unavailableIds: string[] }) => void)
      | undefined;
    live.loadDraftMaterials.mockImplementation(
      () =>
        new Promise<{ assets: unknown[]; unavailableIds: string[] }>(
          (resolve) => {
            resolveMaterials = resolve;
          },
        ),
    );
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("reference")}
      />,
    );
    await acceptRestorePrompt();

    expect(
      await screen.findByText("正在恢复草稿参考图，请稍候。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "整理参考素材" })).toBeNull();
    resolveMaterials?.({
      assets: [
        {
          id: "image-restored",
          name: "恢复参考图.jpg",
          kind: "image",
          group: "参考素材",
          source: "云端草稿",
          saved: true,
        },
      ],
      unavailableIds: [],
    });

    expect(await screen.findByText("恢复参考图.jpg")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "整理参考素材" })).toBeNull();
  });

  it("云端草稿参考图解析失败时保留引用并可重试恢复", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const restored = createState("reference").draft;
    restored.prompt = "恢复草稿";
    restored.referenceIds = ["image-restored"];
    live.loadCloudDraft.mockResolvedValue({
      draft: restored,
      updatedAt: freshUpdatedAt(),
    });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    live.loadDraftMaterials
      .mockRejectedValueOnce(new Error("解析接口失败"))
      .mockResolvedValueOnce({
        assets: [
          {
            id: "image-restored",
            name: "恢复参考图.jpg",
            kind: "image",
            group: "参考素材",
            source: "云端草稿",
            saved: true,
          },
        ],
        unavailableIds: [],
      });
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("reference")}
      />,
    );
    await acceptRestorePrompt();

    expect(
      await screen.findByText("草稿参考图读取失败，请重试。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "整理参考素材" })).toBeNull();
    expect(live.persistCloudDraft).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "重试读取草稿参考图" }));

    expect(await screen.findByText("恢复参考图.jpg")).toBeInTheDocument();
    expect(live.loadDraftMaterials).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button", { name: "整理参考素材" })).toBeNull();
    expect(live.persistCloudDraft).not.toHaveBeenCalled();
  });

  it("切换草稿后忽略旧草稿参考图重试的迟到结果", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const restored = createState("reference").draft;
    restored.referenceIds = ["image-restored"];
    live.loadCloudDraft.mockResolvedValue({
      draft: restored,
      updatedAt: freshUpdatedAt(),
    });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    let resolveRetry:
      | ((value: { assets: unknown[]; unavailableIds: string[] }) => void)
      | undefined;
    live.loadDraftMaterials
      .mockRejectedValueOnce(new Error("解析接口失败"))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveRetry = resolve;
          }),
      );
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createState("reference")}
      />,
    );
    await acceptRestorePrompt();
    fireEvent.click(
      await screen.findByRole("button", { name: "重试读取草稿参考图" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "新建创作" }));
    fireEvent.click(screen.getByRole("button", { name: "从空白创作开始" }));
    resolveRetry?.({
      assets: [
        {
          id: "image-restored",
          name: "不应回填.jpg",
          kind: "image",
          group: "参考素材",
          source: "云端草稿",
          saved: true,
        },
      ],
      unavailableIds: [],
    });
    await Promise.resolve();

    expect(screen.queryByText("不应回填.jpg")).toBeNull();
    expect(screen.queryByText("草稿参考图读取失败，请重试。")).toBeNull();
  });

  it("视频能力读取失败后可重试并恢复参考图操作", async () => {
    api.getIndependentCapabilities
      .mockRejectedValueOnce(new Error("能力接口失败"))
      .mockResolvedValueOnce({
        extended_modes_enabled: true,
        t2v_enabled: true,
        i2v_enabled: true,
        r2v_enabled: true,
        last_frame_enabled: true,
        max_reference_images: 4,
        max_reference_videos: 3,
        max_reference_audios: 3,
        max_quantity: 4,
      });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [
        {
          id: "image-a",
          name: "外立面 A.jpg",
          kind: "image",
          group: "参考素材",
          source: "素材库",
          saved: true,
        },
      ],
    });
    const initial = createState("reference");
    initial.draft.prompt = "参考外立面生成";
    initial.draft.referenceIds = ["image-a"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    expect(
      await screen.findByText("视频生成能力读取失败，请重试。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "生成视频" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试读取视频能力" }));

    await waitFor(() =>
      expect(api.getIndependentCapabilities).toHaveBeenCalledTimes(2),
    );
    expect(screen.getByRole("button", { name: "生成视频" })).toBeEnabled();
  });

  it("确认弹窗打开后参考图失效也不能绕过最终提交校验", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    live.loadStudioData.mockResolvedValue({
      ...emptyStudioData,
      assets: [
        {
          id: "image-a",
          name: "外立面 A.jpg",
          kind: "image",
          group: "参考素材",
          source: "素材库",
          saved: true,
        },
      ],
    });
    const initial = createState("reference");
    initial.draft.prompt = "参考外立面生成";
    initial.draft.referenceIds = ["image-a"];
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    fireEvent.click(await screen.findByRole("button", { name: "生成视频" }));
    expect(await screen.findByText("生成确认 · 视频生成")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "移除 外立面 A.jpg" }));
    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));

    expect(
      await screen.findByText("请至少选择一个参考素材"),
    ).toBeInTheDocument();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();
  });

  it("视频报价失败时确认按钮不可用并可重新获取报价", async () => {
    api.getGenerationPriceQuote
      .mockRejectedValueOnce(new Error("视频报价暂不可用"))
      .mockResolvedValueOnce({
        resolution: "768P",
        duration_seconds: 8,
        quantity: 1,
        unit_price_fen_per_second: 120,
        estimated_seconds: 8,
        estimated_price_fen: 960,
      });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "航拍乡墅庭院" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));

    expect(await screen.findByText("视频报价暂不可用")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "确认费用并提交" });
    expect(submit).toBeDisabled();
    expect(api.createIndependentVideoTask).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重新获取视频报价" }));
    expect(await screen.findByText(/9\.60 元/)).toBeInTheDocument();
    expect(submit).toBeEnabled();
  });

  it("口播报价失败时禁止提交并支持重试", async () => {
    api.getOralPrice
      .mockRejectedValueOnce(new Error("口播报价暂不可用"))
      .mockResolvedValueOnce({ unit_price_fen: 500 });
    live.loadStudioData.mockResolvedValue(createReviewData());
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(await findEnabledButton("生成口播视频"));

    expect(await screen.findByText("口播报价暂不可用")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "确认费用并提交" });
    expect(submit).toBeDisabled();
    expect(api.createOralTask).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "重新获取口播报价" }));
    expect(await screen.findByText("5.00 元/秒")).toBeInTheDocument();
    expect(submit).toBeEnabled();
  });

  it("文案口播关闭字幕时不提交供应商字幕配置", async () => {
    api.createOralTask.mockResolvedValue({ id: "oral-1", status: "QUEUED" });
    live.loadStudioData.mockResolvedValue(createReviewData());
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );

    fireEvent.click(await findEnabledButton("生成口播视频"));
    expect(await screen.findByText("5.00 元/秒")).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("确认费用并提交"));

    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledTimes(1));
    expect(api.createOralTask.mock.calls[0]?.[0]).not.toHaveProperty(
      "subtitle",
    );
  });

  it("文案口播开启字幕时提交供应商原生字幕配置", async () => {
    api.createOralTask.mockResolvedValue({ id: "oral-1", status: "QUEUED" });
    live.loadStudioData.mockResolvedValue(createReviewData());
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "添加" }));
    fireEvent.click(await findEnabledButton("生成口播视频"));
    expect(await screen.findByText("5.00 元/秒")).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("确认费用并提交"));

    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledTimes(1));
    expect(api.createOralTask.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({
        mode: "TTS",
        subtitle: {
          st_show: true,
          st_font_size: 30,
          st_primary_color: "0xFFFFFF",
          st_outline_color: "0x000000",
        },
      }),
    );
  });

  it("旧音频页面提交统一使用 TTS、克隆声音和字幕配置", async () => {
    api.createOralTask.mockResolvedValue({ id: "oral-1", status: "QUEUED" });
    live.loadStudioData.mockResolvedValue(createReviewData());
    const initial = createReviewState("oral-audio");
    initial.draft.subtitles = true;
    render(<StudioWorkspace currentUser={reviewUser} initialState={initial} />);

    expect(await findEnabledButton("生成口播视频")).toBeEnabled();
    expect(screen.queryByRole("button", { name: "添加" })).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("生成口播视频"));
    expect(await screen.findByText("5.00 元/秒")).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("确认费用并提交"));

    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledTimes(1));
    expect(api.createOralTask.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({
        mode: "TTS",
        voiceId: "voice-1",
      }),
    );
    expect(api.createOralTask.mock.calls[0]?.[0]).toHaveProperty("subtitle");
  });

  it("旧口播响应不会关闭后来重新打开的生成弹窗", async () => {
    let resolveTask:
      | ((value: { id: string; status: string }) => void)
      | undefined;
    api.createOralTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTask = resolve;
        }),
    );
    live.loadStudioData.mockResolvedValue(createReviewData());
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );

    fireEvent.click(await screen.findByRole("button", { name: "添加" }));
    fireEvent.click(await findEnabledButton("生成口播视频"));
    const firstDialog = await screen.findByRole("dialog", {
      name: "生成确认 · 数字人口播",
    });
    expect(await screen.findByText("5.00 元/秒")).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("确认费用并提交", firstDialog));
    fireEvent.click(within(firstDialog).getByRole("button", { name: "关闭" }));
    fireEvent.click(await findEnabledButton("生成口播视频"));
    expect(
      await screen.findByRole("dialog", { name: "生成确认 · 数字人口播" }),
    ).toBeInTheDocument();

    await act(async () => {
      resolveTask?.({ id: "oral-old-tts", status: "QUEUED" });
      await Promise.resolve();
    });

    expect(
      screen.getByRole("dialog", { name: "生成确认 · 数字人口播" }),
    ).toBeInTheDocument();
    expect(api.createOralTask.mock.calls[0]?.[0]).toEqual(
      expect.objectContaining({ mode: "TTS", subtitle: expect.any(Object) }),
    );
  });

  it("口播确认连续点击只创建一个任务", async () => {
    let resolveTask:
      | ((value: { id: string; status: string }) => void)
      | undefined;
    api.createOralTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTask = resolve;
        }),
    );
    live.loadStudioData.mockResolvedValue(createReviewData());
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(await findEnabledButton("生成口播视频"));
    expect(await screen.findByText("5.00 元/秒")).toBeInTheDocument();
    const submit = await findEnabledButton("确认费用并提交");

    fireEvent.click(submit);
    fireEvent.click(submit);

    expect(api.createOralTask).toHaveBeenCalledTimes(1);
    resolveTask?.({ id: "oral-1", status: "QUEUED" });
  });

  it("口播结果未知后重新报价仍重放原请求和幂等键", async () => {
    api.getOralPrice
      .mockResolvedValueOnce({ unit_price_fen: 500 })
      .mockResolvedValueOnce({ unit_price_fen: 600 });
    api.createOralTask
      .mockRejectedValueOnce(new Error("提交结果未知，请安全重试。"))
      .mockRejectedValueOnce(new Error("提交结果仍未知，请继续安全重试。"));
    live.loadStudioData.mockResolvedValue(createReviewData());
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );
    const open = await findEnabledButton("生成口播视频");
    fireEvent.click(open);
    expect(await screen.findByText("5.00 元/秒")).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("确认费用并提交"));
    expect(
      await screen.findByText("提交结果未知，请安全重试。"),
    ).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "关闭",
      }),
    );

    fireEvent.click(open);
    expect(await screen.findByText("6.00 元/秒")).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("确认费用并提交"));
    expect(
      await screen.findByText("提交结果仍未知，请继续安全重试。"),
    ).toBeInTheDocument();

    expect(api.createOralTask).toHaveBeenCalledTimes(2);
    expect(api.createOralTask.mock.calls[1]).toEqual(
      api.createOralTask.mock.calls[0],
    );
  });

  it("旧口播提交响应不会关闭后来重新打开的确认弹窗", async () => {
    let resolveTask:
      | ((value: { id: string; status: string }) => void)
      | undefined;
    api.createOralTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTask = resolve;
        }),
    );
    live.loadStudioData.mockResolvedValue(createReviewData());
    render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );
    const open = await findEnabledButton("生成口播视频");
    fireEvent.click(open);
    const firstDialog = await screen.findByRole("dialog", {
      name: "生成确认 · 数字人口播",
    });
    fireEvent.click(await findEnabledButton("确认费用并提交", firstDialog));
    fireEvent.click(within(firstDialog).getByRole("button", { name: "关闭" }));

    fireEvent.click(open);
    expect(
      await screen.findByRole("dialog", { name: "生成确认 · 数字人口播" }),
    ).toBeInTheDocument();

    await act(async () => {
      resolveTask?.({ id: "oral-old", status: "QUEUED" });
      await Promise.resolve();
    });

    expect(
      screen.getByRole("dialog", { name: "生成确认 · 数字人口播" }),
    ).toBeInTheDocument();
    fireEvent.click(await findEnabledButton("确认费用并提交"));
    expect(api.createOralTask).toHaveBeenCalledTimes(2);
    expect(api.createOralTask.mock.calls[1]).toEqual(
      api.createOralTask.mock.calls[0],
    );
  });

  it("同一账号提交口播后切换为审计员会静默丢弃迟到响应", async () => {
    let resolveTask:
      | ((value: { id: string; status: string }) => void)
      | undefined;
    api.createOralTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTask = resolve;
        }),
    );
    live.loadStudioData.mockResolvedValue(createReviewData());
    const view = render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(await findEnabledButton("生成口播视频"));
    const dialog = await screen.findByRole("dialog", {
      name: "生成确认 · 数字人口播",
    });
    fireEvent.click(await findEnabledButton("确认费用并提交", dialog));
    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledOnce());
    const previousHash = window.location.hash;

    view.rerender(
      <StudioWorkspace
        currentUser={{
          ...reviewUser,
          display_name: "审计员",
          role: "auditor",
        }}
        initialState={createReviewState("oral")}
      />,
    );
    await act(async () => {
      resolveTask?.({ id: "oral-after-role-change", status: "QUEUED" });
      await Promise.resolve();
    });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(/口播任务已提交/)).toBeNull();
    expect(window.location.hash).toBe(previousHash);
  });

  it("旧口播请求的 finally 不会解锁角色往返后的新提交", async () => {
    const resolveTasks: Array<(value: { id: string; status: string }) => void> =
      [];
    api.createOralTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTasks.push(resolve);
        }),
    );
    live.loadStudioData.mockResolvedValue(createReviewData());
    const state = createReviewState("oral");
    const view = render(
      <StudioWorkspace currentUser={reviewUser} initialState={state} />,
    );
    fireEvent.click(await findEnabledButton("生成口播视频"));
    fireEvent.click(await findEnabledButton("确认费用并提交"));
    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledOnce());

    view.rerender(
      <StudioWorkspace
        currentUser={{ ...reviewUser, role: "auditor" }}
        initialState={state}
      />,
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    view.rerender(
      <StudioWorkspace currentUser={reviewUser} initialState={state} />,
    );
    fireEvent.click(await findEnabledButton("生成口播视频"));
    fireEvent.click(await findEnabledButton("确认费用并提交"));
    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();

    await act(async () => {
      resolveTasks[0]?.({ id: "oral-a", status: "QUEUED" });
      await Promise.resolve();
    });

    expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
    await act(async () => {
      resolveTasks[1]?.({ id: "oral-b", status: "QUEUED" });
      await Promise.resolve();
    });
  });

  it("工作区卸载后旧口播响应不会再跳转页面", async () => {
    live.loadPersonAssets.mockResolvedValue({ assets: [], errors: [] });
    let resolveTask:
      | ((value: { id: string; status: string }) => void)
      | undefined;
    api.createOralTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveTask = resolve;
        }),
    );
    live.loadStudioData.mockResolvedValue(createReviewData());
    const view = render(
      <StudioWorkspace
        currentUser={reviewUser}
        initialState={createReviewState("oral")}
      />,
    );
    fireEvent.click(await findEnabledButton("生成口播视频"));
    const dialog = await screen.findByRole("dialog", {
      name: "生成确认 · 数字人口播",
    });
    fireEvent.click(await findEnabledButton("确认费用并提交", dialog));
    await waitFor(() => expect(api.createOralTask).toHaveBeenCalledOnce());
    const previousHash = window.location.hash;
    view.unmount();

    await act(async () => {
      resolveTask?.({ id: "oral-after-unmount", status: "QUEUED" });
      await Promise.resolve();
    });

    expect(window.location.hash).toBe(previousHash);
  });

  it("确认弹窗展示按秒报价并可提交任务、预览区进入进度视图", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const submittedBatch = {
      id: "batch-video-1",
      project_id: null,
      creation_kind: "independent",
      stale: false,
      progress: { total_count: 1, terminal_count: 0, progress_percent: 0 },
      tasks: [],
    };
    api.createIndependentVideoTask.mockResolvedValue(submittedBatch);
    const runningTask = {
      id: "batch-video-1",
      type: "视频生成",
      title: "视频生成",
      status: "running",
      progress: 40,
      submitted: "2026-09-06 12:00:00",
    };
    live.loadStudioData
      .mockResolvedValueOnce(emptyStudioData)
      .mockResolvedValue({
        ...emptyStudioData,
        tasks: [runningTask],
      });
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();

    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "航拍乡墅庭院" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));

    // 报价按秒折算：120 分/秒 × 8 秒 = 9.60 元。
    expect(await screen.findByText(/9\.60 元/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));
    await waitFor(() =>
      expect(api.createIndependentVideoTask).toHaveBeenCalledTimes(1),
    );
    const input = api.createIndependentVideoTask.mock.calls[0][0] as {
      display_name: string;
      mode: string;
      prompt_text: string;
      idempotency_key: string;
    };
    expect(input.mode).toBe("t2v");
    expect(input.display_name).toBe("未命名视频");
    expect(input.prompt_text).toBe("航拍乡墅庭院");
    expect(input.idempotency_key).toBeTruthy();

    // 提交后留在本页：预览区出现阶段进度（数据来自任务轮询）。
    expect(await screen.findByText("生成中")).toBeInTheDocument();
    expect(
      screen.getByRole("progressbar", { name: "生成进度" }),
    ).toBeInTheDocument();
  });

  it("旧视频提交响应不会覆盖新打开的确认上下文", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    let resolveBatch:
      | ((value: { id: string; status: string; tasks: unknown[] }) => void)
      | undefined;
    api.createIndependentVideoTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveBatch = resolve;
        }),
    );
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "航拍乡墅庭院" },
    });
    const open = screen.getByRole("button", { name: "生成视频" });
    fireEvent.click(open);
    const firstDialog = await screen.findByRole("dialog", {
      name: "生成确认 · 视频生成",
    });
    await screen.findByText(/9\.60 元/);
    fireEvent.click(
      within(firstDialog).getByRole("button", { name: "确认费用并提交" }),
    );
    fireEvent.click(within(firstDialog).getByRole("button", { name: "关闭" }));

    fireEvent.click(open);
    expect(
      await screen.findByRole("dialog", { name: "生成确认 · 视频生成" }),
    ).toBeInTheDocument();

    await act(async () => {
      resolveBatch?.({ id: "batch-old", status: "QUEUED", tasks: [] });
      await Promise.resolve();
    });

    expect(
      screen.getByRole("dialog", { name: "生成确认 · 视频生成" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));
    expect(api.createIndependentVideoTask).toHaveBeenCalledTimes(2);
    expect(api.createIndependentVideoTask.mock.calls[1]).toEqual(
      api.createIndependentVideoTask.mock.calls[0],
    );
  });

  it("旧视频请求的 finally 不会解锁角色往返后的新提交", async () => {
    api.getIndependentCapabilities.mockResolvedValue({
      extended_modes_enabled: true,
      t2v_enabled: true,
      i2v_enabled: true,
      r2v_enabled: true,
      last_frame_enabled: true,
      max_reference_images: 4,
      max_reference_videos: 3,
      max_reference_audios: 3,
      max_quantity: 4,
    });
    const resolveBatches: Array<
      (value: { id: string; status: string; tasks: unknown[] }) => void
    > = [];
    api.createIndependentVideoTask.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveBatches.push(resolve);
        }),
    );
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    const view = render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "航拍乡墅庭院" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "确认费用并提交" }),
    );
    await waitFor(() =>
      expect(api.createIndependentVideoTask).toHaveBeenCalledOnce(),
    );

    view.rerender(
      <StudioWorkspace currentUser={{ ...reviewUser, role: "auditor" }} />,
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    view.rerender(<StudioWorkspace currentUser={reviewUser} />);
    fireEvent.click(screen.getByRole("button", { name: "生成视频" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "确认费用并提交" }),
    );
    await waitFor(() =>
      expect(api.createIndependentVideoTask).toHaveBeenCalledTimes(2),
    );
    expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();

    await act(async () => {
      resolveBatches[0]?.({ id: "batch-a", status: "QUEUED", tasks: [] });
      await Promise.resolve();
    });

    expect(screen.getByRole("button", { name: "提交中…" })).toBeDisabled();
    await act(async () => {
      resolveBatches[1]?.({ id: "batch-b", status: "QUEUED", tasks: [] });
      await Promise.resolve();
    });
  });

  it("视频结果未知后价格变化不换键，业务参数变化才换键", async () => {
    api.getGenerationPriceQuote
      .mockResolvedValueOnce({
        resolution: "768P",
        duration_seconds: 8,
        quantity: 1,
        unit_price_fen_per_second: 120,
        estimated_seconds: 8,
        estimated_price_fen: 960,
      })
      .mockResolvedValue({
        resolution: "768P",
        duration_seconds: 8,
        quantity: 1,
        unit_price_fen_per_second: 150,
        estimated_seconds: 8,
        estimated_price_fen: 1200,
      });
    api.createIndependentVideoTask
      .mockRejectedValueOnce(new Error("提交结果未知，请安全重试。"))
      .mockRejectedValueOnce(new Error("提交结果仍未知，请继续安全重试。"))
      .mockResolvedValueOnce({ id: "batch-new-input", tasks: [] });
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();
    const prompt = screen.getByLabelText("提示词");
    const open = screen.getByRole("button", { name: "生成视频" });
    fireEvent.change(prompt, { target: { value: "航拍乡墅庭院" } });
    fireEvent.click(open);
    expect(await screen.findByText(/9\.60 元/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));
    expect(
      await screen.findByText("提交结果未知，请安全重试。"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("提交未成功确认，请核对任务及流水"),
    ).toBeInTheDocument();
    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "关闭",
      }),
    );

    fireEvent.click(open);
    expect(await screen.findByText(/12\.00 元/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));
    expect(
      await screen.findByText("提交结果仍未知，请继续安全重试。"),
    ).toBeInTheDocument();
    const firstRequest = api.createIndependentVideoTask.mock.calls[0]?.[0];
    const secondRequest = api.createIndependentVideoTask.mock.calls[1]?.[0];
    expect(secondRequest).toEqual(firstRequest);

    fireEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "关闭",
      }),
    );
    fireEvent.change(prompt, { target: { value: "夜景乡墅庭院" } });
    fireEvent.click(open);
    expect(await screen.findByText(/12\.00 元/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认费用并提交" }));
    await waitFor(() =>
      expect(api.createIndependentVideoTask).toHaveBeenCalledTimes(3),
    );
    const thirdRequest = api.createIndependentVideoTask.mock.calls[2]?.[0];
    expect(thirdRequest.prompt_text).toBe("夜景乡墅庭院");
    expect(thirdRequest.idempotency_key).not.toBe(firstRequest.idempotency_key);
  });

  it("视频参数被拒绝时明确未创建任务且未扣费", async () => {
    api.createIndependentVideoTask.mockRejectedValueOnce(
      Object.assign(new Error("生成参数无效，请检查后重试"), {
        status: 422,
        code: "H3_REQUIRES_CLOUD_STORAGE",
      }),
    );
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "庭院镜头" },
    });
    fireEvent.click(await findEnabledButton("生成视频"));
    fireEvent.click(await findEnabledButton("确认费用并提交"));
    expect(
      await screen.findByText("提交被拒绝 · 未创建任务 · 未扣费"),
    ).toBeInTheDocument();
    expect(api.createIndependentVideoTask).toHaveBeenCalledOnce();
  });

  it("提示词导入：从我的提示词一键回填", async () => {
    api.listUserSavedPrompts.mockResolvedValue([
      {
        id: "sp-1",
        project_id: "project-a",
        name: "庭院黄昏",
        prompt_text: "黄昏光线下的庭院推进镜头",
        created_at: "2026-09-06 12:00:00",
      },
    ]);
    live.loadStudioData.mockResolvedValue(emptyStudioData);
    render(<StudioWorkspace currentUser={reviewUser} />);
    await openVideoPage();

    fireEvent.click(screen.getByRole("button", { name: "导入提示词" }));
    fireEvent.click(await screen.findByText("庭院黄昏"));

    expect((screen.getByLabelText("提示词") as HTMLTextAreaElement).value).toBe(
      "黄昏光线下的庭院推进镜头",
    );
  });
});
