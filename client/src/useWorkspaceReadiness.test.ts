import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  computeWorkspaceReadiness,
  useWorkspaceReadiness,
  type WorkspaceReadinessInput,
} from "./useWorkspaceReadiness";

function readyInput(): WorkspaceReadinessInput {
  return {
    shotCard: { versionId: "shot-v1", dirty: false, saving: false },
    script: { versionId: "script-v1", dirty: false, stale: false },
    character: { versionId: "char-v1", legacyCharacterId: null },
    sourceFrame: { selectionId: "frame-v1" },
    characterReference: { selectionId: "ref-v1" },
    firstFrame: { selectionId: "ff-v1", assetId: "asset-1" },
    prompt: {
      versionId: "prompt-v1",
      status: "LOCKED",
      stale: false,
      outputDurationSeconds: 8,
      resolution: "768P",
      quantity: 1,
      limits: { minQuantity: 1, maxQuantity: 3 },
      lockedSnapshot: {
        outputDurationSeconds: 8,
        resolution: "768P",
        quantity: 1,
      },
    },
  };
}

function missingKeys(input: WorkspaceReadinessInput): string[] {
  return computeWorkspaceReadiness(input).missing.map((item) => item.key);
}

describe("computeWorkspaceReadiness 三段聚合", () => {
  it("全部就绪时 valid 为真且无缺失项", () => {
    const readiness = computeWorkspaceReadiness(readyInput());
    expect(readiness).toEqual({
      content: true,
      people: true,
      launch: true,
      valid: true,
      missing: [],
    });
  });

  it("只缺内容项时 people 与 launch 仍就绪", () => {
    const input = readyInput();
    input.shotCard.versionId = null;
    input.script.versionId = null;
    const readiness = computeWorkspaceReadiness(input);
    expect(readiness.content).toBe(false);
    expect(readiness.people).toBe(true);
    expect(readiness.launch).toBe(true);
    expect(readiness.valid).toBe(false);
  });
});

describe("内容就绪（标签页 content）", () => {
  it("镜头卡无版本 / 脏编辑 / 保存中都计为缺失", () => {
    const noVersion = readyInput();
    noVersion.shotCard.versionId = null;
    expect(missingKeys(noVersion)).toEqual(["shotCardVersion"]);

    const dirty = readyInput();
    dirty.shotCard.dirty = true;
    expect(missingKeys(dirty)).toEqual(["shotCardVersion"]);

    const saving = readyInput();
    saving.shotCard.saving = true;
    expect(missingKeys(saving)).toEqual(["shotCardVersion"]);
  });

  it("口播稿无版本 / 脏稿 / stale 稿都计为缺失", () => {
    const noVersion = readyInput();
    noVersion.script.versionId = null;
    expect(missingKeys(noVersion)).toEqual(["scriptVersion"]);

    const dirty = readyInput();
    dirty.script.dirty = true;
    expect(missingKeys(dirty)).toEqual(["scriptVersion"]);

    const stale = readyInput();
    stale.script.stale = true;
    expect(missingKeys(stale)).toEqual(["scriptVersion"]);
  });

  it("缺失项携带中文标签与 content 标签页", () => {
    const input = readyInput();
    input.shotCard.versionId = null;
    input.script.versionId = null;
    const { missing } = computeWorkspaceReadiness(input);
    expect(missing.map((item) => [item.key, item.label, item.tab])).toEqual([
      ["shotCardVersion", "镜头卡片尚未保存", "content"],
      ["scriptVersion", "口播稿尚未保存", "content"],
    ]);
  });
});

describe("人物就绪（标签页 people）", () => {
  it("无角色版本且无 legacy 绑定时角色项缺失", () => {
    const input = readyInput();
    input.character.versionId = null;
    expect(missingKeys(input)).toEqual(["characterVersion"]);
  });

  it("legacy character_id 绑定满足角色项", () => {
    const input = readyInput();
    input.character.versionId = null;
    input.character.legacyCharacterId = "legacy-char-1";
    const readiness = computeWorkspaceReadiness(input);
    expect(readiness.people).toBe(true);
    expect(readiness.missing).toEqual([]);
  });

  it("源画面缺失计为缺失", () => {
    const input = readyInput();
    input.sourceFrame.selectionId = null;
    expect(missingKeys(input)).toEqual(["sourceFrame"]);
  });

  it("人物参考缺失计为缺失", () => {
    const input = readyInput();
    input.characterReference.selectionId = null;
    expect(missingKeys(input)).toEqual(["characterReference"]);
  });

  it("legacy character_id 路径豁免人物参考项（V1.3 任务 11/12 决议）", () => {
    const input = readyInput();
    input.character.legacyCharacterId = "legacy-char-1";
    input.characterReference.selectionId = null;
    const readiness = computeWorkspaceReadiness(input);
    expect(readiness.people).toBe(true);
    expect(readiness.missing).toEqual([]);
  });

  it("首帧选择存在但 assetId 无效时首帧项仍缺失", () => {
    const input = readyInput();
    input.firstFrame.assetId = null;
    expect(missingKeys(input)).toEqual(["firstFrame"]);

    const noSelection = readyInput();
    noSelection.firstFrame.selectionId = null;
    noSelection.firstFrame.assetId = null;
    expect(missingKeys(noSelection)).toEqual(["firstFrame"]);
  });

  it("缺失项携带中文标签与 people 标签页", () => {
    const input = readyInput();
    input.character.versionId = null;
    input.character.legacyCharacterId = null;
    input.sourceFrame.selectionId = null;
    input.characterReference.selectionId = null;
    input.firstFrame.selectionId = null;
    const { missing } = computeWorkspaceReadiness(input);
    expect(missing.map((item) => [item.key, item.label, item.tab])).toEqual([
      ["characterVersion", "未选择角色版本", "people"],
      ["sourceFrame", "未选择源画面", "people"],
      ["characterReference", "未确认人物参考", "people"],
      ["firstFrame", "未确认首帧", "people"],
    ]);
  });
});

