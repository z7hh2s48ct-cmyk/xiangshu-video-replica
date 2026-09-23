import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import path from "node:path";

import { expect, test } from "@playwright/test";

import { observePage, requiredRunDir } from "./evidence.mjs";
import { restartApi, withBrokenGenerationArchive } from "./runtime.mjs";
import { runWorkerOnce } from "./worker.mjs";

test("@positive creates a character, restarts, and restores three completed videos", async ({
  browser,
  context,
  page,
}) => {
  const runDir = requiredRunDir();
  const evidenceRuns = [{ name: "positive-flow", evidence: observePage(page) }];
  let recoveryContext = null;

  try {
    await enterWorkspace(page);
    await createAndPublishCharacter(page);
    await page.screenshot({
      path: path.join(
        runDir,
        "screenshots",
        "1584x1024-published-character.png",
      ),
      fullPage: true,
    });

    await createProjectBatchViaOneClick(page);
    await expect(
      page.getByText("0 / 3 个结果已完成", { exact: true }),
    ).toBeVisible();

    for (const completedResults of [1, 2, 3]) {
      await runWorkerOnce({
        label: `positive-video-${completedResults}`,
        maxTasks: 1,
      });
      await expect(
        page.getByText(`${completedResults} / 3 个结果已完成`, {
          exact: true,
        }),
      ).toBeVisible({ timeout: 15_000 });
    }

    await previewAndDownloadResults(page, runDir);
    await page.screenshot({
      path: path.join(runDir, "screenshots", "1584x1024-positive-results.png"),
      fullPage: true,
    });

    await context.close();
    await restartApi();
    recoveryContext = await browser.newContext({
      acceptDownloads: true,
      baseURL: requiredEnvironmentPath("GATE1_WEB_URL"),
      recordVideo: { dir: path.join(runDir, "browser", "recovery-video") },
      viewport: { width: 1584, height: 1024 },
    });
    const recoveryPage = await recoveryContext.newPage();
    evidenceRuns.push({
      name: "recovery-flow",
      evidence: observePage(recoveryPage),
    });
    await verifyRestoredWorkspace(recoveryPage, runDir);
    await verifyFailureRecoveryPaths(recoveryPage, runDir);
  } finally {
    await Promise.all(
      evidenceRuns.map(({ evidence, name }) => evidence.save(runDir, name)),
    );
    await recoveryContext?.close();
  }

  for (const { evidence } of evidenceRuns) {
    expect(evidence.consoleErrors).toEqual([]);
    expect(evidence.consoleWarnings).toEqual([]);
    expect(evidence.networkFailures).toEqual([]);
  }
});

async function enterWorkspace(page) {
  await page.goto("/");
  // 启动即自动验证身份并直接进入工作台，无需点击“进入”。
  const serviceStatus = page.getByRole("status", {
    name: "本地服务已连接",
  });
  await expect(serviceStatus).toBeVisible();
  await expect(serviceStatus).toHaveText("");
}

async function createAndPublishCharacter(page) {
  const mediaDir = requiredEnvironmentPath("GATE1_MEDIA_DIR");
  await page.getByRole("button", { name: "人物库" }).click();
  await page.getByLabel("人物名称").fill("Gate 1 林夏");
  await page
    .getByLabel("授权图片")
    .setInputFiles(path.join(mediaDir, "source.png"));
  await page.getByRole("button", { name: "一键生成五视角拼合图" }).click();
  await expect(
    page.getByText(/人物“Gate 1 林夏”五视角拼合图已生成/),
  ).toBeVisible({ timeout: 30_000 });

  await page.getByRole("button", { name: "查看人物 Gate 1 林夏 大图" }).click();
  await expect(
    page.getByRole("dialog", { name: "人物预览 Gate 1 林夏" }),
  ).toBeVisible();
  await expect(page.getByAltText("Gate 1 林夏 五视角拼合图")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "关闭人物预览" }).click();
}

