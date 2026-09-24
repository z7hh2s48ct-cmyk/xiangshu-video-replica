import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterLibrary } from "./CharacterLibrary";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    listSimpleCharacterLibrary: vi.fn(),
    listSimpleCharacterLibraryPage: vi.fn(),
    listCharacterSceneLooks: vi.fn(),
    createCharacterSceneLook: vi.fn(),
    regenerateContactSheet: vi.fn(),
    renamePersonIdentity: vi.fn(),
    deleteSimpleCharacterIdentity: vi.fn(),
    uploadSimpleCharacter: vi.fn(),
    getLatestCharacterSheetTask: vi.fn(),
    getLatestSceneLookTask: vi.fn(),
    waitForCharacterSheetTask: vi.fn(),
    getCachedCharacterAssetUrl: vi.fn(),
    downloadCharacterAsset: vi.fn(),
  };
});

const VIEW_TYPES: api.CharacterViewType[] = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "LEFT_SIDE",
];

function viewsFor(prefix: string): api.SimpleCharacterView[] {
  return VIEW_TYPES.map((view_type, index) => ({
    view_type,
    asset_id: `${prefix}-${view_type}-${index}`,
  }));
}

const entry: api.SimpleLibraryEntry = {
  identity_id: "identity-1",
  persona_id: "persona-1",
  version_number: 1,
  display_name: "林夏",
  role: "",
  service_scope: "",
  target_audience: "",
  expression_style: "",
  owner_user_id: "employee_1",
  status: "ACTIVE",
  contact_sheet_asset_id: null,
  generation_source: "image_provider",
  scene_look_count: 0,
  views: viewsFor("asset"),
};

const foreignEntry: api.SimpleLibraryEntry = {
  ...entry,
  identity_id: "identity-2",
  display_name: "荣哥",
  owner_user_id: "employee_2",
  contact_sheet_asset_id: "sheet-foreign",
  views: viewsFor("foreign"),
};

const sceneLook: api.SimpleSceneLook = {
  identity_id: "identity-1",
  persona_id: "persona-scene-1",
  character_version_id: "version-scene-1",
  scene_name: "工地巡检",
  scene_description: "乡村别墅施工现场，白天自然光",
  costume_description: "黄色安全帽、深蓝色工装和反光背心",
  contact_sheet_asset_id: "sheet-scene-1",
  generation_source: "image_provider",
  views: viewsFor("scene"),
  published_at: "2026-09-01T08:00:00Z",
};

function characterTask(
  status: api.DurableImageTaskStatus,
  overrides: Partial<api.CharacterSheetTask> = {},
): api.CharacterSheetTask {
  return {
    id: "character-task-1",
    project_id: null,
    identity_id: null,
    operation: "CREATE",
    display_name: "林夏",
    status,
    attempt: 1,
    result_identity_id: status === "SUCCEEDED" ? entry.identity_id : null,
    result_version_id: status === "SUCCEEDED" ? "version-1" : null,
    result: null,
    error_code: null,
    error_message: null,
    retryable: status === "FAILED",
    created_at: "2026-08-30T00:00:00Z",
    updated_at: "2026-08-30T00:01:00Z",
    started_at: "2026-08-30T00:00:10Z",
    completed_at:
      status === "PENDING" || status === "RUNNING"
        ? null
        : "2026-08-30T00:01:00Z",
    ...overrides,
  };
}

