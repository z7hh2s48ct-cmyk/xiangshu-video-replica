import { defineConfig, devices } from "@playwright/test";

const webUrl = process.env.CUSTOMER_E2E_WEB_URL ?? "http://127.0.0.1:5173";

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.mjs",
  globalSetup: "./global-setup.mjs",
  globalTeardown: "./global-teardown.mjs",
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [["line"]],
  use: {
    baseURL: webUrl,
    headless: true,
    viewport: { width: 1584, height: 1024 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
    trace: "on",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