// P0-05-02：V1.4 单屏闭环一键动线——打开项目 → 预填确认（角色自动预选/
// 源画面特征/人物参考/首帧/口播稿均为预填后一次确认）→ 一键生成
//（当前提示词直接建批，契约红线 4）→ N=3 预览下载。
// 角色版本自动预选已合入（P0-03-01）：无快照进入自动落库最近发布
// 版本，零点击；fake 分析 original_script 为空，原稿预填为空稿，
// 仍需切自定义稿保存。用户确认类动作 = 4（源画面/参考/首帧对/生成）。
async function createProjectBatchViaOneClick(page) {
  const mediaDir = requiredEnvironmentPath("GATE1_MEDIA_DIR");
  // 当前项目页以视频文件名自动建项目并立即拆解，不再要求先填写项目名。
  await page.getByRole("button", { name: "项目", exact: true }).click();
  await expect(page.getByRole("region", { name: "项目" })).toBeVisible();
  await page.getByLabel("选择一个或多个参考视频").setInputFiles({
    name: "Gate 1 夏日咖啡馆口播.mp4",
    mimeType: "video/mp4",
    buffer: await readFile(path.join(mediaDir, "reference.mp4")),
  });
  await expect(page.getByText("已提交拆解")).toBeVisible({
    timeout: 30_000,
  });
  await page
    .getByRole("button", { name: "打开项目 Gate 1 夏日咖啡馆口播" })
    .click();
  // 三标签页改版后工作台主标题为 h2「复刻工作台」（原「镜头卡片」
  // heading 已随内容配置标签页结构移除）。
  await expect(page.getByRole("heading", { name: "复刻工作台" })).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByText(/FakeGemini 演示拆解/)).toBeVisible();

  // 角色自动预选会立即触发源画面自动提取；这段上游初始化期间工作区
  // 会进入 aria-busy=true 并禁用表单。先等候候选提取完成，避免 fill()
  // 的 actionability 检查与 busy 翻转落在同一帧，造成输入未落入 React 状态。
  await expect(
    page.getByText("已自动提取候选源画面，请核对后确认。"),
  ).toBeAttached({ timeout: 15_000 });
  const workspaceGrid = page.locator(".analysis-workspace-grid");
  await expect(workspaceGrid).toHaveAttribute("aria-busy", "false");

  // 标签页①（默认激活）：S01 原口播触发 800ms 防抖自动保存（P0-02-03），
  // 首个镜头卡版本无需手动保存按钮。
  const firstShotSpokenText = page.getByLabel("S01 原口播");
  const customSpokenText = "夏日咖啡馆的好项目，要从真实需求出发。";
  await expect(firstShotSpokenText).toBeEditable();
  await firstShotSpokenText.fill(customSpokenText);
  await expect(firstShotSpokenText).toHaveValue(customSpokenText);
  await expect(page.getByText(/已自动保存 · 版本 #/)).toBeVisible();

  await page.getByRole("radio", { name: "自定义稿" }).check();
  await page
    .getByLabel("口播稿内容")
    .fill("夏日咖啡馆的好项目，要从真实需求出发。");
  await page.getByRole("button", { name: "保存口播稿" }).click();
  await expect(page.getByText(/口播稿已保存为版本 #/)).toBeVisible();

  // 标签页②：角色版本自动预选并落库（P0-03-01）——无快照进入自动选择
  // 最近发布版本，零点击；源画面/参考/首帧均为预填后一次确认。
  await page.getByRole("tab", { name: "人物设定" }).click();

  await expect(
    page.getByText("已自动选择角色版本 Gate 1 林夏 · V1"),
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("当前角色：Gate 1 林夏")).toBeVisible();

  // 源画面：角色就绪后自动提取候选（P0-03-02，本地截帧无费用），候选与
  // 特征按当前保守默认值预填（正面半身、上半身可见），确认即可。
  await expect(
    page.getByText("已自动提取候选源画面，请核对后确认。"),
  ).toBeVisible({ timeout: 20_000 });
  await expect(page.getByAltText("候选源画面 1")).toBeVisible();
  await expect(page.getByLabel("人物朝向")).toHaveValue("FRONT");
  await expect(page.getByLabel("人物景别")).toHaveValue("HALF_BODY");
  await expect(page.getByLabel("面部可见性")).toHaveValue("VISIBLE");
  await expect(page.getByLabel("身体完整度")).toHaveValue("UPPER_BODY");
  await expect(page.getByRole("radio", { name: /候选 1/ })).toBeChecked();
  await page.getByRole("button", { name: "确认源画面" }).click();
  await expect(page.getByText("已确认候选源画面 1。")).toBeVisible();

  // 人物参考：推荐自动加载并默认勾选（P0-03-03），一键确认落库（红线 1）。
  const confirmReferences = page.getByRole("button", { name: "确认人物参考" });
  await expect(confirmReferences).toBeEnabled({ timeout: 15_000 });
  await confirmReferences.click();
  await expect(page.getByText("当前人物参考图已确认。")).toBeVisible();

  // 首帧：生成仍为显式付费触发（红线 3），生成后自动预选第一张（P0-03-04）。
  await page.getByLabel("候选数量").fill("1");
  await page.getByRole("button", { name: "重新生成候选首帧" }).click();
  await expect(page.getByAltText("首帧候选 1")).toBeVisible({
    timeout: 15_000,
  });
  const confirmFirstFrame = page.getByRole("button", {
    name: "确认用于 H3 的首帧",
  });
  await expect(confirmFirstFrame).toBeEnabled({ timeout: 30_000 });
  await expect(page.getByRole("radio", { name: /首帧候选 1/ })).toBeChecked();
  await confirmFirstFrame.click();
  await expect(
    page.getByText("当前候选首帧已确认，将作为后续 H3 提示词的唯一首帧输入。"),
  ).toBeVisible();

  // 标签页③：数量 3 → 工具栏付费提醒在确认前可见（P0-04-02）。
  await page.getByRole("tab", { name: "生成设置" }).click();
  await page.getByLabel("生成数量").fill("3");
  const toolbarWarning = page.locator(".paid-task-warning--toolbar");
  await expect(
    toolbarWarning.getByText("将创建 3 个付费生成任务"),
  ).toBeVisible();

  await page
    .getByLabel("视频生成提示词内容")
    .fill(
      "夏日咖啡馆，人物面对镜头自然讲述：夏日咖啡馆的好项目，要从真实需求出发。",
    );

  // 主按钮一键生成：直接冻结当前编辑文字并建批（红线 4），
  // 成功后自动交接任务记录页（无需粘贴 Batch ID）。
  await page.getByRole("button", { name: "开始生成" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "任务记录" }),
  ).toBeVisible();
}

async function previewAndDownloadResults(page, runDir) {
  const resultButtons = page
    .getByRole("navigation", { name: "生成结果列表" })
    .getByRole("button", { name: /^查看结果 / });
  await expect(resultButtons).toHaveCount(3);

  const downloadDir = path.join(runDir, "downloads");
  await mkdir(downloadDir, { recursive: true });
  for (let index = 0; index < 3; index += 1) {
    const resultButton = resultButtons.nth(index);
    const taskId = await resultTaskId(resultButton);
    await resultButton.click();
    await expect(resultButton).toHaveAttribute("aria-pressed", "true");

    // 客户视角结果舞台会自动签发并加载当前结果，不再提供手动“加载预览”。
    const video = page.getByLabel(`结果预览 ${taskId}`, { exact: true });
    await expect(video).toBeVisible();
    await expect
      .poll(() => video.evaluate((element) => element.readyState))
      .toBeGreaterThanOrEqual(2);
    await expect(video).toHaveAttribute("src", /\S+/);

    const downloadButton = page.getByRole("button", {
      name: "下载 MP4",
      exact: true,
    });
    await expect(downloadButton).toBeEnabled();
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      downloadButton.click(),
    ]);
    const targetPath = path.join(downloadDir, `result-${index + 1}.mp4`);
    await download.saveAs(targetPath);
    expect((await stat(targetPath)).size).toBeGreaterThan(0);
    const content = await readFile(targetPath);
    expect(content.length).toBeGreaterThan(8);
    expect(content.subarray(4, 8).toString("ascii")).toBe("ftyp");
  }
}

