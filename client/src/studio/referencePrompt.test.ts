import { describe, expect, it } from "vitest";
import {
  constrainReferenceVideoPrompt,
  REFERENCE_VIDEO_DIALOGUE_RULE,
  REFERENCE_VIDEO_VISUAL_ONLY_RULE,
} from "./referencePrompt";

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
