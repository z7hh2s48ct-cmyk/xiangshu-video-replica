import { expect, test } from "@playwright/test";
import {
  enterCustomerAccount,
  openCustomerCenter,
} from "./workspace-navigation.mjs";

test("public workbench gates operations and six-character registration returns home", async ({
  page,
}) => {
  await page.goto("/");
  await expect(
    page
      .getByRole("main")
      .getByRole("button", { name: "登录 / 注册", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "用户档案", exact: true }).click();
  await expect(page.getByRole("heading", { name: "登录账号" })).toBeVisible();
  await enterCustomerAccount(page, "e2e_account_first");
});

test("personal center has one token panel, points and no device settings", async ({
  page,
}) => {
  await enterCustomerAccount(page, "e2e_account_center");
  await openCustomerCenter(page);
  await expect(
    page.getByRole("tab", { name: "Token 管理", exact: true }),
  ).toHaveCount(1);
  await expect(page.getByRole("heading", { name: /我的 Token/ })).toHaveCount(
    1,
  );
  await expect(
    page.getByRole("button", { name: "充值积分", exact: true }),
  ).toHaveCount(1);
  await expect(page.getByText("设备管理", { exact: true })).toHaveCount(0);
  await page.getByRole("tab", { name: "账号设置", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "新建 Token", exact: true }),
  ).toHaveCount(0);
});
