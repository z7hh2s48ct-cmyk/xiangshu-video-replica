import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { StudioWorkspace } from "./studio/StudioWorkspace";

// 本套件只覆盖任务记录页「粘贴 Batch ID 查询」的 Fake provider 契约，
// 不经过工作区动线。原经内部壳 App.tsx 挂载；该壳已随泳道 A 死代码清理删除，
// 现直挂 studio 泳道（生产唯一入口），断言不变。

const testUser = {
  id: "employee_1",
  username: "employee_1",
  display_name: "林夏",
  role: "employee" as const,
};

const healthResponse = { status: "ok", service: "video-replica-api" };

const completedFakeBatch = {
  id: "batch-fake-e2e",
  status: "SUCCEEDED",
  quantity: 2,
  progress: {
    total_count: 2,
    terminal_count: 2,
    progress_percent: 100,
    counts: {
      pending: 0,
      submitting: 0,
      queued: 0,
      running: 0,
      archiving: 0,
      succeeded: 2,
      failed: 0,
      cancelled: 0,
      needs_attention: 0,
    },
  },
  tasks: [
    {
      id: "task-fake-1",
      status: "SUCCEEDED",
      archive_status: "ARCHIVED",
      quality_status: "AUDIO_OK",
      quality_issue_codes: [],
      result_asset_id: "asset-fake-1",
      prompt_snapshot: { status: "LOCKED" },
    },
    {
      id: "task-fake-2",
      status: "SUCCEEDED",
      archive_status: "ARCHIVED",
      quality_status: "AUDIO_OK",
      quality_issue_codes: [],
      result_asset_id: "asset-fake-2",
      prompt_snapshot: { status: "LOCKED" },
    },
  ],
};

describe("Fake provider E2E contract", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    window.localStorage.clear();
  });

  it("renders the locked prompt batch progress and archived fake results", async () => {
    const fetchMock = vi.fn((url: string) => {
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
          json: async () => healthResponse,
        });
      }
      // 项目列表端点必须返回数组：fallback 若透传 completedFakeBatch
      // 对象，ProjectsPage 的 projects.some 会在挂载竞态窗口内崩溃。
      if (url.endsWith("/api/projects")) {
        return Promise.resolve({ ok: true, json: async () => [] });
      }
      return Promise.resolve({
        ok: true,
        json: async () => completedFakeBatch,
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<StudioWorkspace currentUser={testUser} />);
    // 任务中心先展示本地任务概览，再由真实 LiveWorkspacePanel 打开记录。
    fireEvent.click(await screen.findByRole("button", { name: "任务中心" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "历史任务与下载" }),
    );
    await screen.findByRole("region", { name: "任务记录" });
    fireEvent.click(screen.getByText("兼容查询：通过 Batch ID 查找历史记录"));
    fireEvent.change(await screen.findByLabelText("Batch ID"), {
      target: { value: " batch-fake-e2e " },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询任务记录" }));

    // 任务页默认落在生成结果舞台（客户视角）；本契约断言的是运维视角的
    // 批次/任务事实，先切换到运维详情。
    fireEvent.click(await screen.findByRole("button", { name: "运维详情" }));

    expect(await screen.findByText("100%")).toBeInTheDocument();
    expect(screen.getByText("任务已结束 2 / 2")).toBeInTheDocument();
    expect(screen.getByText("task-fake-1")).toBeInTheDocument();
    expect(screen.getByText("task-fake-2")).toBeInTheDocument();
    expect(screen.getAllByText("阶段：已完成")).toHaveLength(2);
    expect(screen.queryByText("需要处理")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: /^下载 MP4 / })).toHaveLength(
      2,
    );
    expect(window.localStorage.getItem("generation.batchId:employee_1")).toBe(
      "batch-fake-e2e",
    );
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "http://127.0.0.1:8000/api/generation-batches/batch-fake-e2e",
        expect.objectContaining({
          headers: expect.any(Headers),
          signal: expect.any(AbortSignal),
        }),
      ),
    );
  });
});
