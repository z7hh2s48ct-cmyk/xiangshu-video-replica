import { expect, test } from "@playwright/test";
import { enterCustomerAccount } from "./workspace-navigation.mjs";

/** 1x1 PNG：走图片分支，上传即完成，不触发时长探测。 */
const PNG_BYTES = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==",
  "base64",
);

/** 六段式样本：段名与顺序按 H3 Ref2VA 官方口径，客户端结构自检据此点亮。 */
const SIX_SECTION_PROMPT = [
  "subject_definitions: <Subject 1> 长发女主。",
  "summary: 一句话概述。",
  "retention_analysis: 保留镜头推进与节奏。",
  "detailed_description: [Shot 1] At 00:00.000 开场。",
  "overall_soundscape: 自然环境音。",
  "non_diegetic_music: 无。",
].join("\n");

async function openReferencePage(page, account) {
  await enterCustomerAccount(page, account);
  await page.goto("/#studio/reference");
  const prompt = page.getByLabel("提示词", { exact: true });
  await expect(prompt).toBeVisible();
  return prompt;
}

// 参考生视频与文/图生视频是两套提示词实现：六段结构由系统生成，用户只说一句需求。
// 本套件不依赖提示词优化服务（e2e 后端没有供应商凭据），只验证入口文案与结构自检
// 跟着正文形态切换，以及素材变化后的告警条把补救入口放在用户眼前。
test("一句话需求按生成口径引导，六段式才切到优化口径", async ({ page }) => {
  const prompt = await openReferencePage(
    page,
    `e2e_ref_prompt_${Date.now().toString(36)}`,
  );

  await expect(page.getByText("六段式（H3 Ref2VA）")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "AI 优化提示词" }),
  ).toContainText("生成标准提示词");
  await prompt.fill("把画面里的人物换成长发女主，镜头缓慢推进");
  await expect(page.getByText(/直接用一句话写需求即可/)).toBeVisible();
  await expect(page.getByRole("region", { name: "结构自检" })).toHaveCount(0);

  await prompt.fill(SIX_SECTION_PROMPT);
  await expect(
    page.getByRole("button", { name: "AI 优化提示词" }),
  ).toContainText("AI 优化");
  await expect(
    page.getByText(/已识别为六段式（你手写或从提示词库导入）/),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "结构自检" })).toContainText(
    "1 个镜头",
  );
});

test("参考素材变化后告警条给出手动核对与重新生成入口", async ({ page }) => {
  const prompt = await openReferencePage(
    page,
    `e2e_ref_stale_${Date.now().toString(36)}`,
  );

  await prompt.fill("保持@1的运镜与节奏，人物换成长发女主");
  await expect(page.getByRole("alert")).toHaveCount(0);

  // 上传素材即绑定变化：正文里的 @1 可能已经指错，告警条必须出现。
  await page.getByLabel("上传参考素材", { exact: true }).setInputFiles({
    name: "e2e-reference.png",
    mimeType: "image/png",
    buffer: PNG_BYTES,
  });
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("参考素材已变化，请核对提示词的素材编号。");
  await expect(
    alert.getByRole("button", { name: "重新生成标准提示词" }),
  ).toBeEnabled();

  await alert.getByRole("button", { name: "我已手动核对" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
