export const REFERENCE_VIDEO_VISUAL_ONLY_RULE =
  "Source-video exclusion: reference videos provide only body motion, camera motion, timing, framing, and spatial interaction. Do not copy, recreate, paraphrase, or retain any source-video dialogue, narration, spoken words, voice identity, subtitles, captions, titles, stickers, watermarks, logos, UI text, or other visible text.";

export const REFERENCE_VIDEO_DIALOGUE_RULE =
  "Dialogue authority: only the current user-confirmed script may be spoken. If no new script is supplied, generate no intelligible speech. Background characters must not repeat source speech. An independently bound Audio reference may be used only for its explicitly assigned purpose; never recover audio from a reference video.";

export function constrainReferenceVideoPrompt(prompt: string): string {
  const missing = [
    REFERENCE_VIDEO_VISUAL_ONLY_RULE,
    REFERENCE_VIDEO_DIALOGUE_RULE,
  ].filter((rule) => !prompt.includes(rule));
  if (!prompt.trim() || missing.length === 0) return prompt;

  const block = missing.join("\n");
  const soundscape = /^overall_soundscape\s*:/m.exec(prompt);
  if (!soundscape || soundscape.index === undefined) {
    return `${prompt.trimEnd()}\n${block}`;
  }
  return `${prompt.slice(0, soundscape.index).trimEnd()}\n${block}\n${prompt.slice(soundscape.index)}`;
}

// 与服务端 prompt_issues(strict=True) 的 Ref2VA 分支同源：六段齐全、顺序正确、
// 有镜头标记、不带代码围栏。客户端据此区分「一句需求待生成」与「已是成品」，
// 决定按钮文案与是否显示结构自检——服务端仍是唯一裁判。
const REF2VA_SECTIONS = [
  "subject_definitions",
  "summary",
  "retention_analysis",
  "detailed_description",
  "overall_soundscape",
  "non_diegetic_music",
] as const;

export function hasRef2vaStructure(prompt: string): boolean {
  if (!prompt.trim() || prompt.includes("```") || !prompt.includes("[Shot 1]"))
    return false;
  let cursor = -1;
  for (const section of REF2VA_SECTIONS) {
    const match = new RegExp(`^\\s*${section}\\s*:`, "m").exec(prompt);
    if (!match || match.index <= cursor) return false;
    cursor = match.index;
  }
  return true;
}
