import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import react from "@vitejs/plugin-react";
import { createServer } from "vite";

// 独立挂载真实任务页；接口与成片均为测试数据，不访问生产或付费服务。
let server;
let origin;
test.beforeAll(async () => {
  server = await createServer({
    root: fileURLToPath(new URL("../../client", import.meta.url)),
    configFile: false,
    envDir: false,
    appType: "custom",
    define: {
      "import.meta.env.VITE_API_BASE_URL": '"https://api.example.test"',
    },
    plugins: [react()],
    server: { host: "127.0.0.1", port: 0 },
  });
  server.middlewares.use("/__player_test", async (_req, res, next) => {
    try {
      const html = await server.transformIndexHtml(
        "/__player_test",
        `
        <html><body><div id="root"></div><script type="module">
          import React from 'react';
          import { createRoot } from 'react-dom/client';
          import { TaskRecordsPanel } from '/src/TaskRecordsPanel.tsx';
          import '/src/styles.css';
          createRoot(document.getElementById('root')).render(React.createElement(TaskRecordsPanel, {
            handoffBatch: null, onHandoffConsumed() {}, userRole: 'customer', currentUserId: 'test-user'
          }));
        </script></body></html>`,
      );
      res.setHeader("Content-Type", "text/html");
      res.end(html);
    } catch (error) {
      next(error);
    }
  });
  await server.listen();
  origin = `http://127.0.0.1:${server.httpServer.address().port}`;
});
test.afterAll(async () => {
  await server?.close();
});

