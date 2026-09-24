import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";

// P0-05-02：浏览器原生 warning 前缀过滤（评审 m1）。
const NATIVE_BROWSER_WARNING_PATTERN =
  /^\[(Deprecation|Violation|Intervention)\]/;
const LOCAL_MEDIA_PATH_PREFIX = "/api/assets/local-objects/";

export function observePage(page) {
  const consoleErrors = [];
  const consoleWarnings = [];
  const networkFailures = [];
  const networkCancellations = [];

  page.on("console", (message) => {
    if (message.type() === "error") {
      consoleErrors.push({ kind: "console", text: message.text() });
    }
    // P0-05-02：控制台 0 error/0 warning 门禁（V1.4 清单）。
    // 过滤浏览器原生实现类消息（[Deprecation]/[Violation]/[Intervention]
    // 前缀）：它们随 Chrome 版本漂移，与应用质量无关（评审 m1）。
    if (
      message.type() === "warning" &&
      !NATIVE_BROWSER_WARNING_PATTERN.test(message.text())
    ) {
      consoleWarnings.push({ kind: "console", text: message.text() });
    }
  });
  page.on("pageerror", (error) => {
    consoleErrors.push({ kind: "pageerror", text: error.message });
  });
  page.on("requestfailed", (request) => {
    const failure = {
      kind: "requestfailed",
      method: request.method(),
      url: request.url(),
      error: request.failure()?.errorText ?? "unknown",
    };
    // 切换结果时 Chromium 会主动中止旧 <video> 的剩余字节流。每个结果
    // 仍由正向用例验证 readyState 与 MP4 下载；此处单独留痕，避免把浏览器
    // 生命周期取消误报为应用网络故障。
    if (isExpectedLocalMediaCancellation(request, failure.error)) {
      networkCancellations.push(failure);
      return;
    }
    networkFailures.push(failure);
  });
  page.on("response", (response) => {
    if (response.status() >= 400) {
      networkFailures.push({
        kind: "http",
        method: response.request().method(),
        url: response.url(),
        status: response.status(),
      });
    }
  });

  return {
    consoleErrors,
    consoleWarnings,
    networkFailures,
    networkCancellations,
    async save(runDir, name) {
      const logsDir = path.join(runDir, "logs");
      await mkdir(logsDir, { recursive: true });
      await Promise.all([
        writeJson(
          path.join(logsDir, `${name}-console-errors.json`),
          consoleErrors,
        ),
        writeJson(
          path.join(logsDir, `${name}-console-warnings.json`),
          consoleWarnings,
        ),
        writeJson(
          path.join(logsDir, `${name}-network-failures.json`),
          networkFailures,
        ),
        writeJson(
          path.join(logsDir, `${name}-network-cancellations.json`),
          networkCancellations,
        ),
      ]);
    },
  };
}

function isExpectedLocalMediaCancellation(request, errorText) {
  if (
    request.method() !== "GET" ||
    request.resourceType() !== "media" ||
    errorText !== "net::ERR_ABORTED"
  ) {
    return false;
  }
  try {
    return new URL(request.url()).pathname.startsWith(LOCAL_MEDIA_PATH_PREFIX);
  } catch {
    return false;
  }
}

export function requiredRunDir() {
  const value = process.env.GATE1_RUN_DIR?.trim();
  if (!value) {
    throw new Error("GATE1_RUN_DIR is required");
  }
  return value;
}

async function writeJson(filePath, value) {
  await writeFile(filePath, `${JSON.stringify(value, null, 2)}\n`, "utf8");
}
