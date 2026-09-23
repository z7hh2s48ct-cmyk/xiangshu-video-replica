import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StudioWorkspace } from "./studio/StudioWorkspace";
import { createState } from "./studio/state";

/** 生产入口唯一路径：main.tsx → RootApp → CustomerWorkspace → StudioWorkspace。
 *  内部壳 App.tsx 已随 CW-019 后的死代码清理删除，本套件改为直挂 studio 泳道，
 *  断言不变（它们描述的本来就是 studio 行为）。 */
const testUser = {
  id: "employee_1",
  username: "employee_1",
  display_name: "林夏",
  role: "employee" as const,
};

const studioLive = vi.hoisted(() => ({
  loadProjectDraft: vi.fn(),
}));

vi.mock("./studio/live", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...studioLive,
}));

const batch = {
  id: "batch-created-from-project",
  project_id: "project-1",
  project_name: "夏日咖啡馆口播复刻",
  display_name: "夏日咖啡馆复刻成片",
  creation_kind: "replica",
  prompt_version_id: "prompt-1",
  status: "SUCCEEDED",
  quantity: 1,
  stale: false,
  created_at: "2026-09-17T08:00:00Z",
  progress: {
    total_count: 1,
    terminal_count: 1,
    progress_percent: 100,
    counts: { succeeded: 1 },
  },
  tasks: [],
};

function createAppFetchMock() {
  return vi.fn((url: string) => {
    if (url.endsWith("/api/auth/me")) {
      return Promise.resolve({
        ok: true,
        json: async () => ({
          id: "employee_1",
          username: "employee_1",
          display_name: "林夏",
          role: "employee",
        }),
      });
    }
    if (url.endsWith("/health")) {
      return Promise.resolve({
        ok: true,
        json: async () => ({ status: "ok", service: "video-replica-api" }),
      });
    }
    if (url.endsWith("/api/projects")) {
      return Promise.resolve({
        ok: true,
        json: async () => [
          {
            id: "project-1",
            owner_user_id: "employee_1",
            name: "夏日咖啡馆口播复刻",
            status: "REFERENCE_READY",
            reference_asset_id: "reference-1",
            reference_upload_status: "READY",
            analysis_status: "READY",
          },
        ],
      });
    }
    if (url.includes("/api/generation-batches?")) {
      return Promise.resolve({
        ok: true,
        json: async () => ({ items: [batch], next_cursor: null, total: 1 }),
      });
    }
    if (url.endsWith(`/api/generation-batches/${batch.id}`)) {
      return Promise.resolve({ ok: true, json: async () => batch });
    }
    return Promise.resolve({ ok: true, json: async () => ({}) });
  });
}

async function openCanonicalReplica(
  entry:
    | `打开项目 ${string}`
    | "生成视频"
    | "查看流程" = "打开项目 夏日咖啡馆口播复刻",
) {
  // 首页“上传视频”已是本机文件上传图标；无来源时进入已实现项目区的
  // 入口是“开始复刻”。项目标题、生成和查看入口现在都汇入同一 Replica。
  await screen.findByRole("heading", {
    level: 1,
    name: "粘贴一条爆款乡墅视频链接，快速生成它的原创视频",
  });
  fireEvent.click(screen.getByRole("button", { name: "开始复刻" }));
  fireEvent.click(await screen.findByRole("button", { name: entry }));
  await screen.findByRole("heading", { name: "1 视频拆解" });
}

describe("App canonical replica entry", () => {
  beforeEach(() => {
    const draft = createState("replica").draft;
    draft.projectId = "project-1";
    draft.sourceId = "reference-1";
    draft.script.title = "夏日咖啡馆口播复刻";
    studioLive.loadProjectDraft.mockReset();
    studioLive.loadProjectDraft.mockResolvedValue({ draft, errors: [] });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
    window.location.hash = "";
  });

  it("项目标题入口进入统一复刻页，并保留真实任务记录入口", async () => {
    window.localStorage.clear();
    window.location.hash = "";
    const fetchMock = createAppFetchMock();
    vi.stubGlobal("fetch", fetchMock);

    render(<StudioWorkspace currentUser={testUser} />);
    await openCanonicalReplica();

    expect(window.location.hash).toBe("#studio/replica");
    expect(
      screen.queryByRole("button", { name: "模拟一键生成完成" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "任务中心" }));
    fireEvent.click(await screen.findByRole("button", { name: "查看结果" }));
    expect(
      await screen.findByRole("heading", { name: "夏日咖啡馆复刻成片" }),
    ).toBeInTheDocument();
    expect(window.location.hash).toContain(
      `#studio/task-detail/generation_batch/${batch.id}`,
    );
  });

  it.each(["生成视频", "查看流程"] as const)(
    "%s 入口也进入同一个复刻页",
    async (entry) => {
      window.localStorage.clear();
      window.location.hash = "";
      vi.stubGlobal("fetch", createAppFetchMock());

      render(<StudioWorkspace currentUser={testUser} />);
      await openCanonicalReplica(entry);

      expect(window.location.hash).toBe("#studio/replica");
      expect(
        screen.getByRole("heading", { name: "2 首帧置换" }),
      ).toBeInTheDocument();
      expect(
        screen.getByRole("heading", { name: "3 文案与生成" }),
      ).toBeInTheDocument();
    },
  );

  it("浏览器存储不可用时仍能进入统一复刻页", async () => {
    window.localStorage.clear();
    window.location.hash = "";
    vi.stubGlobal("fetch", createAppFetchMock());
    const storageWrite = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("storage blocked", "SecurityError");
      });

    render(<StudioWorkspace currentUser={testUser} />);
    await openCanonicalReplica();

    expect(window.location.hash).toBe("#studio/replica");
    expect(
      screen.getByRole("heading", { name: "3 文案与生成" }),
    ).toBeInTheDocument();
    storageWrite.mockRestore();
  });
});
