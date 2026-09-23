import { defineConfig } from "@playwright/test";

const runDir = requiredEnvironmentPath("GATE1_RUN_DIR");

export default defineConfig({
  testDir: ".",
  outputDir: `${runDir}/browser`,
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 120_000,
  expect: {
    timeout: 10_000,
  },
  preserveOutput: "always",
  reporter: [
    ["line"],
    ["json", { outputFile: `${runDir}/logs/playwright-report.json` }],
  ],
  use: {
    actionTimeout: 10_000,
    baseURL: process.env.GATE1_WEB_URL ?? "http://127.0.0.1:5173",
    channel: "chrome",
    headless: true,
    navigationTimeout: 30_000,
    viewport: { width: 1584, height: 1024 },
    acceptDownloads: true,
    trace: "on",
    video: "on",
    screenshot: "only-on-failure",
  },
});

function requiredEnvironmentPath(name) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}
