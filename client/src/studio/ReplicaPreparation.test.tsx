import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { createDraft, patchStudioDraft } from "./state";
import type { StudioDraft } from "./types";

const mocks = vi.hoisted(() => ({
  rewrite: vi.fn(),
  wait: vi.fn(),
  optimize: vi.fn(),
  latest: vi.fn(),
  task: vi.fn(),
  useStudio: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio: mocks.useStudio }));
vi.mock("../api", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  rewriteProjectScript: mocks.rewrite,
  waitForScriptRewriteTask: mocks.wait,
  getLatestScriptRewriteTask: mocks.latest,
  getScriptRewriteTask: mocks.task,
  createPromptOptimization: mocks.optimize,
  capturePromptSession: () => () => true,
}));

import { ReplicaNarration } from "./ReplicaPreparation";

function Harness({ initial }: { initial?: Partial<StudioDraft> } = {}) {
  const [draft, setDraft] = useState({
    ...createDraft(),
    projectId: "p1",
    sourceAssetId: "a1",
    replicaSourcePrompt: "原始动作与旧台词",
    script: { ...createDraft().script, text: "当前口播" },
    ...initial,
  });
  mocks.useStudio.mockReturnValue({
    state: { draft },
    user: { id: "u1", role: "customer" },
    review: false,
    patchDraft: (patch: Partial<StudioDraft>) =>
      setDraft((old) => patchStudioDraft(old, patch) as typeof draft),
  });
  return (
    <>
      <ReplicaNarration />
      <button
        type="button"
        onClick={() =>
          setDraft(
            (old) => patchStudioDraft(old, { projectId: "p2" }) as typeof draft,
          )
        }
      >
        换项目
      </button>
    </>
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  mocks.latest.mockResolvedValue(null);
  mocks.task.mockResolvedValue(null);
  mocks.rewrite.mockResolvedValue({ id: "rw1", status: "RUNNING" });
  mocks.wait.mockResolvedValue({
    id: "rw1",
    status: "SUCCEEDED",
    result: { rewritten_text: "AI 新口播" },
  });
});
it("在口播文案区域确认，编辑后须重新确认", () => {
  render(<Harness />);
  const panel = screen
    .getByLabelText("口播文案")
    .closest(".creation-replica-narration");
  const confirm = screen.getByRole("button", { name: /^确认$/ });
  expect(panel).toContainElement(confirm);
  fireEvent.click(confirm);
  expect(screen.getByRole("button", { name: "已确认" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("口播文案"), {
    target: { value: "编辑后新文案" },
  });
  expect(screen.getByRole("button", { name: /^确认$/ })).toBeEnabled();
});
it("文案为空时确认按钮提示「确认无口播」", () => {
  render(
    <Harness initial={{ script: { ...createDraft().script, text: "" } }} />,
  );
  const confirm = screen.getByRole("button", { name: "确认无口播" });
  expect(confirm).toBeEnabled();
  fireEvent.click(confirm);
  expect(screen.getByRole("button", { name: "已确认" })).toBeDisabled();
  // 填入文案后回到常规口径：这是带内容的口播确认。
  fireEvent.change(screen.getByLabelText("口播文案"), {
    target: { value: "重新口播" },
  });
  expect(screen.getByRole("button", { name: /^确认$/ })).toBeEnabled();
});
it("AI 改写覆盖唯一文案框并支持撤销", async () => {
  render(<Harness />);
  // ⑦ 口播文案框改为内容自适应高度，起始 3 行（原固定 10 行）。
  expect(screen.getByLabelText("口播文案")).toHaveAttribute("rows", "3");
  fireEvent.click(screen.getByRole("button", { name: "AI 改写" }));
  await waitFor(() =>
    expect(screen.getByLabelText("口播文案")).toHaveValue("AI 新口播"),
  );
  fireEvent.click(screen.getByRole("button", { name: "撤销上次改写" }));
  expect(screen.getByLabelText("口播文案")).toHaveValue("当前口播");
});
it.each(["edit", "project"])(
  "迟到改写不覆盖用户编辑或其他项目：%s",
  async (kind) => {
    let finish!: (result: unknown) => void;
    mocks.wait.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 改写" }));
    await waitFor(() => expect(mocks.wait).toHaveBeenCalled());
    if (kind === "edit")
      fireEvent.change(screen.getByLabelText("口播文案"), {
        target: { value: "手工新稿" },
      });
    else fireEvent.click(screen.getByRole("button", { name: "换项目" }));
    await act(async () =>
      finish({
        id: "rw1",
        status: "SUCCEEDED",
        result: { rewritten_text: "迟到稿" },
      }),
    );
    expect(screen.getByLabelText("口播文案")).toHaveValue(
      kind === "edit" ? "手工新稿" : "",
    );
  },
);
it("准备内容变更后拦截生成，只有显式双素材交接可解除", () => {
  const draft = { ...createDraft(), projectId: "p", sourceAssetId: "s" };
  const prepared = patchStudioDraft(draft, { replicaSourcePrompt: "拆解" });
  expect(prepared.replicaPreparationPending).toBe(true);
  const handedOff = patchStudioDraft(prepared, {
    replicaPreparationPending: false,
    firstFrameId: "frame",
    frameConfirmed: true,
  });
  expect(handedOff.replicaPreparationPending).toBe(false);
  const edited = patchStudioDraft(handedOff, {
    script: { ...handedOff.script, text: "新文案" },
  });
  expect(edited.replicaPreparationPending).toBe(true);
  expect(
    patchStudioDraft(edited, { projectId: "other" }).replicaPreparationPending,
  ).toBeUndefined();
});
it("重进页面恢复已完成的改写且不重复提交", async () => {
  // S7：改写任务已成功但结果未回填（离开/刷新丢会话），重进后应恢复结果，
  // 且不得再发起新任务（否则重复扣费）。
  sessionStorage.setItem(
    "studio:pending-replica-rewrite:u1",
    JSON.stringify({
      resultText: "当前口播",
      requestKey: "key-1",
      startedAt: 1,
      taskId: "rw9",
    }),
  );
  mocks.task.mockResolvedValue({
    id: "rw9",
    project_id: "p1",
    identity_id: null,
    source_asset_id: "a1",
    source_text: "当前口播",
    status: "SUCCEEDED",
    result: { rewritten_text: "恢复的新口播" },
  });
  render(<Harness />);
  await waitFor(() =>
    expect(screen.getByLabelText("口播文案")).toHaveValue("恢复的新口播"),
  );
  expect(mocks.rewrite).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "撤销上次改写" }));
  expect(screen.getByLabelText("口播文案")).toHaveValue("当前口播");
});
it("重进页面恢复运行中的改写并自动回填", async () => {
  mocks.latest.mockResolvedValue({
    id: "rw10",
    project_id: "p1",
    identity_id: null,
    source_asset_id: "a1",
    source_text: "当前口播",
    status: "RUNNING",
  });
  let finish!: (result: unknown) => void;
  mocks.wait.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(<Harness />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "AI 改写中…" })).toBeDisabled(),
  );
  expect(mocks.rewrite).not.toHaveBeenCalled();
  await act(async () =>
    finish({
      id: "rw10",
      status: "SUCCEEDED",
      result: { rewritten_text: "恢复完成的稿" },
    }),
  );
  expect(screen.getByLabelText("口播文案")).toHaveValue("恢复完成的稿");
});
it("来源不匹配的历史任务不会回填", async () => {
  mocks.latest.mockResolvedValue({
    id: "rw11",
    project_id: "p1",
    identity_id: null,
    source_asset_id: "other-asset",
    source_text: "当前口播",
    status: "SUCCEEDED",
    result: { rewritten_text: "不应出现" },
  });
  render(<Harness />);
  await waitFor(() =>
    expect(mocks.latest).toHaveBeenCalledWith("p1", null, "a1"),
  );
  expect(screen.getByLabelText("口播文案")).toHaveValue("当前口播");
});
it("结果已在正文时不重复应用", async () => {
  const script = {
    ...createDraft().script,
    text: "已应用的改写稿",
    resultKind: "rewritten" as const,
    rewriteTaskId: "rw12",
  };
  sessionStorage.setItem(
    "studio:pending-replica-rewrite:u1",
    JSON.stringify({
      resultText: "原口播",
      requestKey: "key-2",
      startedAt: 1,
      taskId: "rw12",
    }),
  );
  mocks.task.mockResolvedValue({
    id: "rw12",
    project_id: "p1",
    identity_id: null,
    source_asset_id: "a1",
    source_text: "原口播",
    status: "SUCCEEDED",
    result: { rewritten_text: "已应用的改写稿" },
  });
  render(<Harness initial={{ script }} />);
  await waitFor(() => expect(mocks.task).toHaveBeenCalledWith("rw12"));
  expect(screen.getByLabelText("口播文案")).toHaveValue("已应用的改写稿");
  expect(screen.queryByRole("button", { name: "撤销上次改写" })).toBeNull();
});
it("恢复等待期间用户编辑后结果不覆盖当前文案", async () => {
  mocks.latest.mockResolvedValue({
    id: "rw13",
    project_id: "p1",
    identity_id: null,
    source_asset_id: "a1",
    source_text: "当前口播",
    status: "RUNNING",
  });
  let finish!: (result: unknown) => void;
  mocks.wait.mockImplementation(
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  render(<Harness />);
  await waitFor(() => expect(mocks.wait).toHaveBeenCalledWith("rw13"));
  fireEvent.change(screen.getByLabelText("口播文案"), {
    target: { value: "手工新稿" },
  });
  await act(async () =>
    finish({
      id: "rw13",
      status: "SUCCEEDED",
      result: { rewritten_text: "恢复稿" },
    }),
  );
  expect(screen.getByLabelText("口播文案")).toHaveValue("手工新稿");
});
it("重进后重新提交复用未完成任务的幂等键", async () => {
  // S7：script.id 重进即重赋，指纹必变；pending 里的 requestKey 与正文一致
  // 时必须复用，服务端才能把重复提交归并到同一任务而非新建（重复扣费）。
  sessionStorage.setItem(
    "studio:pending-replica-rewrite:u1",
    JSON.stringify({
      resultText: "当前口播",
      requestKey: "reused-key",
      startedAt: 1,
    }),
  );
  render(<Harness />);
  await waitFor(() => expect(mocks.latest).toHaveBeenCalled());
  fireEvent.click(screen.getByRole("button", { name: "AI 改写" }));
  await waitFor(() => expect(mocks.rewrite).toHaveBeenCalled());
  expect(mocks.rewrite).toHaveBeenCalledWith(
    "p1",
    "当前口播",
    undefined,
    "a1",
    "reused-key",
  );
});
it("可重试的失败保留幂等键供重试", async () => {
  mocks.wait.mockRejectedValue(
    Object.assign(new Error("上游超时"), { retryable: true }),
  );
  render(<Harness />);
  fireEvent.click(screen.getByRole("button", { name: "AI 改写" }));
  await waitFor(() => expect(mocks.wait).toHaveBeenCalledWith("rw1"));
  await waitFor(() => expect(screen.getByText("上游超时")).toBeInTheDocument());
  const stored = JSON.parse(
    sessionStorage.getItem("studio:pending-replica-rewrite:u1") ?? "null",
  );
  expect(stored?.taskId).toBe("rw1");
});