async function resultTaskId(resultButton) {
  const label = await resultButton.getAttribute("aria-label");
  const match = label?.match(/^查看结果 \d+：(.+)$/);
  if (!match) {
    throw new Error(`无法从结果按钮解析任务 ID：${label ?? "无 aria-label"}`);
  }
  return match[1];
}

async function verifyRestoredWorkspace(page, runDir) {
  await enterWorkspace(page);
  await expect(page.getByRole("region", { name: "项目" })).toBeVisible();
  await expect(
    page.getByRole("button", {
      name: "打开项目 Gate 1 夏日咖啡馆口播",
    }),
  ).toBeVisible();

  await page.getByRole("button", { name: "人物库" }).click();
  const characterPreview = page.getByRole("button", {
    name: "查看人物 Gate 1 林夏 大图",
  });
  await expect(characterPreview).toBeVisible();
  await characterPreview.click();
  await expect(
    page.getByRole("dialog", { name: "人物预览 Gate 1 林夏" }),
  ).toBeVisible();
  await expect(page.getByAltText("Gate 1 林夏 五视角拼合图")).toBeVisible({
    timeout: 15_000,
  });
  await page.getByRole("button", { name: "关闭人物预览" }).click();

  await page.getByRole("button", { name: "任务记录" }).click();
  await expect(
    page.getByText("3 / 3 个结果已完成", { exact: true }),
  ).toBeVisible();
  const resultButtons = page
    .getByRole("navigation", { name: "生成结果列表" })
    .getByRole("button", { name: /^查看结果 / });
  await expect(resultButtons).toHaveCount(3);
  const firstTaskId = await resultTaskId(resultButtons.first());
  await expect(resultButtons.first()).toHaveAttribute("aria-pressed", "true");
  const video = page.getByLabel(`结果预览 ${firstTaskId}`, { exact: true });
  await expect
    .poll(() => video.evaluate((element) => element.readyState))
    .toBeGreaterThanOrEqual(2);
  await expect(video).toHaveAttribute("src", /\S+/);

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByRole("button", { name: "下载 MP4", exact: true }).click(),
  ]);
  const targetPath = path.join(runDir, "downloads", "recovered-result.mp4");
  await download.saveAs(targetPath);
  const content = await readFile(targetPath);
  expect(content.subarray(4, 8).toString("ascii")).toBe("ftyp");
  await page.screenshot({
    path: path.join(runDir, "screenshots", "1584x1024-recovered-results.png"),
    fullPage: true,
  });
}

