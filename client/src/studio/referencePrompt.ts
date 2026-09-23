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
