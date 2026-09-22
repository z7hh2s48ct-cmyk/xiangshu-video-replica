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
  useStudio: vi.fn(),
}));
vi.mock("./context", () => ({ useStudio: mocks.useStudio }));
vi.mock("../api", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  rewriteProjectScript: mocks.rewrite,
  waitForScriptRewriteTask: mocks.wait,
  createPromptOptimization: mocks.optimize,
  capturePromptSession: () => () => true,
}));

import { ReplicaNarration } from "./ReplicaPreparation";

function Harness() {
  const [draft, setDraft] = useState({
    ...createDraft(),
    projectId: "p1",
    sourceAssetId: "a1",
    replicaSourcePrompt: "原始动作与旧台词",
    script: { ...createDraft().script, text: "当前口播" },
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
