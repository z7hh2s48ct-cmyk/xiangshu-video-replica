const LEGACY_ALL_PEOPLE_ANCHOR =
  "全片人物身份、服装和配饰以首帧为准，后续不得恢复源人物外观。";

const MULTI_PERSON_ROLE_RULE =
  "多人场景角色分层：<Picture 1> 主讲人是唯一主要角色；村民等人物属于背景配角，按分镜保留人数、位置、动作和互动，面部与服装彼此不同；不得复制主讲人的脸、服装或身份，不得互换主配角。";

const VOICE_CONSISTENCY_RULE =
  "配音一致性：全部清晰口播只属于 <Picture 1> 主讲人；声线的性别呈现、年龄感须与首帧人物一致，全片保持同一说话者和同一音色。主讲人未出镜时使用同一人的画外音；背景配角只保留无明确语义的环境人声，不得朗读主讲人台词，不得出现异性声线替换、多人同时口播或中途变声。";

const NARRATION_COMPLETENESS_RULE =
  "口播完整性：确认文案必须从第一个字到最后一个字全部读出，不得漏句、改写、重复、合并或截断；在目标时长内通过统一语速和自然停顿完成全部内容。";

const SOURCE_VIDEO_EXCLUSION_RULE =
  "源视频排除：源视频只用于人物动作、镜头运动、节奏、构图和空间互动参考；不得复制、恢复、改写或近义复述源视频中的口播、对白、旁白、原说话人音色，以及字幕、标题、贴纸、水印、Logo、账号名、界面文字或其他可读文字。只有当前确认文案可以作为台词。";

const NO_NARRATION_MARKER = "无口播，不添加台词或人声旁白。";

const PRESENTER_IDENTITY_RULES = [
  "主讲人绑定：<Picture 1> 中的主体是全片唯一主讲人身份参考；所有分镜里的主持人、主讲人、讲解者和口播者均指首帧中的主讲人。",
  "首帧中的主讲人，其性别、面部、发型、身形、服装和配饰只能取自 <Picture 1>；不得恢复源视频主持人的外观、性别或音色。",
  MULTI_PERSON_ROLE_RULE,
];

/**
 * 无口播的提示词里补写配音和口播规则，会和正文的「无口播」自相矛盾，
 * 因此这两条只在确实有确认文案时下发。
 */
function firstFramePresenterRules(prompt: string): string {
  const narrationRules = prompt.includes(NO_NARRATION_MARKER)
    ? []
    : [VOICE_CONSISTENCY_RULE, NARRATION_COMPLETENESS_RULE];
  return [
    ...PRESENTER_IDENTITY_RULES,
    ...narrationRules,
    SOURCE_VIDEO_EXCLUSION_RULE,
  ].join("\n");
}

const PRESENTER_ROLE_PATTERN =
  /(?:女性|男性|女|男)?(?:主持人|主讲人|讲解员|出镜人)/g;
const GENDERED_PRESENTER_ROLE_PATTERN =
  /(?:女性|男性|女|男)(?:主持人|主讲人|讲解员|出镜人)/g;
const VOICE_GENDER_PATTERN = /(?:女性|男性|女|男)(?:声音|声线|嗓音|人声)/g;

/**
 * Compatibility guard for prompts compiled by an older cloud API.
 *
 * A replacement first frame can show a different person from the source
 * presenter. Retained source labels such as “女主持人” then contradict
 * Picture 1 and can make H3 restore the source identity. Keep the source
 * action and composition while rebinding the performer to Picture 1.
 */
export function anchorReplicaPromptToFirstFrame(prompt: string): string {
  if (!prompt.trim()) {
    return prompt;
  }

  const silent = prompt.includes(NO_NARRATION_MARKER);
  const hasPresenterAnchor = prompt.includes("主讲人绑定：<Picture 1>");
  const hasMultiPersonRule = prompt.includes("多人场景角色分层：");
  const hasVoiceRule = silent || prompt.includes("配音一致性：");
  const hasNarrationRule = silent || prompt.includes("口播完整性：");
  const hasSourceExclusionRule = prompt.includes("源视频排除：");
  const sanitizedPrompt = hasPresenterAnchor
    ? prompt
        .replace(GENDERED_PRESENTER_ROLE_PATTERN, "首帧中的主讲人")
        .replace(VOICE_GENDER_PATTERN, "与首帧主讲人一致的声线")
    : prompt;
  if (
    hasPresenterAnchor &&
    hasMultiPersonRule &&
    hasVoiceRule &&
    hasNarrationRule &&
    hasSourceExclusionRule
  )
    return sanitizedPrompt;

  const sectionStart = "integrated_multimodal_description: [Shot 1]";
  if (hasPresenterAnchor) {
    const missingRules = [
      hasMultiPersonRule ? "" : MULTI_PERSON_ROLE_RULE,
      hasVoiceRule ? "" : VOICE_CONSISTENCY_RULE,
      hasNarrationRule ? "" : NARRATION_COMPLETENESS_RULE,
      hasSourceExclusionRule ? "" : SOURCE_VIDEO_EXCLUSION_RULE,
    ].filter(Boolean);
    return sanitizedPrompt.includes(sectionStart)
      ? sanitizedPrompt.replace(
          sectionStart,
          `${sectionStart}\n${missingRules.join("\n")}`,
        )
      : `${missingRules.join("\n")}\n${sanitizedPrompt}`;
  }

  const protectedRole = "__FIRST_FRAME_PRESENTER__";
  let anchored = prompt
    .replaceAll("首帧中的主讲人", protectedRole)
    .replace(PRESENTER_ROLE_PATTERN, protectedRole)
    .replace(VOICE_GENDER_PATTERN, "与首帧主讲人一致的声线")
    .replaceAll(protectedRole, "首帧中的主讲人");

  const presenterRules = firstFramePresenterRules(prompt);
  if (anchored.includes(LEGACY_ALL_PEOPLE_ANCHOR)) {
    return anchored.replace(LEGACY_ALL_PEOPLE_ANCHOR, presenterRules);
  }

  if (anchored.includes(sectionStart)) {
    anchored = anchored.replace(
      sectionStart,
      `${sectionStart}\n${presenterRules}`,
    );
  }
  return anchored;
}
