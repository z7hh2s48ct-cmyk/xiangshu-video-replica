import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PromptMarkdown } from "./PromptMarkdown";

const SAMPLE_TEXT = [
  "生成一条 10 秒、768P、写实短视频，从提供的首帧自然开始。",
  "保持首帧人物身份、服装、发型、场景和光线连续。",
  "[0.0-2.5s] 近景，特写，固定；女子走向别墅，场景：庭院，转场：无。口播意图：乡下的房子。",
  "[2.5-5.0s] 全景，远景，缓推；别墅外观，场景：庭院，转场：切。口播意图：真好。",
  "口播意图：乡下的房子真好。",
  "环境音与音乐保持自然；不要增加无关人物。",
].join("\n");

describe("PromptMarkdown", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders a structured preview with timecode chips and shot lines", () => {
    render(<PromptMarkdown text={SAMPLE_TEXT} />);

    expect(screen.getByText("0.0-2.5s")).toBeInTheDocument();
    expect(screen.getByText("2.5-5.0s")).toBeInTheDocument();
    expect(
      screen.getByText(
        "近景，特写，固定；女子走向别墅，场景：庭院，转场：无。口播意图：乡下的房子。",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "生成一条 10 秒、768P、写实短视频，从提供的首帧自然开始。",
      ),
    ).toBeInTheDocument();
  });

  it("copies the raw prompt text unchanged", async () => {
    render(<PromptMarkdown text={SAMPLE_TEXT} />);

    fireEvent.click(screen.getByRole("button", { name: "复制" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(SAMPLE_TEXT));
    expect(
      await screen.findByRole("button", { name: "已复制" }),
    ).toBeInTheDocument();
  });

  it("switches to source editing and saves through onSave", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<PromptMarkdown onSave={onSave} text={SAMPLE_TEXT} />);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));

    const editor = screen.getByLabelText("提示词源码");
    expect(editor).toHaveValue(SAMPLE_TEXT);

    fireEvent.change(editor, { target: { value: "手写提示词。" } });
    expect(
      screen.getByText("另存到我的提示词，原始拆解内容保留。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "另存到我的提示词" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledWith("手写提示词。"));
    // 保存成功后退回预览态；text 由宿主刷新，这里仍是原文。
    expect(screen.queryByLabelText("提示词源码")).toBeNull();
    expect(screen.getByText("0.0-2.5s")).toBeInTheDocument();
  });

  it("rejects blank saves and stays in editing mode", async () => {
    const onSave = vi.fn();
    render(<PromptMarkdown onSave={onSave} text={SAMPLE_TEXT} />);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("提示词源码"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "另存到我的提示词" }));

    expect(await screen.findByText("提示词不能为空。")).toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByLabelText("提示词源码")).toBeInTheDocument();
  });

  it("keeps editing mode and shows the reason when saving fails", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("上游输入已变化。"));
    render(<PromptMarkdown onSave={onSave} text={SAMPLE_TEXT} />);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("提示词源码"), {
      target: { value: "新文本" },
    });
    fireEvent.click(screen.getByRole("button", { name: "另存到我的提示词" }));

    expect(await screen.findByText("上游输入已变化。")).toBeInTheDocument();
    expect(screen.getByLabelText("提示词源码")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消" })).toBeEnabled();
  });

  it("restores the raw text when editing is cancelled", () => {
    render(<PromptMarkdown onSave={vi.fn()} text={SAMPLE_TEXT} />);

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("提示词源码"), {
      target: { value: "半成品" },
    });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(
      screen.getByText(
        "生成一条 10 秒、768P、写实短视频，从提供的首帧自然开始。",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("提示词源码")).toBeNull();
  });

  it("hides the edit entry when read-only or without a save handler", () => {
    const { rerender } = render(
      <PromptMarkdown onSave={vi.fn()} readOnly text={SAMPLE_TEXT} />,
    );
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();

    rerender(<PromptMarkdown text={SAMPLE_TEXT} />);
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
    // 复制在只读/无保存回调时依然可用。
    expect(screen.getByRole("button", { name: "复制" })).toBeEnabled();
  });
});
