import { describe, expect, it } from "vitest";
import {
  buildOralInput,
  createDraft,
  createState,
  DEFAULT_MAX_REFERENCE_AUDIOS,
  DEFAULT_MAX_REFERENCE_IMAGES,
  DEFAULT_MAX_REFERENCE_VIDEOS,
  draftFromTask,
  hasCopyResult,
  patchStudioDraft,
  resolveSubmittedRatio,
  routeFromHash,
  studioHashForState,
  studioRouteFromHash,
  validateReferences,
  withImportedProject,
} from "./state";
import type { StudioAsset } from "./types";

describe("V1.4 交接合同", () => {
  it("新草稿默认使用竖屏生成比例", () => {
    expect(createDraft().ratio).toBe("9:16");
  });

  // 供应商 ratio 契约（2026-09-19 对 MiniMax-H3 真实付费核对）。
  it("文生视频自动/无效比例回落 9:16，具体比例透传", () => {
    expect(resolveSubmittedRatio("t2v", "自动")).toBe("9:16");
    expect(resolveSubmittedRatio("t2v", "adaptive")).toBe("9:16");
    expect(resolveSubmittedRatio("t2v", "")).toBe("9:16");
    expect(resolveSubmittedRatio("t2v", "16:9")).toBe("16:9");
  });
  it("图生/尾帧视频恒为 adaptive，用户选择不生效", () => {
    expect(resolveSubmittedRatio("i2v", "16:9")).toBe("adaptive");
    expect(resolveSubmittedRatio("i2v", "自动")).toBe("adaptive");
    expect(resolveSubmittedRatio("l2v", "9:16")).toBe("adaptive");
  });
  it("参考生视频尊重用户选择，无效/自动时为 adaptive", () => {
    expect(resolveSubmittedRatio("r2v", "16:9")).toBe("16:9");
    expect(resolveSubmittedRatio("r2v", "自动")).toBe("adaptive");
    expect(resolveSubmittedRatio("r2v", "nonsense")).toBe("adaptive");
  });

  it("提取原文不冒充二创结果，历史人工稿仍可编辑", () => {
    const script = { ...createDraft().script, original: "原文", text: "原文" };
    expect(hasCopyResult(script)).toBe(false);
    expect(hasCopyResult({ ...script, text: "人工改写" })).toBe(true);
    expect(hasCopyResult({ ...script, confirmed: true })).toBe(true);
    expect(
      hasCopyResult({ ...script, text: "", resultKind: "rewritten" }),
    ).toBe(true);
  });
  it("选择另一条来源时清空旧项目、资产和终稿，保留已选人物", () => {
    const draft = {
      ...createDraft(),
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
      script: { ...createDraft().script, text: "A 的终稿", confirmed: true },
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
    ] as const)
      expect(next[key]).toBeUndefined();
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

  it("项目恢复不能以不同脚本 ID 或更高版本覆盖人工编辑，包括主动清空", () => {
    for (const text of ["人工尚未保存的稿件", ""]) {
      const state = createState("copy");
      state.draft.projectId = "project-a";
      state.draft.script.text = text;
      state.draft.scriptEdited = true;
      const imported = createDraft();
      imported.projectId = "project-a";
      imported.script.text = "服务器保存的另一个版本";
      imported.script.version = 99;

      expect(withImportedProject(state, imported).draft.script).toEqual(
        state.draft.script,
      );
    }
  });

  it("真实口播任务以实际提交正文创建独立的待确认稿", () => {
    const task = {
      id: "oral-1",
      backendKind: "oral_task" as const,
      title: "已完成的口播",
      type: "数字人口播" as const,
      status: "completed" as const,
      submitted: "2026-09-07",
      driverMode: "text" as const,
      scriptText: "该口播实际提交的完整正文",
      ipId: "person-a",
      avatarId: "avatar-a",
      voiceId: "voice-a",
    };

    const draft = draftFromTask(task);
    const another = draftFromTask(task);

    expect(draft.script).toMatchObject({
      title: task.title,
      text: task.scriptText,
      ipId: "person-a",
      confirmed: false,
    });
    expect(draft.ipId).toBe("person-a");
    expect(draft.voiceId).toBe("voice-a");
    expect(draft.id).not.toBe(another.id);
    expect(draft.script.id).not.toBe(another.script.id);
  });

  it("任务详情地址编码对象类型、后端 ID 与返回位置", () => {
    const route = studioRouteFromHash(
      "#studio/task-detail/oral_task/oral%2F42?returnTo=analytics",
    );
    expect(route).toMatchObject({
      page: "task-detail",
      selectedTaskKind: "oral_task",
      selectedTaskBackendId: "oral/42",
      selectedTaskId: "oral-oral/42",
      returnTo: "analytics",
    });
    expect(studioHashForState({ ...createState(), ...route })).toBe(
      "#studio/task-detail/oral_task/oral%2F42?returnTo=analytics",
    );
  });

  it("详情选择可恢复且未知路由与旧别名安全回退", () => {
    expect(
      studioRouteFromHash(
        "#studio/person-voices?person=person-7&returnTo=oral",
      ),
    ).toMatchObject({
      page: "person-voices",
      selectedPersonId: "person-7",
      returnTo: "oral",
    });
    expect(routeFromHash("#projects")).toBe("replica");
    expect(routeFromHash("#studio/not-a-page?token=forbidden")).toBe(
      "workbench",
    );
  });
  it("记录 Prompt 与文案的本地编辑状态，包括主动清空", () => {
    const draft = createDraft();
    const withPrompt = patchStudioDraft(draft, { prompt: "待编辑" });
    const clearedPrompt = patchStudioDraft(withPrompt, { prompt: "" });
    const changedScript = patchStudioDraft(clearedPrompt, {
      script: { ...clearedPrompt.script, title: "本地标题" },
    });

    expect(clearedPrompt.promptEdited).toBe(true);
    expect(changedScript.scriptEdited).toBe(true);
  });

  it("切换项目时清空未显式交接的旧项目文本与编辑标记", () => {
    const draft = {
      ...createDraft(),
      projectId: "project-a",
      prompt: "A Prompt",
      promptEdited: true,
      script: {
        ...createDraft().script,
        title: "A 标题",
        original: "A 原文",
        text: "A 文案",
      },
      scriptEdited: true,
    };

    const next = patchStudioDraft(draft, { projectId: "project-b" });

    expect(next.prompt).toBe("");
    expect(next.promptEdited).toBe(false);
    expect(next.script).toMatchObject({ title: "", original: "", text: "" });
    expect(next.scriptEdited).toBe(false);
  });

  it("项目、人物或目标图变化时使旧首帧确认与版本失效", () => {
    const confirmed = {
      ...createDraft(),
      projectId: "project-a",
      ipId: "person-a",
      imageId: "photo-a",
      firstFrameId: "frame-a",
      firstFrameSelectionVersionId: "ffv-a",
      frameConfirmed: true,
    };

    for (const patch of [
      { projectId: "project-b" },
      { ipId: "person-b" },
      { imageId: "photo-b" },
    ]) {
      const next = patchStudioDraft(confirmed, patch);
      expect(next.firstFrameId).toBeUndefined();
      expect(next.firstFrameSelectionVersionId).toBeUndefined();
      expect(next.frameConfirmed).toBe(false);
    }
  });

  it("同项目返回保留人物和未保存终稿，不回退到旧版本", () => {
    const state = createState("copy");
    state.draft = {
      ...state.draft,
      projectId: "p1",
      ipId: "zhang",
      voiceId: "voice",
      imageId: "photo",
      script: {
        ...state.draft.script,
        id: "script",
        text: "本地终稿",
        version: 3,
      },
    };
    const imported = {
      ...createDraft(),
      projectId: "p1",
      script: { ...state.draft.script, text: "旧版本", version: 2 },
    };
    const next = withImportedProject(state, imported);
    expect(next.draft.id).toBe(state.draft.id);
    expect(next.draft.ipId).toBe("zhang");
    expect(next.draft.voiceId).toBe("voice");
    expect(next.draft.imageId).toBe("photo");
    expect(next.draft.script.text).toBe("本地终稿");
  });
  it("切换项目清除上一项目的选择和返回上下文", () => {
    const state = {
      ...createState(),
      selectedTaskId: "old-task",
      selectedAssetId: "old-audio",
      selectedPersonId: "old-ip",
      selectedVideoId: "old-video",
      returnTo: "oral" as const,
    };
    const imported = { ...createDraft(), projectId: "new-project" };
    const next = withImportedProject(state, imported);
    expect(next.draft).toEqual(imported);
    for (const key of [
      "selectedTaskId",
      "selectedAssetId",
      "selectedPersonId",
      "selectedVideoId",
      "returnTo",
    ] as const)
      expect(next[key]).toBeUndefined();
  });
  it("历史任务再创作使用独立草稿，不把成片当上游视频", () => {
    const original = createDraft();
    original.sourceId = "source-original";
    original.script = {
      ...original.script,
      text: "旧任务的脚本",
      confirmed: true,
    };
    const next = draftFromTask({
      id: "task",
      title: "任务",
      type: "数字人口播",
      status: "completed",
      submitted: "today",
      driverMode: "audio",
      audioId: "speech-original",
      resultId: "final-video",
      ipId: "person-original",
      draftSnapshot: original,
    });
    expect(next.id).not.toBe(original.id);
    expect(next.sourceId).toBe("source-original");
    expect(next.audioId).toBe("speech-original");
    expect(next.voiceId).toBeUndefined();
    expect(next.script.confirmed).toBe(false);
    expect(next.script.text).toBe("旧任务的脚本");
  });
  it("首页默认工作台，旧路由有确定映射", () => {
    expect(routeFromHash("")).toBe("workbench");
    expect(routeFromHash("#projects")).toBe("replica");
    expect(routeFromHash("#studio/oral-audio")).toBe("oral");
    expect(routeFromHash("#settings")).toBe("settings");
    expect(routeFromHash("#admin")).toBe("workbench");
  });
  it("更换IP清除旧分身与声音并使报价失效", () => {
    const draft = {
      ...createDraft(),
      ipId: "a",
      imageId: "photo-a",
      avatarId: "avatar-a",
      voiceId: "voice-a",
    };
    const next = patchStudioDraft(draft, { ipId: "b" });
    expect(next.imageId).toBeUndefined();
    expect(next.avatarId).toBeUndefined();
    expect(next.voiceId).toBeUndefined();
    expect(next.quoteRevision).toBe(draft.quoteRevision + 1);
    expect(next.id).toBe(draft.id);
  });
  it("更换IP并显式交接新照片时保留新人物照片", () => {
    const draft = {
      ...createDraft(),
      ipId: "a",
      imageId: "photo-a",
      avatarId: "avatar-a",
      voiceId: "voice-a",
    };

    const next = patchStudioDraft(draft, {
      ipId: "b",
      imageId: "photo-b",
    });

    expect(next.imageId).toBe("photo-b");
    expect(next.avatarId).toBeUndefined();
    expect(next.voiceId).toBeUndefined();
  });
  it("照片带入仅修改目标，不覆盖原始画面与选中镜头", () => {
    const draft = {
      ...createDraft(),
      originalImageId: "original",
      selectedShotId: "shot-2",
    };
    const next = patchStudioDraft(draft, { imageId: "target" });
    expect(next.originalImageId).toBe("original");
    expect(next.selectedShotId).toBe("shot-2");
  });
  it("音频驱动不携带文案/TTS/声音/模板字段", () => {
    const draft = {
      ...createDraft(),
      audioId: "speech",
      avatarId: "avatar",
      voiceId: "voice",
      ipId: "ip",
    };
    expect(buildOralInput(draft, "audio")).toEqual({
      draftId: draft.id,
      mode: "audio",
      ipId: "ip",
      avatarId: "avatar",
      audioAssetId: "speech",
    });
  });
  it("文案模式需要已确认终稿和声音，不提交音频", () => {
    const draft = createDraft();
    expect(() => buildOralInput(draft, "text")).toThrow("终稿");
    const ready = {
      ...draft,
      ipId: "ip",
      avatarId: "avatar",
      voiceId: "voice",
      audioId: "irrelevant",
      script: { ...draft.script, confirmed: true, text: "我是张工" },
    };
    const input = buildOralInput(ready, "text");
    expect(input).not.toHaveProperty("audioAssetId");
    expect(input).toHaveProperty("scriptVersion", ready.script.version);
  });
});

const referenceFixture = (
  id: string,
  kind: StudioAsset["kind"],
  name = id,
): StudioAsset => ({
  id,
  name,
  kind,
  group: "参考素材",
  source: "素材库",
  saved: true,
});

describe("R2V 参考素材统一混合列表校验", () => {
  it("未归档成片与明确禁止参考的素材不能成为付费任务资产", () => {
    const result = validateReferences(
      ["direct", "forbidden", "stored"],
      [
        {
          ...referenceFixture("direct", "video"),
          delivery: "direct",
          saved: false,
        },
        {
          ...referenceFixture("forbidden", "audio"),
          allowedUses: ["voice_clone"],
        },
        { ...referenceFixture("stored", "video"), allowedUses: ["reference"] },
      ],
    );
    expect(result.invalidCount).toBe(2);
    expect(result.referenceIds).toEqual(["stored"]);
    expect(result.repairIds).toEqual(["stored"]);
  });
  it("默认每类上限为图 8 / 视频 3 / 音频 3", () => {
    expect(DEFAULT_MAX_REFERENCE_IMAGES).toBe(8);
    expect(DEFAULT_MAX_REFERENCE_VIDEOS).toBe(3);
    expect(DEFAULT_MAX_REFERENCE_AUDIOS).toBe(3);
  });

  it("按 kind 分流图片/视频/音频并保持选择顺序", () => {
    const available = [
      referenceFixture("img-1", "image"),
      referenceFixture("vid-1", "video"),
      referenceFixture("aud-1", "audio"),
      referenceFixture("img-2", "image"),
    ];
    const result = validateReferences(
      ["vid-1", "img-1", "aud-1", "img-2"],
      available,
    );
    expect(result.referenceIds).toEqual(["vid-1", "img-1", "aud-1", "img-2"]);
    expect(result.imageIds).toEqual(["img-1", "img-2"]);
    expect(result.videoIds).toEqual(["vid-1"]);
    expect(result.audioIds).toEqual(["aud-1"]);
    expect(result.imageCount).toBe(2);
    expect(result.videoCount).toBe(1);
    expect(result.audioCount).toBe(1);
    expect(result.imageLimit).toBe(DEFAULT_MAX_REFERENCE_IMAGES);
    expect(result.videoLimit).toBe(DEFAULT_MAX_REFERENCE_VIDEOS);
    expect(result.audioLimit).toBe(DEFAULT_MAX_REFERENCE_AUDIOS);
    expect(result.issues).toEqual([]);
  });

  it("去重并把重复项报告为问题", () => {
    const result = validateReferences(
      ["img-1", "img-1"],
      [referenceFixture("img-1", "image")],
    );
    expect(result.duplicateCount).toBe(1);
    expect(result.referenceIds).toEqual(["img-1"]);
    expect(result.issues.some((issue) => issue.includes("不能重复选择"))).toBe(
      true,
    );
  });

  it("无法解析的引用计入无效素材", () => {
    const result = validateReferences(
      ["img-1", "ghost"],
      [referenceFixture("img-1", "image")],
    );
    expect(result.invalidCount).toBe(1);
    expect(result.referenceIds).toEqual(["img-1"]);
    expect(result.issues.some((issue) => issue.includes("无效素材"))).toBe(
      true,
    );
  });

  it("每类分别限制上限并按选择顺序裁剪整理", () => {
    const available = [
      referenceFixture("img-1", "image"),
      referenceFixture("vid-1", "video"),
      referenceFixture("img-2", "image"),
      referenceFixture("vid-2", "video"),
      referenceFixture("aud-1", "audio"),
    ];
    const result = validateReferences(
      ["img-1", "vid-1", "img-2", "vid-2", "aud-1"],
      available,
      { maxReferenceImages: 1, maxReferenceVideos: 1, maxReferenceAudios: 1 },
    );
    expect(result.imageCount).toBe(2);
    expect(result.videoCount).toBe(2);
    expect(result.audioCount).toBe(1);
    expect(result.overLimitCount).toBe(2);
    expect(result.repairIds).toEqual(["img-1", "vid-1", "aud-1"]);
    expect(result.issues.some((issue) => issue.includes("1 张参考图"))).toBe(
      true,
    );
    expect(result.issues.some((issue) => issue.includes("1 个参考视频"))).toBe(
      true,
    );
  });

  it("视频/音频参考时长超过 15 秒计为问题并在整理时移除", () => {
    const available = [
      { ...referenceFixture("vid-ok", "video"), durationSeconds: 12 },
      { ...referenceFixture("vid-long", "video"), durationSeconds: 20 },
      { ...referenceFixture("aud-long", "audio"), durationSeconds: 30 },
      referenceFixture("aud-unknown", "audio"),
    ];
    const result = validateReferences(
      ["vid-ok", "vid-long", "aud-long", "aud-unknown"],
      available,
    );
    expect(result.overDurationCount).toBe(2);
    expect(
      result.issues.some((issue) => issue.includes("不能超过 15 秒")),
    ).toBe(true);
    // 整理时移除超时素材，保留合规与时长未知（放行）的素材
    expect(result.repairIds).toEqual(["vid-ok", "aud-unknown"]);
  });
});

describe("参考素材累计时长", () => {
  it("同类累计超过15秒会拦截且整理后可提交", () => {
    const assets = [
      { ...referenceFixture("v1", "video"), durationSeconds: 12.066667 },
      { ...referenceFixture("v2", "video"), durationSeconds: 4.458333 },
      { ...referenceFixture("a1", "audio"), durationSeconds: 15 },
    ];
    const result = validateReferences(["v1", "v2", "a1"], assets);
    expect(
      result.issues.some((issue) => issue.includes("参考视频累计时长")),
    ).toBe(true);
    expect(result.repairIds).toEqual(["v1", "a1"]);
    expect(validateReferences(result.repairIds, assets).issues).toEqual([]);
  });
  it("视频和音频分别允许累计15秒", () => {
    const assets = [
      { ...referenceFixture("v1", "video"), durationSeconds: 7 },
      { ...referenceFixture("v2", "video"), durationSeconds: 8 },
      { ...referenceFixture("a1", "audio"), durationSeconds: 15 },
    ];
    expect(validateReferences(["v1", "v2", "a1"], assets).issues).toEqual([]);
  });
});

it("混合参考素材合计最多12项", () => {
  const assets = [
    ...Array.from({ length: 8 }, (_, i) => referenceFixture(`i-${i}`, "image")),
    ...Array.from({ length: 3 }, (_, i) => ({
      ...referenceFixture(`v-${i}`, "video"),
      durationSeconds: 2,
    })),
    ...Array.from({ length: 2 }, (_, i) => ({
      ...referenceFixture(`a-${i}`, "audio"),
      durationSeconds: 2,
    })),
  ];
  const result = validateReferences(
    assets.map((a) => a.id),
    assets,
  );
  expect(result.issues).toContain("参考素材合计最多 12 项，请移除部分素材。");
  expect(result.repairIds).toHaveLength(12);
  expect(validateReferences(result.repairIds, assets).issues).toEqual([]);
});
