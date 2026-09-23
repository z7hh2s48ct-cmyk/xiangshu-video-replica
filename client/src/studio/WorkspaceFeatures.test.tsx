import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { createReviewData, reviewUser } from "./fixtures";
import { createState } from "./state";
import { WorkspaceNotifications } from "./WorkspaceNotifications";
import { WorkspaceSearch } from "./WorkspaceSearch";

const mocks = vi.hoisted(() => ({
  useStudio: vi.fn(),
  searchWorkspace: vi.fn(),
  getWorkspaceNotifications: vi.fn(),
  markWorkspaceNotificationsRead: vi.fn(),
  getStudioSavedScript: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio: mocks.useStudio }));
vi.mock("../api", async (original) => ({
  ...(await original<object>()),
  ...mocks,
}));
vi.mock("./StudioWorkspace", () => ({
  StudioDialog: ({ children }: { children: ReactNode }) => (
    <div>{children}</div>
  ),
}));
const value = {
  user: reviewUser,
  review: false,
  state: createState(),
  data: createReviewData(),
  navigate: vi.fn(),
  patchState: vi.fn(),
  updateData: vi.fn(),
};
beforeEach(() => {
  vi.clearAllMocks();
  mocks.useStudio.mockReturnValue(value);
  mocks.searchWorkspace.mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    page_size: 12,
  });
});

it("切换关键词后丢弃迟到的旧搜索结果", async () => {
  let complete!: (result: unknown) => void;
  mocks.searchWorkspace.mockImplementationOnce(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  const view = render(<WorkspaceSearch query="旧词" onClose={() => {}} />);
  view.rerender(<WorkspaceSearch query="新词" onClose={() => {}} />);
  await screen.findByText("没有匹配结果");
  await act(async () =>
    complete({
      items: [{ id: "old", kind: "video", title: "旧数据" }],
      total: 1,
      page: 1,
      page_size: 12,
    }),
  );
  expect(screen.queryByText("旧数据")).toBeNull();
  expect(mocks.searchWorkspace).toHaveBeenLastCalledWith("新词", "video", 1);
});

it("搜索读取服务器分页，打开文案重置其他创作来源", async () => {
  mocks.searchWorkspace.mockResolvedValue({
    items: [{ id: "s1", kind: "script", title: "预算文案" }],
    total: 13,
    page: 1,
    page_size: 12,
  });
  mocks.getStudioSavedScript.mockResolvedValue({
    script_id: "s1",
    title: "预算文案",
    text: "正文",
    original: "",
    version: 1,
    ip_id: null,
    source_project_id: null,
    source_kind: null,
  });
  render(<WorkspaceSearch query="预算" onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: "文案" }));
  fireEvent.click(await screen.findByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(mocks.searchWorkspace).toHaveBeenLastCalledWith("预算", "script", 2),
  );
  fireEvent.click(await screen.findByRole("button", { name: "预算文案" }));
  await waitFor(() => expect(value.navigate).toHaveBeenCalledWith("copy"));
  expect(value.patchState).toHaveBeenCalledWith({
    draft: expect.objectContaining({
      referenceIds: [],
      frameConfirmed: false,
      script: expect.objectContaining({ text: "正文", confirmed: false }),
    }),
  });
});

it("通知读取真实未读数，已读成功后重取并保留任务身份", async () => {
  const item = {
    id: "oral:o1",
    task_id: "o1",
    task_kind: "oral_task",
    title: "口播成片",
    status: "SUCCEEDED",
    occurred_at: "2026-09-13T00:00:00Z",
    unread: true,
  };
  mocks.getWorkspaceNotifications.mockResolvedValue({
    enabled: true,
    unread_count: 1,
    items: [item],
  });
  mocks.markWorkspaceNotificationsRead.mockResolvedValue({
    read_before: "2026-09-13T01:00:00Z",
  });
  render(<WorkspaceNotifications />);
  fireEvent.click(screen.getByRole("button", { name: "查看任务动态" }));
  await screen.findByText("最近 50 条任务状态；未读 1 条。");
  mocks.getWorkspaceNotifications.mockResolvedValue({
    enabled: true,
    unread_count: 0,
    items: [{ ...item, unread: false }],
  });
  fireEvent.click(screen.getByRole("button", { name: "全部标为已读" }));
  await screen.findByText("最近 50 条任务状态；未读 0 条。");
  fireEvent.click(screen.getByRole("button", { name: /口播成片 · 已完成/ }));
  expect(value.navigate).toHaveBeenCalledWith("task-detail", {
    selectedTaskId: "oral-o1",
    selectedTaskKind: "oral_task",
    selectedTaskBackendId: "o1",
    returnTo: "tasks",
  });
});

it("标记已读失败不假装清空未读数", async () => {
  mocks.getWorkspaceNotifications.mockResolvedValue({
    enabled: true,
    unread_count: 3,
    items: [],
  });
  mocks.markWorkspaceNotificationsRead.mockRejectedValue(new Error("保存失败"));
  render(<WorkspaceNotifications />);
  fireEvent.click(screen.getByRole("button", { name: "查看任务动态" }));
  await screen.findByText("最近 50 条任务状态；未读 3 条。");
  fireEvent.click(screen.getByRole("button", { name: "全部标为已读" }));
  await screen.findByRole("alert");
  expect(
    screen.getByText("最近 50 条任务状态；未读 3 条。"),
  ).toBeInTheDocument();
});
