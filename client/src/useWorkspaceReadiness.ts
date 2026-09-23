export type ReadinessKey =
  | "shotCardVersion"
  | "scriptVersion"
  | "characterVersion"
  | "sourceFrame"
  | "characterReference"
  | "firstFrame"
  | "promptLocked";

export type ReadinessTab = "content" | "people" | "launch";

export interface ReadinessMissingItem {
  key: ReadinessKey;
  label: string;
  tab: ReadinessTab;
}

export interface WorkspaceReadiness {
  content: boolean;
  people: boolean;
  launch: boolean;
  valid: boolean;
  missing: ReadonlyArray<ReadinessMissingItem>;
}

export interface WorkspaceReadinessInput {
  shotCard: { versionId: string | null; dirty: boolean; saving: boolean };
  script: { versionId: string | null; dirty: boolean; stale: boolean };
  character: { versionId: string | null; legacyCharacterId: string | null };
  sourceFrame: { selectionId: string | null };
  characterReference: { selectionId: string | null };
  firstFrame: { selectionId: string | null; assetId: string | null };
  prompt: {
    versionId: string | null;
    status: "SAVED" | "LOCKED" | "USED" | null;
    stale: boolean;
    outputDurationSeconds: number | null;
    resolution: "768P" | "2K" | null;
    quantity: number | null;
    limits: { minQuantity: number; maxQuantity: number } | null;
    lockedSnapshot: {
      outputDurationSeconds: number | null;
      resolution: string | null;
      quantity: number | null;
    } | null;
  };
}

const MISSING_LABELS: Record<
  ReadinessKey,
  { label: string; tab: ReadinessTab }
> = {
  shotCardVersion: { label: "镜头卡片尚未保存", tab: "content" },
  scriptVersion: { label: "口播稿尚未保存", tab: "content" },
  characterVersion: { label: "未选择角色版本", tab: "people" },
  sourceFrame: { label: "未选择源画面", tab: "people" },
  characterReference: { label: "未确认人物参考", tab: "people" },
  firstFrame: { label: "未确认首帧", tab: "people" },
  promptLocked: { label: "Prompt 未锁定或参数不一致", tab: "launch" },
};

const PROMPT_RESOLUTIONS = new Set(["768P", "2K"]);

function isIntegerInRange(
  value: number | null,
  min: number,
  max: number,
): boolean {
  return (
    value !== null && Number.isInteger(value) && value >= min && value <= max
  );
}

function snapshotValueMatches(
  snapshotValue: number | string | null,
  currentValue: number | string | null,
): boolean {
  // 与服务端 PROMPT_PARAMETERS_MISMATCH 校验一致：快照中的空字段不参与匹配。
  return snapshotValue === null || snapshotValue === currentValue;
}

function computeMissing(input: WorkspaceReadinessInput): ReadinessKey[] {
  const missing: ReadinessKey[] = [];

  // 内容就绪：镜头卡片已保存且无脏编辑，口播稿存在已保存版本。
  const shotCardReady =
    input.shotCard.versionId !== null &&
    !input.shotCard.dirty &&
    !input.shotCard.saving;
  if (!shotCardReady) {
    missing.push("shotCardVersion");
  }

  const scriptReady =
    input.script.versionId !== null &&
    !input.script.dirty &&
    !input.script.stale;
  if (!scriptReady) {
    missing.push("scriptVersion");
  }

  // 人物就绪：角色快照（含 legacy character_id 兼容路径）。
  const legacyBound = input.character.legacyCharacterId !== null;
  const characterReady = input.character.versionId !== null || legacyBound;
  if (!characterReady) {
    missing.push("characterVersion");
  }

  if (input.sourceFrame.selectionId === null) {
    missing.push("sourceFrame");
  }

  // 人物参考：legacy character_id 旧项目路径豁免（V1.3 任务 11/12 决议）。
  const referenceReady =
    input.characterReference.selectionId !== null || legacyBound;
  if (!referenceReady) {
    missing.push("characterReference");
  }

  const firstFrameReady =
    input.firstFrame.selectionId !== null && input.firstFrame.assetId !== null;
  if (!firstFrameReady) {
    missing.push("firstFrame");
  }

  // 生成就绪：LOCKED Prompt + 参数合法 + 与锁定快照一致。
  const { prompt } = input;
  const durationValid = isIntegerInRange(prompt.outputDurationSeconds, 4, 15);
  const resolutionValid =
    prompt.resolution !== null && PROMPT_RESOLUTIONS.has(prompt.resolution);
  const quantityValid =
    prompt.limits !== null &&
    isIntegerInRange(
      prompt.quantity,
      prompt.limits.minQuantity,
      prompt.limits.maxQuantity,
    );
  const snapshot = prompt.lockedSnapshot;
  const snapshotMatches =
    snapshot === null ||
    (snapshotValueMatches(
      snapshot.outputDurationSeconds,
      prompt.outputDurationSeconds,
    ) &&
      snapshotValueMatches(snapshot.resolution, prompt.resolution) &&
      snapshotValueMatches(snapshot.quantity, prompt.quantity));
  const promptReady =
    prompt.versionId !== null &&
    prompt.status === "LOCKED" &&
    !prompt.stale &&
    durationValid &&
    resolutionValid &&
    quantityValid &&
    snapshotMatches;
  if (!promptReady) {
    missing.push("promptLocked");
  }

  return missing;
}

export function computeWorkspaceReadiness(
  input: WorkspaceReadinessInput,
): WorkspaceReadiness {
  const missingKeys = computeMissing(input);
  const missing = missingKeys.map((key) => ({
    key,
    label: MISSING_LABELS[key].label,
    tab: MISSING_LABELS[key].tab,
  }));
  const content =
    !missingKeys.includes("shotCardVersion") &&
    !missingKeys.includes("scriptVersion");
  const people =
    !missingKeys.includes("characterVersion") &&
    !missingKeys.includes("sourceFrame") &&
    !missingKeys.includes("characterReference") &&
    !missingKeys.includes("firstFrame");
  const launch = !missingKeys.includes("promptLocked");
  return {
    content,
    people,
    launch,
    valid: content && people && launch,
    missing,
  };
}

export function useWorkspaceReadiness(
  input: WorkspaceReadinessInput,
): WorkspaceReadiness {
  // Memo disabled: compute is O(1) and result not downstream dependent.
  return computeWorkspaceReadiness(input);
}
