import { describe, expect, it } from "vitest";
import { createDraft, patchStudioDraft } from "./state";

describe("爆款来源草稿隔离", () => {
  it("切换来源时清空旧项目素材和终稿，但保留已选人物", () => {
    const base = createDraft();
    const draft = {
      ...base,
      sourceId: "asset-a",
      sourceAssetId: "asset-a",
      projectId: "project-a",
      ipId: "person-a",
      originalImageId: "original-a",
      firstFrameId: "frame-a",
      firstFrameSelectionVersionId: "frame-version-a",
      tailFrameId: "tail-a",
      audioId: "audio-a",
      referenceIds: ["reference-a"],
      prompt: "A 的提示词",
      scriptEdited: true,
      script: { ...base.script, text: "A 的终稿", confirmed: true },
    };

    const next = patchStudioDraft(draft, { sourceId: "viral-b" });

    expect(next).toMatchObject({
      sourceId: "viral-b",
      ipId: "person-a",
      prompt: "",
      referenceIds: [],
      scriptEdited: false,
      script: { original: "", text: "", confirmed: false },
    });
    for (const key of [
      "projectId",
      "sourceAssetId",
      "originalImageId",
      "firstFrameId",
      "firstFrameSelectionVersionId",
      "tailFrameId",
      "audioId",
    ] as const) {
      expect(next[key]).toBeUndefined();
    }
    expect(next.script.id).not.toBe(draft.script.id);
  });

  it("重复选择同一来源不清空正在编辑的正文", () => {
    const draft = createDraft();
    draft.sourceId = "source-a";
    draft.script.text = "当前编辑";

    expect(patchStudioDraft(draft, { sourceId: "source-a" }).script).toEqual(
      draft.script,
    );
  });
});
