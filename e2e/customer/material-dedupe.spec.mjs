import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { expect, test } from "@playwright/test";
import { enterCustomerAccount } from "./workspace-navigation.mjs";

/** 1x1 PNG：走图片分支，绕开时长探测，直接落在 uploadVideoMaterial 上。 */
const PNG_BYTES = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==",
  "base64",
);

/** 2 秒 440Hz 单声道 MP3，走音频分支落在 uploadReferenceAudioMaterial 上。
 *
 *  必须真能解码：客户端在浏览器里读时长，服务端在 complete 时用 ffprobe 复核，
 *  两者相差超过 1 秒会 422 MATERIAL_AUDIO_DURATION_MISMATCH。
 *  这里现场用 ffmpeg 生成而不是把二进制塞进仓库——服务端音频链路本来就走 PATH 上的
 *  ffmpeg/ffprobe（app.media_tools.resolve_media_binary），用的是同一份工具。 */
function makeMp3Fixture() {
  const directory = mkdtempSync(
    path.join(os.tmpdir(), "material-dedupe-audio-"),
  );
  const target = path.join(directory, "reference-tone.mp3");
  execFileSync(
    "ffmpeg",
    [
      "-hide_banner",
      "-loglevel",
      "error",
      "-f",
      "lavfi",
      "-i",
      "sine=frequency=440:duration=2",
      "-ac",
      "1",
      "-ar",
      "8000",
      "-b:a",
      "8k",
      "-y",
      target,
    ],
    { stdio: "ignore" },
  );
  return readFileSync(target);
}

function trackMaterialRequests(page) {
  const requests = [];
  page.on("request", (request) => {
    const url = request.url();
    if (url.includes("/api/studio/materials/")) {
      requests.push({ method: request.method(), url });
    }
  });
  return requests;
}

function materialCalls(requests) {
  return {
    intents: requests.filter(
      (r) => r.method === "POST" && r.url.endsWith("/upload-intent"),
    ),
    content: requests.filter(
      (r) => r.method === "PUT" && r.url.endsWith("/content"),
    ),
    completes: requests.filter(
      (r) => r.method === "POST" && r.url.endsWith("/complete"),
    ),
  };
}

/** 上传同一份字节两次，断言第二次命中 sha256 去重：仍会查登记表
 *  （upload-intent + resolve），但不再 PUT 字节、也不再 /complete。 */
async function expectReusedOnSecondUpload(page, requests, file) {
  const input = page.getByLabel("上传参考素材", { exact: true });
  await expect(input).toBeEnabled();
  const toast = page.locator(".studio-toast");
  const uploaded = `参考素材「${file.name}」已上传到素材库。`;

  // 第一次：全新内容，走真实的三步上传。
  await input.setInputFiles(file);
  await expect(toast).toContainText(uploaded, { timeout: 30_000 });

  const first = materialCalls(requests);
  expect(first.intents).toHaveLength(1);
  expect(first.content).toHaveLength(1);
  expect(first.completes).toHaveLength(1);

  // 等首次提示消失，让第二次的断言不依赖残留文本。
  await expect(toast).toBeHidden({ timeout: 20_000 });

  // 第二次：字节完全相同，应当复用已登记素材而不是重新传输。
  await input.setInputFiles(file);
  await expect(toast).toContainText(uploaded, { timeout: 30_000 });
  await expect(toast).not.toContainText("素材上传失败");

  const all = materialCalls(requests);
  expect(all.intents).toHaveLength(2);
  expect(all.content).toHaveLength(1);
  expect(all.completes).toHaveLength(1);
}

async function openReferenceUpload(page, account) {
  await enterCustomerAccount(page, account);
  const requests = trackMaterialRequests(page);
  await page.goto("/#studio/reference");
  await expect(page.getByLabel("上传参考素材", { exact: true })).toBeAttached();
  return requests;
}

test("参考生视频页重复上传同一图片不再失败，且第二次不传输字节", async ({
  page,
}) => {
  const requests = await openReferenceUpload(
    page,
    `e2e_dedupe_img_${Date.now().toString(36)}`,
  );
  await expectReusedOnSecondUpload(page, requests, {
    name: "e2e-dedupe-1x1.png",
    mimeType: "image/png",
    buffer: PNG_BYTES,
  });
});

test("参考生视频页重复上传同一音频不再失败，且第二次不传输字节", async ({
  page,
}) => {
  const requests = await openReferenceUpload(
    page,
    `e2e_dedupe_audio_${Date.now().toString(36)}`,
  );
  await expectReusedOnSecondUpload(page, requests, {
    name: "e2e-dedupe-tone.mp3",
    mimeType: "audio/mpeg",
    buffer: makeMp3Fixture(),
  });
});
