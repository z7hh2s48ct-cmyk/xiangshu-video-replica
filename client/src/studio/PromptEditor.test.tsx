import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PromptOptimizeResult } from "../api";
import {
  type FinalReplicaSnapshot,
  PromptEditor,
  ReplicaFinalPromptControls,
} from "./PromptEditor";
import { readAppliedOptimization } from "./usePromptOptimization";

const api = vi.hoisted(() => ({
  create: vi.fn(),
  get: vi.fn(),
  session: vi.fn(() => true),
  compile: vi.fn(),
  script: vi.fn(),
  latestPrompt: vi.fn(async () => ({ version: null, stale: false })),
}));
vi.mock("../api", () => ({
  createPromptOptimization: api.create,
  getPromptOptimization: api.get,
  capturePromptSession: () => api.session,
  customerVisibleErrorMessage: (_error: unknown, fallback: string) => fallback,
  getLatestGenerationPrompt: api.latestPrompt,
  getLatestProjectShotCards: vi.fn(async () => ({ id: "shots" })),
  createScriptVersion: api.script,
  compileGenerationPrompt: api.compile,
}));
function Harness({
  scope = "user:project",
  rows,
  optimizationActionLabel,
}: {
  scope?: string;
  rows?: number;
  optimizationActionLabel?: string;
}) {
  const [text, setText] = useState("原始提示词");
  return (
    <PromptEditor
      value={text}
      onChange={setText}
      scope={scope}
      context={{ route: "text_image", duration_seconds: 8 }}
      rows={rows}
      optimizationActionLabel={optimizationActionLabel}
    />
  );
}
const success: PromptOptimizeResult = {
  task_id: "task",
  status: "SUCCEEDED",
  mode: "T2VA",
  editor_revision: 0,
  context_hash: "hash",
  formatter_version: "v1",
  result: { prompt_text: "优化结果", warnings: [], validation_status: "valid" },
};

it("doubles the requested prompt editing rows", () => {
  const view = render(<Harness />);
  expect(screen.getByLabelText("提示词")).toHaveAttribute("rows", "16");

  view.rerender(<Harness rows={5} />);
  expect(screen.getByLabelText("提示词")).toHaveAttribute("rows", "10");
});

function FinalHarness({
  script = "新文案",
  frame = "frame",
  duration = 4,
  sourceDuration = 4,
  sourceFrameTimestamp = 0,
  restoreEnabled = true,
}: {
  script?: string;
  frame?: string;
  duration?: number;
  sourceDuration?: number;
  sourceFrameTimestamp?: number;
  restoreEnabled?: boolean;
}) {
  const [text, setText] = useState("");
  const [snapshot, setSnapshot] = useState<FinalReplicaSnapshot | null>(null);
  return (
    <>
      <ReplicaFinalPromptControls
        sourceDuration={sourceDuration}
        sourceFrameTimestamp={sourceFrameTimestamp}
        restoreEnabled={restoreEnabled}
        input={{
          projectId: "project",
          scriptText: script,
          firstFrameAssetId: frame,
          duration,
          resolution: "768P",
          ratio: "adaptive",
          shotCardVersionId: "shots",
        }}
        value={text}
        onChange={setText}
        snapshot={snapshot}
        onPrepared={setSnapshot}
      />
      <textarea
        aria-label="最终正文"
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
    </>
  );
}

it("keeps adopted optimization provenance scoped to the account and exact text", async () => {
  api.create.mockResolvedValue(success);
  render(<Harness scope="receipt-user:receipt-project" />);
  fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
  await waitFor(() =>
    expect(screen.getByLabelText("提示词")).toHaveValue("优化结果"),
  );
  expect(
    readAppliedOptimization("receipt-user:receipt-project", "优化结果"),
  ).toEqual({
    source: "ai",
    optimization_task_id: "task",
    context_hash: "hash",
  });
  expect(
    readAppliedOptimization("other-user:receipt-project", "优化结果"),
  ).toEqual({});
  expect(
    readAppliedOptimization("receipt-user:receipt-project", "人工修改"),
  ).toEqual({});
});