async function verifyFailureRecoveryPaths(page, runDir) {
  await page.getByRole("button", { name: "运维详情" }).click();
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "100");
  await page.getByText("整批再次生成（付费）", { exact: true }).click();
  await expect(page.getByLabel("整批重生成原因")).toBeVisible();
  await page
    .getByLabel("整批重生成原因")
    .fill("Gate 1 验证异常分类与不重复付费边界");
  await page.getByLabel("确认新建 3 个付费任务").check();
  await page.getByRole("button", { name: "整批付费再次生成" }).click();
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "0");

  await runWorkerOnce({
    label: "failure-submission-uncertain",
    fakeH3Outcome: "submission_uncertain",
  });
  await runWorkerOnce({
    label: "failure-provider-terminal",
    fakeH3Outcome: "provider_failed",
  });
  await withBrokenGenerationArchive(() =>
    runWorkerOnce({ label: "failure-archive", fakeH3Outcome: "ok" }),
  );
  await refreshActiveBatch(page);

  const failedCard = taskCardForStage(page, "失败");
  const archiveCard = taskCardForStage(page, "归档失败");
  const uncertainCard = taskCardForStage(page, "提交结果待确认");
  await expect(failedCard).toHaveCount(1);
  await expect(archiveCard).toHaveCount(1);
  await expect(uncertainCard).toHaveCount(1);
  await expect(
    page
      .getByRole("region", { name: "需要关注" })
      .getByText("需要处理 2", { exact: true }),
  ).toBeVisible();

  const failedTaskId = await taskIdFromCard(failedCard);
  const archiveTaskId = await taskIdFromCard(archiveCard);
  const uncertainTaskId = await taskIdFromCard(uncertainCard);
  const archiveProviderTail = await taskFactValue(archiveCard, "Provider 尾号");
  expect(archiveProviderTail).toMatch(/^\S+$/);
  expect(archiveProviderTail).not.toBe("未公开");

  // 运维动作按风险默认折叠：先显式展开，再验证付费与安全重试边界。
  await openDetailsWithin(failedCard, "details.task-paid-regeneration");
  await openDetailsWithin(archiveCard, "details.task-resolution-controls");
  await openDetailsWithin(uncertainCard, "details.task-resolution-controls");

  await expect(
    failedCard.getByText(
      "METASO H3 task failed or returned an invalid result.",
    ),
  ).toBeVisible();
  await expect(
    failedCard.getByLabel(`重新生成原因 ${failedTaskId}`),
  ).toBeVisible();
  await expect(
    failedCard.getByLabel(`确认为任务 ${failedTaskId} 新增一次付费生成`),
  ).toBeVisible();
  await expect(
    failedCard.getByLabel(`付费重新生成 ${failedTaskId}`),
  ).toBeDisabled();

  await expect(
    archiveCard.getByText(
      "Generation result could not be archived to configured storage.",
    ),
  ).toBeVisible();
  await expect(
    archiveCard.getByLabel(`重试归档 ${archiveTaskId}`),
  ).toBeDisabled();
  await expect(
    archiveCard.getByLabel(new RegExp(`付费重新生成 ${archiveTaskId}`)),
  ).toHaveCount(0);
  expect(await taskFactValue(archiveCard, "尝试")).toBe("1 次");

  await expect(
    uncertainCard.getByText("Fake H3 submission result is unknown"),
  ).toBeVisible();
  expect(await taskFactValue(uncertainCard, "Provider 尾号")).toBe("未公开");
  await expect(
    uncertainCard.getByLabel(`确认未计费 ${uncertainTaskId}`),
  ).toBeDisabled();
  await expect(
    uncertainCard.getByLabel(new RegExp(`付费重新生成 ${uncertainTaskId}`)),
  ).toHaveCount(0);

  await archiveCard
    .getByLabel(`处理原因 ${archiveTaskId}`)
    .fill("已恢复本地归档目录，只重试已付费结果的归档");
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(`/api/generation-tasks/${archiveTaskId}/retry`) &&
        response.ok(),
    ),
    archiveCard.getByLabel(`重试归档 ${archiveTaskId}`).click(),
  ]);
  await runWorkerOnce({ label: "failure-archive-safe-retry" });
  const retriedArchiveCard = taskCardById(page, archiveTaskId);
  await expect(retriedArchiveCard.getByText("阶段：已归档")).toBeVisible({
    timeout: 15_000,
  });
  expect(await taskFactValue(retriedArchiveCard, "Provider 尾号")).toBe(
    archiveProviderTail,
  );
  expect(await taskFactValue(retriedArchiveCard, "尝试")).toBe("1 次");
  const archiveTaskAfterRetry = await readActiveBatchTask(page, archiveTaskId);
  expect(archiveTaskAfterRetry.archive_retry_count).toBe(0);

  await openDetailsWithin(uncertainCard, "details.task-resolution-controls");
  await uncertainCard
    .getByLabel(`处理原因 ${uncertainTaskId}`)
    .fill("已核对 Fake H3 未产生计费与 Provider 任务");
  await Promise.all([
    page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        response
          .url()
          .endsWith(
            `/api/generation-tasks/${uncertainTaskId}/confirm-not-charged`,
          ) &&
        response.ok(),
    ),
    uncertainCard.getByLabel(`确认未计费 ${uncertainTaskId}`).click(),
  ]);
  await runWorkerOnce({ label: "failure-uncertain-admin-requeue" });
  const recoveredUncertainCard = taskCardById(page, uncertainTaskId);
  await expect(recoveredUncertainCard.getByText("阶段：已归档")).toBeVisible({
    timeout: 15_000,
  });
  expect(await taskFactValue(recoveredUncertainCard, "尝试")).toBe("2 次");
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "100");
  await expect(
    page
      .getByRole("region", { name: "需要关注" })
      .getByText("需要处理 2", { exact: true }),
  ).toHaveCount(0);

  const output = {
    failed: { task_id: failedTaskId, required_action: "paid_regeneration" },
    archive_failed: {
      task_id: archiveTaskId,
      provider_tail_before_retry: archiveProviderTail,
      attempt_after_retry: 1,
      archive_retry_performed: true,
      failed_archive_retry_count: archiveTaskAfterRetry.archive_retry_count,
    },
    submission_uncertain: {
      task_id: uncertainTaskId,
      admin_confirmation_required: true,
      attempt_after_confirmation: 2,
    },
  };
  await writeFile(
    path.join(runDir, "logs", "failure-recovery-summary.json"),
    `${JSON.stringify(output, null, 2)}\n`,
    "utf8",
  );
  await page.screenshot({
    path: path.join(runDir, "screenshots", "1584x1024-failure-recovery.png"),
    fullPage: true,
  });
  await verifyCompactViewport(page, runDir);
}

