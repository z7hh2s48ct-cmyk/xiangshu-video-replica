import path from "node:path";
import { expect, test } from "@playwright/test";

import { observePage, requiredRunDir } from "./evidence.mjs";
import { runWorkerOnce } from "./worker.mjs";

test("@smoke starts the isolated desktop shell with clean browser evidence", async ({
  page,
}) => {
  const runDir = requiredRunDir();
  const evidence = observePage(page);

  try {
    await page.goto("/");
    // 启动即自动验证身份并直接进入工作台，无需点击“进入”。
    await expect(
      page.getByRole("navigation", { name: "主导航" }),
    ).toBeVisible();
    const serviceStatus = page.getByRole("status", {
      name: "本地服务已连接",
    });
    await expect(serviceStatus).toBeVisible();
    await expect(serviceStatus).toHaveText("");
    await runWorkerOnce({ label: "smoke-empty-queue" });
    await page.screenshot({
      path: path.join(runDir, "screenshots", "1584x1024-smoke.png"),
      fullPage: true,
    });
    expect(process.env.GATE1_FORCE_SMOKE_FAILURE).not.toBe("1");
  } finally {
    await evidence.save(runDir, "smoke");
  }

  expect(evidence.consoleErrors).toEqual([]);
  expect(evidence.consoleWarnings).toEqual([]);
  expect(evidence.networkFailures).toEqual([]);
});