describe("最终提示词后置", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.script.mockResolvedValue({ id: "script-final" });
    api.compile.mockResolvedValue({
      id: "final",
      payload: { prompt_text: "最终稿" },
    });
    api.latestPrompt.mockResolvedValue({ version: null, stale: false });
  });
  it("审核示例不读取真实历史终稿", async () => {
    render(<FinalHarness restoreEnabled={false} />);
    await Promise.resolve();
    expect(api.latestPrompt).not.toHaveBeenCalled();
  });
  it("确认文案和首帧后才合成；改变时长使旧稿失效且保留编辑", async () => {
    const view = render(<FinalHarness frame="" />);
    fireEvent.click(screen.getByLabelText("采用这份文案"));
    expect(
      screen.getByRole("button", { name: "合成最终提示词" }),
    ).toBeDisabled();
    view.rerender(<FinalHarness />);
    fireEvent.click(screen.getByLabelText("采用这份文案"));
    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));
    await waitFor(() =>
      expect(screen.getByLabelText("最终正文")).toHaveValue("最终稿"),
    );
    expect(api.script).toHaveBeenCalledWith("project", {
      source: "custom",
      text: "新文案",
      shot_card_version_id: "shots",
    });
    fireEvent.change(screen.getByLabelText("最终正文"), {
      target: { value: "人工编辑" },
    });
    view.rerender(<FinalHarness duration={15} />);
    expect(screen.getByText(/待合成/)).toBeInTheDocument();
    expect(screen.getByLabelText("最终正文")).toHaveValue("人工编辑");
    expect(api.compile).toHaveBeenCalledOnce();
  });
  it("明确无口播会保存空脚本，不把说明文字当作台词", async () => {
    render(<FinalHarness script="" />);
    fireEvent.click(screen.getByLabelText("本视频无口播"));
    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));
    await waitFor(() => expect(api.compile).toHaveBeenCalledOnce());
    expect(api.script).toHaveBeenCalledWith("project", {
      source: "no_narration",
      text: "",
      shot_card_version_id: "shots",
    });
  });
  it("口播字数不再是门禁：15 秒下的短文案与长文案都放行", () => {
    const { unmount } = render(
      <FinalHarness duration={15} script={"短文案"} />,
    );

    const checklist = screen.getByRole("region", { name: "生成前检查" });
    expect(checklist).not.toHaveTextContent("口播字数");
    expect(checklist).not.toHaveTextContent("60–90");
    expect(
      screen.getByRole("button", { name: "合成最终提示词" }),
    ).toBeEnabled();
    unmount();

    render(<FinalHarness duration={15} script={"字".repeat(200)} />);
    expect(
      screen.getByRole("region", { name: "生成前检查" }),
    ).not.toHaveTextContent("口播字数");
    expect(
      screen.getByRole("button", { name: "合成最终提示词" }),
    ).toBeEnabled();
  });
  it("口播尚未确认时只提醒，不阻止合成最终提示词", async () => {
    render(<FinalHarness />);

    const checklist = screen.getByRole("region", { name: "生成前检查" });
    expect(checklist).toHaveTextContent("可继续 · 1 项建议");
    const compose = screen.getByRole("button", { name: "合成最终提示词" });
    expect(compose).toBeEnabled();

    fireEvent.click(compose);
    await waitFor(() => expect(api.compile).toHaveBeenCalledOnce());
  });
  it("重新合成失败时回滚原有终稿，不弄丢已就绪的稿子", async () => {
    // 回归：compose() 入口先 onPrepared(null) 再发起请求，失败后不回滚，
    // 用户手里原本「已就绪」的终稿被一次失败的重新合成抹掉，只能重来。
    api.compile
      .mockResolvedValueOnce({
        id: "final",
        payload: { prompt_text: "第一稿" },
      })
      .mockRejectedValueOnce(new Error("合成服务暂不可用"));
    render(<FinalHarness />);
    fireEvent.click(screen.getByLabelText("采用这份文案"));
    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));
    await waitFor(() =>
      expect(screen.getByLabelText("最终正文")).toHaveValue("第一稿"),
    );
    expect(screen.getByText(/已就绪/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "重新合成" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("合成服务暂不可用"),
    );

    // 失败的这次没有产出新稿：原来的终稿必须回滚成「已就绪」，
    // 按钮也要回到「重新合成」，而不是把用户打回「待合成」。
    expect(screen.getByText(/已就绪/)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "重新合成" }),
    ).toBeInTheDocument();
  });
  it("迟到合成只作为候选展示，不能覆盖等待期间的人工修改", async () => {
    let finish: ((value: unknown) => void) | undefined;
    api.compile.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    render(<FinalHarness />);
    fireEvent.click(screen.getByLabelText("采用这份文案"));
    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));
    await waitFor(() => expect(api.compile).toHaveBeenCalledOnce());
    fireEvent.change(screen.getByLabelText("最终正文"), {
      target: { value: "继续编辑" },
    });
    await act(async () => {
      finish?.({ id: "final", payload: { prompt_text: "迟到新稿" } });
    });
    expect(screen.getByLabelText("最终正文")).toHaveValue("继续编辑");
    fireEvent.click(screen.getByRole("button", { name: "采用这份最终稿" }));
    expect(screen.getByLabelText("最终正文")).toHaveValue("迟到新稿");
  });
  it("只在时长压缩时要求确认，中段帧才直接显示开场衔接", () => {
    const view = render(
      <FinalHarness sourceDuration={8} sourceFrameTimestamp={0} />,
    );
    fireEvent.click(screen.getByLabelText("采用这份文案"));
    expect(
      screen.getByLabelText("将 8.0 秒内容压缩到 4 秒"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "合成最终提示词" }),
    ).toBeDisabled();
    expect(screen.queryByLabelText("开场衔接")).toBeNull();

    view.rerender(
      <FinalHarness sourceDuration={4} sourceFrameTimestamp={1.2} />,
    );
    expect(screen.queryByLabelText(/压缩/)).toBeNull();
    expect(screen.getByLabelText("开场衔接")).toBeInTheDocument();
  });

  // 服务端把「未知时间戳」(-1) 与「中段帧」(>0.25) 一起判为必须填开场衔接。前端曾把
  // 未知当成 0（视频开头），于是该必填时输入框折叠进高级设置，用户点合成必然撞 409
  // 却看不到修正入口。草稿里的时间戳没有恢复路径，未知是常态而非边缘情况。
  it("时间戳未知时按服务端口径要求开场衔接，不折叠进高级设置", () => {
    render(<FinalHarness sourceFrameTimestamp={-1} />);
    expect(screen.getByLabelText("开场衔接")).toHaveAttribute(
      "placeholder",
      expect.stringContaining("自然衔接到原视频"),
    );
  });

  it("非开头首帧默认用持续行走的开场衔接，并提交到合成接口", async () => {
    render(<FinalHarness sourceFrameTimestamp={1.2} />);
    const opening = screen.getByLabelText("开场衔接");
    expect((opening as HTMLTextAreaElement).value).toContain("边走边开始口播");
    fireEvent.click(screen.getByLabelText("采用这份文案"));
    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));
    await waitFor(() =>
      expect(api.compile).toHaveBeenCalledWith(
        "project",
        expect.objectContaining({
          opening_action: (opening as HTMLTextAreaElement).value,
        }),
      ),
    );
  });

  it("执行前逐项显示缺失原因，并在补齐前不调用服务端", () => {
    render(<FinalHarness frame="" sourceFrameTimestamp={-1} />);
    fireEvent.change(screen.getByLabelText("开场衔接"), {
      target: { value: "" },
    });

    const checklist = screen.getByRole("region", { name: "生成前检查" });
    expect(checklist).toHaveTextContent("首帧选择");
    expect(checklist).toHaveTextContent("请先完成首帧置换并选定图片");
    expect(checklist).toHaveTextContent("口播文案");
    expect(checklist).toHaveTextContent("请在口播文案区域点击“确认”");
    expect(checklist).toHaveTextContent("开场衔接");
    expect(checklist).toHaveTextContent("首帧时间点未知");
    expect(
      screen.getByRole("button", { name: "合成最终提示词" }),
    ).toBeDisabled();
    expect(api.script).not.toHaveBeenCalled();
    expect(api.compile).not.toHaveBeenCalled();
  });

  it("服务端判定缺开场衔接时，把输入框显示出来而不是只报错", async () => {
    api.compile.mockRejectedValue(
      Object.assign(new Error("首帧不是视频开头，请在「开场衔接」里写明。"), {
        status: 409,
        code: "FIRST_FRAME_ALIGNMENT_REQUIRED",
      }),
    );
    render(<FinalHarness sourceFrameTimestamp={0} />);
    expect(screen.queryByLabelText("开场衔接")).toBeNull();

    fireEvent.click(screen.getByLabelText("采用这份文案"));
    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));

    await waitFor(() =>
      expect(screen.getByLabelText("开场衔接")).toBeInTheDocument(),
    );
    // 服务端给的具体原因要原样透出，不能被通用 409 文案盖掉。
    expect(screen.getByRole("status")).toHaveTextContent("首帧不是视频开头");
  });

  // 前端按分镜算源时长（未加载时为 0），服务端优先用拆解记录的 duration_seconds。
  // 两者分叉时压缩勾选框整个不渲染，用户既撞 409 又无处勾选。
  it("服务端判定需要压缩确认时，把勾选框显示出来", async () => {
    api.compile.mockRejectedValue(
      Object.assign(new Error("源视频长于目标时长，请勾选压缩确认。"), {
        status: 409,
        code: "TIMELINE_CONFIRMATION_REQUIRED",
      }),
    );
    render(<FinalHarness sourceDuration={0} sourceFrameTimestamp={0} />);
    fireEvent.click(screen.getByLabelText("采用这份文案"));
    expect(screen.queryByLabelText(/压缩/)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "合成最终提示词" }));

    await waitFor(() =>
      expect(screen.getByLabelText(/压缩/)).toBeInTheDocument(),
    );
  });
});

