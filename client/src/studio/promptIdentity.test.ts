import { describe, expect, it } from "vitest";
import { anchorReplicaPromptToFirstFrame } from "./promptIdentity";

const LEGACY_TEXT =
  "全片人物身份、服装和配饰以首帧为准，后续不得恢复源人物外观。";

describe("anchorReplicaPromptToFirstFrame", () => {
  it("rebinds source presenter labels to the confirmed first frame", () => {
    const prompt = [
      "For the target video, <Picture 1> is fully referenced.",
      "integrated_multimodal_description: [Shot 1]",
      "全片人物身份、服装和配饰以首帧为准，后续不得恢复源人物外观。",
      "camera_language: 从全景切入女主持人的中景口播",
      "composition: 女主持人位于中央，村民站在两侧",
      "动作：主持人拿着文件夹讲解",
      "overall_soundscape: 女主持人的清晰女性声音",
      "non_diegetic_music: N/A",
    ].join("\n");

    const anchored = anchorReplicaPromptToFirstFrame(prompt);

    expect(anchored).not.toContain("女主持人");
    expect(anchored).not.toContain("女性声音");
    expect(anchored).toContain("首帧中的主讲人位于中央");
    expect(anchored).toContain("<Picture 1> 中的主体是全片唯一主讲人身份参考");
    expect(anchored).toContain("村民等人物属于背景配角");
    expect(anchored).toContain("声线的性别呈现、年龄感须与首帧人物一致");
    expect(anchored).toContain("不得出现异性声线替换、多人同时口播或中途变声");
    expect(anchored).toContain("确认文案必须从第一个字到最后一个字全部读出");
    expect(anchored).toContain(
      "源视频只用于人物动作、镜头运动、节奏、构图和空间互动参考",
    );
    expect(anchored).toContain("字幕、标题、贴纸、水印、Logo、账号名");
    expect(anchored).not.toContain(LEGACY_TEXT);
  });

  it("does not duplicate complete server-side role and voice rules", () => {
    const prompt = [
      "integrated_multimodal_description: [Shot 1]",
      "主讲人绑定：<Picture 1> 中的主体是全片唯一主讲人身份参考。",
      "多人场景角色分层：已区分主配角。",
      "配音一致性：声线与首帧人物一致。",
      "口播完整性：确认文案全部读出。",
      "源视频排除：不得复用源内容。",
      "composition: 女主持人居中，overall_soundscape: 女性声线。",
    ].join("\n");

    const anchored = anchorReplicaPromptToFirstFrame(prompt);

    expect(anchored).not.toContain("女主持人");
    expect(anchored).not.toContain("女性声线");
    expect(anchored).toContain("composition: 首帧中的主讲人居中");
    expect(anchored.match(/主讲人绑定：/g)).toHaveLength(1);
  });

  it("upgrades an earlier identity-only anchor with role and voice rules", () => {
    const prompt = [
      "integrated_multimodal_description: [Shot 1]",
      "主讲人绑定：<Picture 1> 中的主体是全片唯一主讲人身份参考。",
    ].join("\n");

    const anchored = anchorReplicaPromptToFirstFrame(prompt);

    expect(anchored).toContain("多人场景角色分层：");
    expect(anchored).toContain("配音一致性：");
    expect(anchored).toContain("口播完整性：");
    expect(anchored).toContain("源视频排除：");
    expect(anchored.match(/主讲人绑定：/g)).toHaveLength(1);
  });

  it("keeps a silent prompt silent instead of demanding a full read-through", () => {
    const prompt = [
      "integrated_multimodal_description: [Shot 1]",
      "全片人物身份、服装和配饰以首帧为准，后续不得恢复源人物外观。",
      "无口播，不添加台词或人声旁白。",
      "non_diegetic_music: N/A",
    ].join("\n");

    const anchored = anchorReplicaPromptToFirstFrame(prompt);

    expect(anchored).toContain("多人场景角色分层：");
    expect(anchored).toContain("源视频排除：");
    expect(anchored).not.toContain("配音一致性：");
    expect(anchored).not.toContain("口播完整性：");
  });
});