test("@player direct video plays, replays, downloads and survives refresh", async ({
  page,
}, testInfo) => {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  // 浏览器现场录制短 MP4，避免提交二进制样本或下载外部素材。
  const bytes = Buffer.from(
    await page.evaluate(async () => {
      const canvas = document.createElement("canvas");
      canvas.width = 240;
      canvas.height = 426;
      const context = canvas.getContext("2d");
      const stream = canvas.captureStream(20);
      const recorder = new MediaRecorder(stream, { mimeType: "video/mp4" });
      const chunks = [];
      recorder.ondataavailable = (event) => chunks.push(event.data);
      const recorded = new Promise((resolve) => {
        recorder.onstop = resolve;
      });
      const draw = window.setInterval(() => {
        context.fillStyle = "#202040";
        context.fillRect(0, 0, 240, 426);
        context.fillStyle = "#e8b23f";
        context.fillRect((Date.now() / 10) % 160, 180, 60, 60);
      }, 50);
      recorder.start();
      await new Promise((resolve) => window.setTimeout(resolve, 1_100));
      recorder.stop();
      await recorded;
      window.clearInterval(draw);
      for (const track of stream.getTracks()) track.stop();
      return Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));
    }),
  );
  expect(bytes.subarray(4, 8).toString()).toBe("ftyp");
  const task = {
    id: "test-task",
    status: "SUCCEEDED",
    stage: "QUALITY_FAILED",
    archive_status: "DIRECT",
    result_asset_id: null,
    direct_result_available: true,
    quality_status: "VISUAL_QUALITY_FAILED",
    quality_issue_codes: ["VIDEO_SEVERE_FLICKER"],
    available_actions: [],
    provider: "fake_h3",
    attempt: 1,
    archive_retry_count: 0,
    completed_at: "2026-09-03 10:00:00",
    prompt_snapshot: null,
  };
  const progress = {
    total_count: 1,
    terminal_count: 1,
    progress_percent: 100,
    counts: {
      pending: 0,
      submitting: 0,
      queued: 0,
      running: 0,
      archiving: 0,
      succeeded: 1,
      failed: 0,
      cancelled: 0,
      needs_attention: 1,
    },
  };
  const detail = {
    id: "test-batch",
    status: "NEEDS_ATTENTION",
    quantity: 1,
    project_id: "test-project",
    progress,
    tasks: [task],
  };
  let previewRequests = 0;
  let hidden = false;
  let deleteRequests = 0;
  const unexpected = [];
  await page.route("https://api.example.test/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    const headers = {
      "Access-Control-Allow-Origin": origin,
      "Access-Control-Allow-Methods": "GET, DELETE, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
    };
    if (request.method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers });
    } else if (
      request.method() === "DELETE" &&
      pathname === "/api/generation-batches/test-batch"
    ) {
      deleteRequests += 1;
      hidden = true;
      await route.fulfill({ status: 204, headers });
    } else if (request.method() !== "GET") {
      unexpected.push(`${request.method()} ${pathname}`);
      await route.abort();
    } else if (pathname === "/api/generation-batches") {
      await route.fulfill({
        headers,
        json: {
          items: hidden
            ? []
            : [
                {
                  ...detail,
                  created_by_user_id: "test-user",
                  status: "QUEUED",
                  project_name: "测试视频",
                  created_at: "2026-09-03 10:00:00",
                },
              ],
          next_cursor: null,
        },
      });
    } else if (pathname === "/api/generation-batches/test-batch") {
      await route.fulfill({ headers, json: detail });
    } else if (pathname === "/api/generation-tasks/test-task/preview-url") {
      previewRequests += 1;
      await route.fulfill({
        headers,
        json: { url: "https://media.example.test/test.mp4" },
      });
    } else {
      unexpected.push(pathname);
      await route.abort();
    }
  });
  await page.route("https://media.example.test/**", async (route) => {
    expect(route.request().headers().authorization).toBeUndefined();
    await route.fulfill({
      body: bytes,
      contentType: "video/mp4",
      headers: { "Access-Control-Allow-Origin": "*" },
    });
  });
  await page.goto(`${origin}/__player_test`);
  const video = page.getByLabel("结果预览 test-task", { exact: true });
  await expect
    .poll(() => video.evaluate((element) => element.readyState))
    .toBeGreaterThanOrEqual(2);
  const play = page.getByRole("button", {
    name: "播放 结果预览 test-task",
    exact: true,
  });
  await play.scrollIntoViewIfNeeded();
  // 按住直至 :active 过渡结束，才能复现按钮偏移导致 mouseup 丢失的缺陷。
  await play.click({ delay: 250 });
  await expect
    .poll(() => video.evaluate((element) => element.currentTime))
    .toBeGreaterThan(0);
  await expect
    .poll(() => video.evaluate((element) => element.ended))
    .toBe(true);
  await play.click({ delay: 250 });
  await expect
    .poll(() => video.evaluate((element) => element.paused))
    .toBe(false);
  await page.getByRole("button", { name: "暂停", exact: true }).click();
  await expect
    .poll(() => video.evaluate((element) => element.paused))
    .toBe(true);
  await expect(page.getByText(/音频质检未通过/)).toHaveCount(0);
  await expect(page.getByText(/画面质检未通过：/)).toHaveCount(0);
  await expect(page.getByText("保存成片", { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "打开批次 test-batch" }),
  ).toContainText("已完成");

  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 MP4", exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe("test-task.mp4");
  expect(await readFile(await download.path())).toEqual(bytes);
  expect(previewRequests).toBe(2);
  await expect(
    page.getByText("已交给浏览器下载，请查看浏览器下载列表。"),
  ).toBeVisible();

  const element = await video.elementHandle();
  const time = await video.evaluate((current) => current.currentTime);
  detail.status = "SUCCEEDED";
  task.quality_status = "AUDIO_OK";
  task.quality_issue_codes = [];
  task.stage = "COMPLETED";
  progress.counts.needs_attention = 0;
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "打开批次 test-batch" }),
  ).toContainText("已完成");
  expect(
    await video.evaluate((current, previous) => current === previous, element),
  ).toBe(true);
  expect(await video.evaluate((current) => current.currentTime)).toBe(time);
  expect(previewRequests).toBe(2);
  expect(unexpected).toEqual([]);
  expect(errors).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("task-records-completed.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 900, height: 1100 });
  const fullscreen = page.getByRole("button", { name: "全屏", exact: true });
  await expect(fullscreen).toBeVisible();
  await fullscreen.scrollIntoViewIfNeeded();
  const playerBounds = await video.boundingBox();
  const controlBounds = await fullscreen.boundingBox();
  expect(controlBounds.x + controlBounds.width).toBeLessThanOrEqual(
    playerBounds.x + playerBounds.width + 1,
  );
  await page.screenshot({
    path: testInfo.outputPath("task-records-narrow.png"),
    fullPage: true,
  });
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("后台生成和费用记录保留");
    await dialog.accept();
  });
  await page.getByRole("button", { name: "打开批次 test-batch" }).hover();
  await page.getByRole("button", { name: "删除批次 测试视频" }).click();
  await expect(
    page.getByRole("button", { name: "打开批次 test-batch" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "打开批次 test-batch" }),
  ).toHaveCount(0);
  expect(deleteRequests).toBe(1);
  expect(detail.tasks).toHaveLength(1);
  expect(unexpected).toEqual([]);
  expect(errors).toEqual([]);
});