async function verifyCompactViewport(page, runDir) {
  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(
    page.getByRole("heading", { level: 1, name: "任务记录" }),
  ).toBeVisible();
  await expect(
    page.getByRole("progressbar", { name: "批次进度" }),
  ).toHaveAttribute("aria-valuenow", "100");
  await expect(page.locator("li.task-result-card")).toHaveCount(3);

  const metrics = await page.evaluate(() => ({
    viewport: {
      height: window.innerHeight,
      width: window.innerWidth,
    },
    document: {
      clientWidth: document.documentElement.clientWidth,
      scrollWidth: document.documentElement.scrollWidth,
    },
    body: {
      clientWidth: document.body.clientWidth,
      scrollWidth: document.body.scrollWidth,
    },
  }));
  expect(metrics.viewport).toEqual({ height: 768, width: 1024 });
  expect(metrics.document.scrollWidth).toBe(metrics.document.clientWidth);
  expect(metrics.body.scrollWidth).toBe(metrics.body.clientWidth);

  await writeFile(
    path.join(runDir, "logs", "1024x768-layout.json"),
    `${JSON.stringify({ ...metrics, horizontal_overflow: false }, null, 2)}\n`,
    "utf8",
  );
  await page.screenshot({
    path: path.join(runDir, "screenshots", "1024x768-failure-recovery.png"),
    fullPage: true,
  });
}