describe("PromptEditor", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    api.session.mockReturnValue(true);
  });
  it("优化回填可撤销，继续手改后不能撤销覆盖新输入", async () => {
    api.create.mockResolvedValue(success);
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
    await waitFor(() =>
      expect(screen.getByLabelText("提示词")).toHaveValue("优化结果"),
    );
    fireEvent.click(screen.getByRole("button", { name: "撤销" }));
    expect(screen.getByLabelText("提示词")).toHaveValue("原始提示词");
    fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
    await waitFor(() =>
      expect(screen.getByLabelText("提示词")).toHaveValue("优化结果"),
    );
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "继续手改" },
    });
    expect(screen.queryByRole("button", { name: "撤销" })).toBeNull();
  });
  it("转换导入模板时立即展示进度，按钮仍保留统一的可访问名称", async () => {
    api.create.mockReturnValue(new Promise(() => {}));
    render(<Harness optimizationActionLabel="按当前素材 AI 转换" />);

    const button = screen.getByRole("button", { name: "AI 优化提示词" });
    expect(button).toHaveTextContent("按当前素材 AI 转换");
    fireEvent.click(button);

    expect(await screen.findByRole("status")).toHaveTextContent(
      "正在提交优化任务",
    );
    expect(button).toHaveTextContent("正在优化");
  });
  it("迟到结果不覆盖等待期间的编辑，可主动应用", async () => {
    let finish: ((value: PromptOptimizeResult) => void) | undefined;
    api.create.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "新输入" },
    });
    await act(async () => finish?.(success));
    expect(screen.getByLabelText("提示词")).toHaveValue("新输入");
    fireEvent.click(screen.getByRole("button", { name: "应用此结果" }));
    expect(screen.getByLabelText("提示词")).toHaveValue("优化结果");
  });
  it.each(["project", "session"])("%s 变化后旧结果不回填", async (kind) => {
    let finish: ((value: PromptOptimizeResult) => void) | undefined;
    api.create.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const view = render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
    if (kind === "project") view.rerender(<Harness scope="other" />);
    else api.session.mockReturnValue(false);
    await act(async () => finish?.(success));
    expect(screen.getByLabelText("提示词")).toHaveValue("原始提示词");
    expect(screen.queryByRole("button", { name: "应用此结果" })).toBeNull();
  });
  it("失败保留原文，空白和超限时禁用按钮", async () => {
    api.create.mockRejectedValue(new Error("failed"));
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
    await screen.findByText("优化失败，原文已保留。");
    expect(screen.getByLabelText("提示词")).toHaveValue("原始提示词");
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: " " },
    });
    expect(
      screen.getByRole("button", { name: "AI 优化提示词" }),
    ).toBeDisabled();
    fireEvent.change(screen.getByLabelText("提示词"), {
      target: { value: "🎥".repeat(7000) },
    });
    expect(screen.getByRole("button", { name: "AI 优化提示词" })).toBeEnabled();
  });
  it("网络结果未知后重试沿用原请求，刷新后查询原任务", async () => {
    api.create
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(success);
    const view = render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
    await screen.findByText("优化失败，原文已保留。");
    const input = api.create.mock.calls[0][0];
    view.unmount();
    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "AI 优化提示词" }));
    await waitFor(() =>
      expect(screen.getByLabelText("提示词")).toHaveValue("优化结果"),
    );
    expect(api.create.mock.calls[1][0]).toEqual(input);
  });
});
