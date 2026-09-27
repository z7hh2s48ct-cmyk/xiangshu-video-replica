import { describe, expect, it } from "vitest";
import {
  constrainReferenceVideoPrompt,
  hasRef2vaStructure,
  REFERENCE_VIDEO_DIALOGUE_RULE,
  REFERENCE_VIDEO_VISUAL_ONLY_RULE,
} from "./referencePrompt";

const STRUCTURED = [
  "subject_definitions: <Subject 1> 主讲人。",
  "summary: reference generation。",
  "retention_analysis: <Subject 1>：partially_preserved。",
  "detailed_description:",
  "[Shot 1] At 00:00.000 中景：人物入画。",
  "overall_soundscape: 室内自然声。",
  "non_diegetic_music: N/A",
].join("\n");

describe("constrainReferenceVideoPrompt", () => {
  it("excludes source speech and all original visible text before sound sections", () => {
    const prompt = [
      "subject_definitions: S1 is the presenter.",
      "summary: reference generation.",
      "retention_analysis: Keep motion.",
      "detailed_description: Recreate the camera movement.",
      "overall_soundscape: Natural ambience.",
      "non_diegetic_music: None.",
    ].join("\n");

    const constrained = constrainReferenceVideoPrompt(prompt);

    expect(constrained).toContain(REFERENCE_VIDEO_VISUAL_ONLY_RULE);
    expect(constrained).toContain(REFERENCE_VIDEO_DIALOGUE_RULE);
    expect(constrained.indexOf(REFERENCE_VIDEO_VISUAL_ONLY_RULE)).toBeLessThan(
      constrained.indexOf("overall_soundscape:"),
    );
  });

  it("does not duplicate complete constraints", () => {
    const prompt = `${REFERENCE_VIDEO_VISUAL_ONLY_RULE}\n${REFERENCE_VIDEO_DIALOGUE_RULE}`;
    expect(constrainReferenceVideoPrompt(prompt)).toBe(prompt);
  });
});

// 客户端判定只用来分流按钮文案与结构自检；服务端 strict 门禁才是裁判，
// 所以两边口径必须一致，否则会出现「界面说已就绪、提交被拒」。
describe("hasRef2vaStructure", () => {
  it("accepts the six sections in order with a shot marker", () => {
    expect(hasRef2vaStructure(STRUCTURED)).toBe(true);
  });

  it.each([
    ["一句需求", "一位设计师在挑高客厅里介绍落地窗采光"],
    ["复刻稿", "integrated_multimodal_description: <Picture 1> says: 大家好"],
    ["缺段", STRUCTURED.replace(/^summary:.*\n/m, "")],
    [
      "乱序",
      `${STRUCTURED.split("\n").slice(3).join("\n")}\nsubject_definitions: 迟到的一段。`,
    ],
    ["代码围栏", `\`\`\`\n${STRUCTURED}\n\`\`\``],
    ["只有详细描述", "detailed_description:\n[Shot 1] At 00:00.000 中景。"],
    ["空文本", "   "],
  ])("%s 不当作六段式", (_case, prompt) => {
    expect(hasRef2vaStructure(prompt)).toBe(false);
  });
});