describe("生成就绪（标签页 launch）", () => {
  it("无 Prompt 版本计为缺失", () => {
    const input = readyInput();
    input.prompt.versionId = null;
    expect(missingKeys(input)).toEqual(["promptLocked"]);
  });

  it("Prompt 未锁定（SAVED）计为缺失", () => {
    const input = readyInput();
    input.prompt.status = "SAVED";
    expect(missingKeys(input)).toEqual(["promptLocked"]);
  });

  it("Prompt stale 计为缺失", () => {
    const input = readyInput();
    input.prompt.stale = true;
    expect(missingKeys(input)).toEqual(["promptLocked"]);
  });

  it("时长越界（3 秒 / 16 秒 / 非整数）计为缺失", () => {
    const tooShort = readyInput();
    tooShort.prompt.outputDurationSeconds = 3;
    expect(missingKeys(tooShort)).toEqual(["promptLocked"]);

    const tooLong = readyInput();
    tooLong.prompt.outputDurationSeconds = 16;
    expect(missingKeys(tooLong)).toEqual(["promptLocked"]);

    const fractional = readyInput();
    fractional.prompt.outputDurationSeconds = 8.5;
    expect(missingKeys(fractional)).toEqual(["promptLocked"]);
  });

  it("分辨率非法计为缺失", () => {
    const input = readyInput();
    // 运行时脏数据防御：类型层之外仍需拒绝非法分辨率。
    input.prompt.resolution =
      "4K" as WorkspaceReadinessInput["prompt"]["resolution"];
    expect(missingKeys(input)).toEqual(["promptLocked"]);
  });

  it("数量越界（低于下限 / 超过上限）计为缺失", () => {
    const below = readyInput();
    below.prompt.quantity = 0;
    expect(missingKeys(below)).toEqual(["promptLocked"]);

    const above = readyInput();
    above.prompt.quantity = 4;
    expect(missingKeys(above)).toEqual(["promptLocked"]);
  });

  it("参数与锁定快照不一致计为缺失", () => {
    const mismatchedDuration = readyInput();
    mismatchedDuration.prompt.outputDurationSeconds = 10;
    expect(missingKeys(mismatchedDuration)).toEqual(["promptLocked"]);

    const mismatchedResolution = readyInput();
    mismatchedResolution.prompt.resolution = "2K";
    expect(missingKeys(mismatchedResolution)).toEqual(["promptLocked"]);

    const mismatchedQuantity = readyInput();
    mismatchedQuantity.prompt.quantity = 2;
    expect(missingKeys(mismatchedQuantity)).toEqual(["promptLocked"]);
  });

  it("锁定快照中的空字段不参与匹配（与服务端空快照豁免一致）", () => {
    const input = readyInput();
    input.prompt.lockedSnapshot = {
      outputDurationSeconds: null,
      resolution: null,
      quantity: null,
    };
    const readiness = computeWorkspaceReadiness(input);
    expect(readiness.launch).toBe(true);
  });

  it("缺失项携带中文标签与 launch 标签页", () => {
    const input = readyInput();
    input.prompt.status = "SAVED";
    const { missing } = computeWorkspaceReadiness(input);
    expect(missing.map((item) => [item.key, item.label, item.tab])).toEqual([
      ["promptLocked", "Prompt 未锁定或参数不一致", "launch"],
    ]);
  });
});

describe("useWorkspaceReadiness Hook", () => {
  it("输入变化时重新计算就绪状态", () => {
    const { result, rerender } = renderHook(
      (input: WorkspaceReadinessInput) => useWorkspaceReadiness(input),
      { initialProps: readyInput() },
    );

    expect(result.current.valid).toBe(true);

    const next = readyInput();
    next.character.versionId = null;
    next.character.legacyCharacterId = null;
    act(() => {
      rerender(next);
    });

    expect(result.current.valid).toBe(false);
    expect(result.current.missing.map((item) => item.key)).toEqual([
      "characterVersion",
    ]);
  });
});