describe("CharacterLibrary", () => {
  it("管理入口能定位不在第一页的人物", async () => {
    vi.mocked(api.listSimpleCharacterLibraryPage)
      .mockResolvedValueOnce({ items: [], next_cursor: "page2", total: 1 })
      .mockResolvedValue({ items: [entry], next_cursor: null, total: 1 });
    render(
      <CharacterLibrary
        userId="employee_1"
        userRole="employee"
        initialIdentityId={entry.identity_id}
      />,
    );
    expect(
      await screen.findByRole("dialog", { name: /林夏/ }),
    ).toBeInTheDocument();
    expect(api.listSimpleCharacterLibraryPage).toHaveBeenCalledWith(
      expect.objectContaining({ cursor: "page2" }),
    );
  });
  it("收起场景表单后仍显示生成任务，完成后展示同一套结果", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    let complete!: (look: api.SimpleSceneLook) => void;
    vi.mocked(api.createCharacterSceneLook).mockReturnValueOnce(
      new Promise((resolve) => {
        complete = resolve;
      }),
    );
    render(<CharacterLibrary userRole="employee" userId="employee_1" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "查看人物 林夏 大图" }),
    );
    fireEvent.click(screen.getByRole("tab", { name: "场景造型" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "新增场景造型" }),
    );
    await waitFor(() => expect(api.getLatestSceneLookTask).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("场景名称"), {
      target: { value: "工地巡检" },
    });
    fireEvent.change(screen.getByLabelText("场景描述"), {
      target: { value: "施工现场" },
    });
    fireEvent.change(screen.getByLabelText("服装描述"), {
      target: { value: "安全帽工装" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成场景五视图" }));
    fireEvent.click(screen.getByRole("button", { name: "收起" }));
    expect(
      screen.getByRole("status", { name: "场景造型生成进度" }),
    ).toHaveTextContent("工地巡检");
    complete(sceneLook);
    expect(
      await screen.findByRole("button", { name: "查看工地巡检五视图" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("status", { name: "场景造型生成进度" }),
    ).toBeNull();
  });

  beforeEach(() => {
    // resetAllMocks (not clearAllMocks) also drops leftover mockResolvedValueOnce
    // queues from earlier tests, which would otherwise leak into this one.
    vi.resetAllMocks();
    vi.mocked(api.getCachedCharacterAssetUrl).mockImplementation(
      async (assetId) => ({
        url: `http://127.0.0.1:8000/mock/${assetId}`,
      }),
    );
    vi.mocked(api.downloadCharacterAsset).mockResolvedValue(undefined);
    vi.mocked(api.deleteSimpleCharacterIdentity).mockResolvedValue(undefined);
    vi.mocked(api.listCharacterSceneLooks).mockResolvedValue([]);
    vi.mocked(api.getLatestCharacterSheetTask).mockResolvedValue(null);
    vi.mocked(api.getLatestSceneLookTask).mockResolvedValue(null);
    vi.mocked(api.listSimpleCharacterLibraryPage).mockImplementation(
      async () => {
        const items = await api.listSimpleCharacterLibrary();
        return { items, next_cursor: null, total: items.length };
      },
    );
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("从 Studio 指定人物进入时直接打开该人物的场景造型", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(
      <CharacterLibrary
        initialIdentityId="identity-1"
        initialTab="scenes"
        userRole="employee"
        userId="employee_1"
      />,
    );

    expect(
      await screen.findByRole("tab", { name: "场景造型" }),
    ).toHaveAttribute("aria-selected", "true");
    await waitFor(() =>
      expect(api.listCharacterSceneLooks).toHaveBeenCalledWith("identity-1"),
    );
  });

  it("展示后端场景造型数量并从统一页面进入完整档案", async () => {
    const onOpenProfile = vi.fn();
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([
      { ...entry, scene_look_count: 2 },
    ]);

    render(
      <CharacterLibrary
        onOpenProfile={onOpenProfile}
        userRole="employee"
        userId="employee_1"
      />,
    );

    expect(await screen.findByText("场景造型 2 套")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "完整档案" }));
    expect(onOpenProfile).toHaveBeenCalledWith("identity-1");
  });

  it("支持搜索人物并通过加载更多访问第十三个人物", async () => {
    const firstPage = Array.from({ length: 12 }, (_, index) => ({
      ...entry,
      identity_id: `identity-${index + 1}`,
      display_name: `人物${index + 1}`,
      views: viewsFor(`asset-${index + 1}`),
    }));
    const thirteenth = {
      ...entry,
      identity_id: "identity-13",
      display_name: "人物13",
      views: viewsFor("asset-13"),
    };
    vi.mocked(api.listSimpleCharacterLibraryPage)
      .mockResolvedValueOnce({
        items: firstPage,
        next_cursor: "page-2",
        total: 13,
      })
      .mockResolvedValueOnce({
        items: [thirteenth],
        next_cursor: null,
        total: 13,
      })
      .mockResolvedValueOnce({
        items: [firstPage[11]],
        next_cursor: null,
        total: 13,
      })
      .mockResolvedValueOnce({ items: [], next_cursor: null, total: 13 });

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByText("人物1")).toBeInTheDocument();
    expect(screen.queryByText("人物13")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "加载更多人物" }));
    expect(await screen.findByText("人物13")).toBeInTheDocument();
    expect(api.listSimpleCharacterLibraryPage).toHaveBeenNthCalledWith(2, {
      limit: 12,
      cursor: "page-2",
      query: "",
    });

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索人物" }), {
      target: { value: "人物12" },
    });
    expect(await screen.findByText("人物12")).toBeInTheDocument();
    await waitFor(() =>
      expect(api.listSimpleCharacterLibraryPage).toHaveBeenNthCalledWith(3, {
        limit: 12,
        cursor: undefined,
        query: "人物12",
      }),
    );
    expect(screen.queryByText("人物1")).not.toBeInTheDocument();

    fireEvent.change(screen.getByRole("searchbox", { name: "搜索人物" }), {
      target: { value: "不存在" },
    });
    expect(
      await screen.findByText("未找到匹配人物，请更换搜索词。"),
    ).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "搜索人物" })).toHaveValue(
      "不存在",
    );
  });

  it("新建人物取代正在请求的下一页时恢复加载更多按钮", async () => {
    let finishPage!: (page: api.SimpleLibraryPage) => void;
    const pendingPage = new Promise<api.SimpleLibraryPage>((resolve) => {
      finishPage = resolve;
    });
    vi.mocked(api.listSimpleCharacterLibraryPage)
      .mockResolvedValueOnce({
        items: [entry],
        next_cursor: "page-2",
        total: 2,
      })
      .mockReturnValueOnce(pendingPage);
    vi.mocked(api.uploadSimpleCharacter).mockResolvedValue({
      identity_id: "identity-new",
      persona_id: "persona-new",
      character_version_id: "version-new",
      publication_hash: "publication-new",
      contact_sheet_asset_id: "sheet-new",
      generation_source: "image_provider",
      views: viewsFor("new"),
    });

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "加载更多人物" }),
    );
    expect(screen.getByRole("button", { name: "正在加载…" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "新人物" },
    });
    fireEvent.change(screen.getByLabelText("授权图片"), {
      target: {
        files: [new File(["png"], "source.png", { type: "image/png" })],
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "一键生成五视图拼合图" }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: "我已阅读并确认以上图像授权声明" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认授权并生成" }));

    await waitFor(() =>
      expect(api.uploadSimpleCharacter).toHaveBeenCalledWith(
        null,
        expect.any(File),
        "新人物",
        "",
        "2026-09-14-v1",
      ),
    );
    expect(
      await screen.findByRole("button", { name: "查看人物 新人物 大图" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "加载更多人物" })).toBeEnabled();
    finishPage({ items: [], next_cursor: null, total: 2 });
  });

  it("separates the base appearance from scene looks and directly generates a new look", async () => {
    const onChanged = vi.fn();
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.mocked(api.listCharacterSceneLooks).mockResolvedValue([sceneLook]);
    vi.mocked(api.createCharacterSceneLook).mockResolvedValue({
      ...sceneLook,
      persona_id: "persona-scene-2",
      character_version_id: "version-scene-2",
      scene_name: "商务讲解",
      scene_description: "现代会议室，落地窗自然光",
      costume_description: "深灰色西装和浅色衬衫",
    });

    render(
      <CharacterLibrary
        onChanged={onChanged}
        userRole="employee"
        userId="employee_1"
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "查看人物 林夏 大图" }),
    );

    expect(screen.getByRole("tab", { name: "人物基准" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "场景造型" }));

    expect(await screen.findByText("工地巡检")).toBeInTheDocument();
    expect(
      screen.getByText("黄色安全帽、深蓝色工装和反光背心"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看工地巡检五视图" }));
    const sceneViews = screen.getByRole("region", {
      name: "工地巡检五视图",
    });
    expect(
      await within(sceneViews).findByRole("img", {
        name: "林夏 工地巡检 场景五视图",
      }),
    ).toHaveAttribute("src", "http://127.0.0.1:8000/mock/sheet-scene-1");
    fireEvent.click(screen.getByRole("button", { name: "新增场景造型" }));
    fireEvent.change(screen.getByLabelText("场景名称"), {
      target: { value: "商务讲解" },
    });
    fireEvent.change(screen.getByLabelText("场景描述"), {
      target: { value: "现代会议室，落地窗自然光" },
    });
    fireEvent.change(screen.getByLabelText("服装描述"), {
      target: { value: "深灰色西装和浅色衬衫" },
    });
    fireEvent.click(screen.getByRole("button", { name: "生成场景五视图" }));

    await waitFor(() =>
      expect(api.createCharacterSceneLook).toHaveBeenCalledWith(
        "identity-1",
        {
          scene_name: "商务讲解",
          scene_description: "现代会议室，落地窗自然光",
          costume_description: "深灰色西装和浅色衬衫",
        },
        expect.any(Function),
      ),
    );
    expect(await screen.findByText("商务讲解")).toBeInTheDocument();
    expect(screen.queryByText("等待管理员审核")).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  it("restores a scene task inside its identity scene tab", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.mocked(api.listCharacterSceneLooks)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([sceneLook]);
    const running = characterTask("RUNNING", {
      id: "scene-task-1",
      identity_id: entry.identity_id,
      operation: "SCENE",
      display_name: entry.display_name,
    });
    vi.mocked(api.getLatestSceneLookTask).mockResolvedValue(running);
    vi.mocked(api.waitForCharacterSheetTask).mockResolvedValue(
      characterTask("SUCCEEDED", {
        ...running,
        status: "SUCCEEDED",
        result_identity_id: entry.identity_id,
        result_version_id: sceneLook.character_version_id,
        result: sceneLook,
      }),
    );

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);
    fireEvent.click(
      await screen.findByRole("button", { name: "查看人物 林夏 大图" }),
    );

    expect(
      await screen.findByRole("tab", { name: "场景造型" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByText("工地巡检")).toBeInTheDocument();
    expect(api.waitForCharacterSheetTask).toHaveBeenCalledWith(
      "scene-task-1",
      expect.any(Function),
    );
    expect(api.listCharacterSceneLooks).toHaveBeenCalledTimes(1);
  });

  it("does not render a scene task as a base-character generation card", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.mocked(api.getLatestCharacterSheetTask).mockResolvedValue(
      characterTask("RUNNING", {
        identity_id: entry.identity_id,
        operation: "SCENE",
      }),
    );

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    await waitFor(() =>
      expect(api.getLatestCharacterSheetTask).toHaveBeenCalledTimes(1),
    );
    expect(screen.queryByLabelText("人物 林夏 生成进度")).toBeNull();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders front-face covers and moves detail views into the lightbox", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([
      entry,
      foreignEntry,
    ]);

    render(<CharacterLibrary userRole="admin" userId="admin_1" />);

    // 卡片封面一律用正脸近景，下载与拼合详情收进灯箱。
    expect(await screen.findByAltText("林夏 正脸近景")).toBeInTheDocument();
    expect(screen.getByAltText("荣哥 正脸近景")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "下载拼合图" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "下载全部（5 张）" }),
    ).toBeNull();

    // 旧人物（无拼合图）灯箱=视角网格 + 下载全部。
    fireEvent.click(screen.getByRole("button", { name: "查看人物 林夏 大图" }));
    expect(
      await screen.findByRole("dialog", { name: "人物预览 林夏" }),
    ).toBeInTheDocument();
    expect(screen.getByAltText("林夏 左侧面")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载全部（5 张）" }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭人物预览" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    // 新人物灯箱=五视图拼合大图。
    fireEvent.click(screen.getByRole("button", { name: "查看人物 荣哥 大图" }));
    expect(await screen.findByAltText("荣哥 五视图拼合图")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "下载拼合图" }),
    ).toBeInTheDocument();
    expect(api.getCachedCharacterAssetUrl).toHaveBeenCalledWith(
      entry.views[0].asset_id,
    );
    expect(api.getCachedCharacterAssetUrl).toHaveBeenCalledWith(
      foreignEntry.contact_sheet_asset_id,
    );
  });

  it("closes the lightbox via Esc, the backdrop, and the close button", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    const { container } = render(
      <CharacterLibrary userRole="employee" userId="employee_1" />,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "查看人物 林夏 大图" }),
    );
    expect(
      await screen.findByRole("dialog", { name: "人物预览 林夏" }),
    ).toBeInTheDocument();

    // Esc 关闭。
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();

    // 点遮罩关闭；点对话框本体不关闭。
    fireEvent.click(screen.getByRole("button", { name: "查看人物 林夏 大图" }));
    fireEvent.click(screen.getByRole("dialog", { name: "人物预览 林夏" }));
    expect(
      screen.getByRole("dialog", { name: "人物预览 林夏" }),
    ).toBeInTheDocument();
    const backdrop = container.querySelector(".character-lightbox");
    expect(backdrop).not.toBeNull();
    fireEvent.click(backdrop as HTMLElement);
    expect(screen.queryByRole("dialog")).toBeNull();

    // 右上「关闭」按钮。
    fireEvent.click(screen.getByRole("button", { name: "查看人物 林夏 大图" }));
    fireEvent.click(screen.getByRole("button", { name: "关闭人物预览" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("creates a character and shows its contact sheet immediately", async () => {
    vi.mocked(api.listSimpleCharacterLibrary)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        { ...entry, contact_sheet_asset_id: "sheet-new" },
      ]);
    vi.mocked(api.uploadSimpleCharacter).mockResolvedValue({
      identity_id: entry.identity_id,
      persona_id: "persona-1",
      character_version_id: "version-1",
      publication_hash: "publication-sha",
      contact_sheet_asset_id: "sheet-new",
      generation_source: "image_provider",
      views: entry.views,
    });

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "林夏" },
    });
    fireEvent.change(screen.getByLabelText("授权图片"), {
      target: {
        files: [new File(["webp"], "source.webp", { type: "image/webp" })],
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "一键生成五视图拼合图" }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: "我已阅读并确认以上图像授权声明" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认授权并生成" }));

    await waitFor(() =>
      expect(api.uploadSimpleCharacter).toHaveBeenCalledWith(
        null,
        expect.any(File),
        "林夏",
        "",
        "2026-09-14-v1",
      ),
    );
    expect(vi.mocked(api.uploadSimpleCharacter).mock.calls[0]?.[1].type).toBe(
      "image/webp",
    );
    expect(await screen.findByAltText("林夏 正脸近景")).toBeInTheDocument();
    expect(screen.getByText(/五视图拼合图已生成/)).toBeInTheDocument();
  });

  it("shows the source image and progress card while generation is running", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([]);
    let finishGeneration!: (result: api.SimpleCharacterResult) => void;
    const generation = new Promise<api.SimpleCharacterResult>((resolve) => {
      finishGeneration = resolve;
    });
    vi.mocked(api.uploadSimpleCharacter).mockReturnValue(generation);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.change(screen.getByLabelText("人物名称"), {
      target: { value: "林夏" },
    });
    fireEvent.change(screen.getByLabelText("授权图片"), {
      target: {
        files: [new File(["png"], "source.png", { type: "image/png" })],
      },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "一键生成五视图拼合图" }),
    );
    fireEvent.click(
      screen.getByRole("checkbox", { name: "我已阅读并确认以上图像授权声明" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认授权并生成" }));

    expect(
      await screen.findByLabelText("人物 林夏 生成进度"),
    ).toBeInTheDocument();
    expect(screen.getByAltText("林夏 授权原图")).toBeInTheDocument();
    expect(screen.getByLabelText("林夏 预计生成进度")).toHaveValue(8);
    expect(screen.getByText(/可离开当前页面.*后台继续/)).toBeInTheDocument();

    finishGeneration({
      identity_id: entry.identity_id,
      persona_id: "persona-1",
      character_version_id: "version-1",
      publication_hash: "publication-sha",
      contact_sheet_asset_id: "sheet-new",
      generation_source: "image_provider",
      views: entry.views,
    });

    expect(await screen.findByAltText("林夏 正脸近景")).toBeInTheDocument();
    expect(screen.queryByLabelText("人物 林夏 生成进度")).toBeNull();
  });

  it("reloads the library when a completed background task is recovered", async () => {
    vi.mocked(api.listSimpleCharacterLibrary)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        { ...entry, contact_sheet_asset_id: "sheet-new" },
      ]);
    vi.mocked(api.getLatestCharacterSheetTask).mockResolvedValue(
      characterTask("SUCCEEDED"),
    );

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByAltText("林夏 正脸近景")).toBeInTheDocument();
    expect(screen.getByText(/人物“林夏”五视图已生成/)).toBeInTheDocument();
    expect(api.listSimpleCharacterLibrary).toHaveBeenCalledTimes(2);
    expect(api.waitForCharacterSheetTask).not.toHaveBeenCalled();
  });

  it.each([
    ["FAILED", "图片服务拒绝了请求"],
    ["SUBMISSION_UNCERTAIN", "云端提交结果暂时无法确认"],
  ] as const)(
    "restores a %s background task as an actionable error",
    async (status, message) => {
      vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([]);
      vi.mocked(api.getLatestCharacterSheetTask).mockResolvedValue(
        characterTask(status, { error_message: message }),
      );

      render(<CharacterLibrary userRole="employee" userId="employee_1" />);

      expect(await screen.findByText(message)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "移除失败任务" }),
      ).toBeInTheDocument();
      expect(api.waitForCharacterSheetTask).not.toHaveBeenCalled();
    },
  );

  it("marks non-provider placeholder publications explicitly", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([
      { ...entry, generation_source: "local_placeholder" },
    ]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByText("本地占位结果")).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "重新生成人物 林夏 的五视图",
      }),
    ).toBeInTheDocument();
  });

  it("shows a retry action instead of leaving a failed preview loading forever", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    const coverId = entry.views[0].asset_id;
    let coverAttempts = 0;
    vi.mocked(api.getCachedCharacterAssetUrl).mockImplementation(
      async (assetId) => {
        if (assetId === coverId && coverAttempts++ === 0) {
          throw new Error("cache unavailable");
        }
        return { url: `http://127.0.0.1:8000/mock/${assetId}` };
      },
    );

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    expect(await screen.findByText("预览加载失败")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看人物 林夏 大图" }));

    expect(await screen.findByAltText("林夏 正脸近景")).toBeInTheDocument();
    expect(coverAttempts).toBe(2);
  });

  it("downloads the five-view contact sheet", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "查看人物 荣哥 大图" }),
    );
    fireEvent.click(await screen.findByRole("button", { name: "下载拼合图" }));

    await waitFor(() =>
      expect(api.downloadCharacterAsset).toHaveBeenCalledWith(
        "sheet-foreign",
        "荣哥-五视图拼合图.png",
      ),
    );
  });

  it("downloads a single view and the whole set", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "查看人物 林夏 大图" }),
    );
    const downloadButtons = await screen.findAllByRole("button", {
      name: "下载",
    });
    fireEvent.click(downloadButtons[0]);

    await waitFor(() =>
      expect(api.downloadCharacterAsset).toHaveBeenCalledWith(
        entry.views[0].asset_id,
        "林夏-正脸近景.png",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "下载全部（5 张）" }));

    await waitFor(() =>
      expect(api.downloadCharacterAsset).toHaveBeenCalledTimes(6),
    );
  });

  it("lets the owner rename an identity inline", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.mocked(api.renamePersonIdentity).mockResolvedValue({
      id: "identity-1",
      display_name: "林小夏",
    } as unknown as api.PersonIdentity);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "改名" }));
    fireEvent.change(screen.getByLabelText("修改人物名称"), {
      target: { value: "林小夏" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    await waitFor(() =>
      expect(api.renamePersonIdentity).toHaveBeenCalledWith(
        "identity-1",
        "林小夏",
      ),
    );
    expect(await screen.findByText("林小夏")).toBeInTheDocument();
    expect(screen.getByText(/人物名称已更新/)).toBeInTheDocument();
  });

  it("lets an admin rename identities created by others", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);
    vi.mocked(api.renamePersonIdentity).mockResolvedValue({
      id: "identity-2",
      display_name: "荣哥二号",
    } as unknown as api.PersonIdentity);

    render(<CharacterLibrary userRole="admin" userId="admin_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "改名" }));
    fireEvent.change(screen.getByLabelText("修改人物名称"), {
      target: { value: "荣哥二号" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    await waitFor(() =>
      expect(api.renamePersonIdentity).toHaveBeenCalledWith(
        "identity-2",
        "荣哥二号",
      ),
    );
    expect(await screen.findByText("荣哥二号")).toBeInTheDocument();
  });

  it("does not render identities owned by another workspace account", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    await waitFor(() =>
      expect(api.listSimpleCharacterLibrary).toHaveBeenCalled(),
    );
    expect(screen.queryByText("林夏")).toBeNull();
    expect(screen.queryByRole("button", { name: "改名" })).toBeNull();
  });

  it("deletes a character after confirmation", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "删除人物 林夏" }),
    );

    expect(window.confirm).toHaveBeenCalledWith(
      expect.stringContaining("删除“林夏”？"),
    );
    await waitFor(() =>
      expect(api.deleteSimpleCharacterIdentity).toHaveBeenCalledWith(
        "identity-1",
      ),
    );
    expect(await screen.findByText(/人物“林夏”已删除。/)).toBeInTheDocument();
    expect(screen.queryByText("林夏")).toBeNull();
  });

  it("keeps the character when the delete confirmation is dismissed", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    fireEvent.click(
      await screen.findByRole("button", { name: "删除人物 林夏" }),
    );

    expect(api.deleteSimpleCharacterIdentity).not.toHaveBeenCalled();
    expect(screen.getByText("林夏")).toBeInTheDocument();
  });

  it("does not expose delete controls for filtered foreign identities", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    await waitFor(() =>
      expect(api.listSimpleCharacterLibrary).toHaveBeenCalled(),
    );
    expect(screen.queryByText("荣哥")).toBeNull();
    expect(screen.queryByRole("button", { name: "删除人物 荣哥" })).toBeNull();
  });

  it("keeps auditors read-only without download controls", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);

    render(<CharacterLibrary userRole="auditor" userId="auditor_1" />);

    expect(await screen.findByText("林夏")).toBeInTheDocument();
    expect(screen.queryByLabelText("一键上传人物")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "一键生成五视图拼合图" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "改名" })).toBeNull();
    expect(screen.queryByRole("button", { name: "删除人物 林夏" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: "下载全部（5 张）" }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "下载拼合图" })).toBeNull();
    expect(screen.queryAllByRole("button", { name: "下载" })).toHaveLength(0);
    expect(screen.getByText(/审计身份只读/)).toBeInTheDocument();

    // 审计可开灯箱看图，但灯箱内同样没有下载按钮。
    fireEvent.click(screen.getByRole("button", { name: "查看人物 林夏 大图" }));
    expect(
      await screen.findByRole("dialog", { name: "人物预览 林夏" }),
    ).toBeInTheDocument();
    expect(screen.getByAltText("林夏 左侧面")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "下载全部（5 张）" }),
    ).toBeNull();
    expect(screen.queryAllByRole("button", { name: "下载" })).toHaveLength(0);
  });

  it("shows a recoverable error and stays editable when renaming fails", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([entry]);
    vi.mocked(api.renamePersonIdentity).mockRejectedValue(
      new Error("已归档人物身份不能修改。"),
    );

    render(<CharacterLibrary userRole="admin" userId="admin_1" />);

    fireEvent.click(await screen.findByRole("button", { name: "改名" }));
    fireEvent.change(screen.getByLabelText("修改人物名称"), {
      target: { value: "新名字" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存名称" }));

    expect(
      await screen.findByText("已归档人物身份不能修改。"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("修改人物名称")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  });

  it("regenerates the contact sheet as a new version and refreshes the library", async () => {
    vi.mocked(api.listSimpleCharacterLibrary)
      .mockResolvedValueOnce([foreignEntry])
      .mockResolvedValueOnce([
        { ...foreignEntry, contact_sheet_asset_id: "sheet-new" },
      ]);
    vi.mocked(api.regenerateContactSheet).mockResolvedValue({
      identity_id: "identity-2",
      persona_id: "persona-2",
      character_version_id: "cv-2",
      previous_version_id: "cv-1",
      version_number: 2,
      publication_hash: "publication-sha-2",
      contact_sheet_asset_id: "sheet-new",
      generation_source: "image_provider",
      views: viewsFor("new"),
    });

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "重新生成人物 荣哥 的五视图",
      }),
    );

    await waitFor(() =>
      expect(api.regenerateContactSheet).toHaveBeenCalledWith("identity-2"),
    );
    expect(
      await screen.findByText(/五视图已重新生成（V2）/),
    ).toBeInTheDocument();
    expect(api.listSimpleCharacterLibrary).toHaveBeenCalledTimes(2);
  });

  it("shows a recoverable error when regeneration fails", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);
    vi.mocked(api.regenerateContactSheet).mockRejectedValue(
      new Error("生成服务暂不可用。"),
    );

    render(<CharacterLibrary userRole="employee" userId="employee_2" />);

    fireEvent.click(
      await screen.findByRole("button", {
        name: "重新生成人物 荣哥 的五视图",
      }),
    );

    expect(await screen.findByText("生成服务暂不可用。")).toBeInTheDocument();
    // 失败后按钮恢复可用，人物卡片仍在列表中。
    expect(
      screen.getByRole("button", { name: "重新生成人物 荣哥 的五视图" }),
    ).toBeEnabled();
  });

  it("does not render a non-owner's character entry", async () => {
    vi.mocked(api.listSimpleCharacterLibrary).mockResolvedValue([foreignEntry]);

    render(<CharacterLibrary userRole="employee" userId="employee_1" />);

    await waitFor(() =>
      expect(api.listSimpleCharacterLibrary).toHaveBeenCalledTimes(1),
    );
    expect(screen.queryByText("荣哥")).toBeNull();
    expect(
      screen.queryByRole("button", {
        name: "重新生成人物 荣哥 的五视图",
      }),
    ).toBeNull();
  });
});