async function refreshActiveBatch(page) {
  const activeBatch = page.locator(
    'button[aria-pressed="true"][aria-label^="打开批次 "]',
  );
  await expect(activeBatch).toHaveCount(1);
  await activeBatch.click();
}

function taskCardForStage(page, stage) {
  return page
    .locator(".task-list > li.task-result-card")
    .filter({ hasText: `阶段：${stage}` });
}

function taskCardById(page, taskId) {
  return page
    .locator(".task-list > li.task-result-card")
    .filter({ hasText: taskId });
}

async function taskIdFromCard(card) {
  const value = (await card.locator(".task-id-mono").textContent())?.trim();
  expect(value).toBeTruthy();
  return value;
}

async function openDetailsWithin(scope, selector) {
  const details = scope.locator(selector);
  await expect(details).toHaveCount(1);
  if ((await details.getAttribute("open")) === null) {
    await details.locator(":scope > summary").click();
  }
  await expect(details).toHaveAttribute("open", "");
}

async function taskFactValue(card, label) {
  const fact = card.locator(".task-facts > div").filter({ hasText: label });
  await expect(fact.getByText(label, { exact: true })).toHaveCount(1);
  const value = (await fact.locator("dd").textContent())?.trim();
  expect(value).toBeTruthy();
  return value;
}

async function readActiveBatchTask(page, taskId) {
  const activeBatch = page.locator(
    'button[aria-pressed="true"][aria-label^="打开批次 "]',
  );
  const label = await activeBatch.getAttribute("aria-label");
  const batchId = label?.slice("打开批次 ".length).trim();
  if (!batchId) {
    throw new Error("无法解析当前批次 ID");
  }
  const apiUrl = requiredEnvironmentPath("GATE1_API_URL");
  const task = await page.evaluate(
    async ({ activeBatchId, apiBaseUrl, expectedTaskId }) => {
      const response = await fetch(
        `${apiBaseUrl}/api/generation-batches/${encodeURIComponent(activeBatchId)}`,
      );
      if (!response.ok) {
        throw new Error(`读取当前批次失败（${response.status}）`);
      }
      const batch = await response.json();
      return batch.tasks.find((item) => item.id === expectedTaskId) ?? null;
    },
    { activeBatchId: batchId, apiBaseUrl: apiUrl, expectedTaskId: taskId },
  );
  if (!task) {
    throw new Error(`当前批次中不存在任务 ${taskId}`);
  }
  return task;
}

function requiredEnvironmentPath(name) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}
