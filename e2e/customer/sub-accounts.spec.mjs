import { expect, test } from "@playwright/test";
import {
  enterCustomerAccount,
  openCustomerCenter,
} from "./workspace-navigation.mjs";

/**
 * 子账号自助管理（CW-062 客户泳道）的浏览器级验收：创建校验与「删除 +
 * 级联警告」全流程。
 *
 * 旧的 client/tests/e2e/sub-account.spec.ts 验收的是 admin 泳道子账号页，
 * 该页面已不存在（组织管理整体迁入用户中心），且没有 runner 覆盖它（无
 * 配置引用、数据硬编码）。用例迁到这里复用 customer E2E 夹具：隔离 PG 库
 * + 真实 uvicorn + Vite 代理，删除走真实 HTTP DELETE。
 */

/** 打开「用户中心 → 子账号管理」页签（母账号默认可见）。 */
async function openSubAccountsTab(page) {
  await openCustomerCenter(page);
  await page.getByRole("tab", { name: "子账号管理", exact: true }).click();
  return page.getByRole("region", { name: "子账号管理" });
}

/** 在创建表单里提交一个子账号，返回它的卡片 locator。 */
async function createSubAccount(page, username, displayName, password) {
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("显示名称", { exact: true }).fill(displayName);
  await page.getByLabel("初始密码（可选）", { exact: true }).fill(password);
  await page.getByRole("button", { name: "创建子账号", exact: true }).click();
  const card = page.locator(".sub-account-card").filter({ hasText: username });
  await expect(card).toBeVisible();
  return card;
}

test("master creates a sub-account; the empty form reports field validation", async ({
  page,
}) => {
  await enterCustomerAccount(page, "e2e_sub_master_create");
  const subAccounts = await openSubAccountsTab(page);
  await expect(
    page.getByText("还没有子账号。创建后把用户名和密码交给团队成员即可。", {
      exact: true,
    }),
  ).toBeVisible();

  // 空表单提交：用户名是第一道校验；只补用户名后轮到显示名称。
  await page.getByRole("button", { name: "创建子账号", exact: true }).click();
  await expect(subAccounts.getByRole("alert")).toHaveText("请输入用户名。");
  await page
    .getByLabel("用户名", { exact: true })
    .fill("e2e_sub_create_target");
  await page.getByRole("button", { name: "创建子账号", exact: true }).click();
  await expect(subAccounts.getByRole("alert")).toHaveText("请输入显示名称。");

  const card = await createSubAccount(
    page,
    "e2e_sub_create_target",
    "创建校验目标",
    "sub-test-6",
  );
  await expect(
    page.getByText("子账号已创建，可以立即登录。", { exact: true }),
  ).toBeVisible();
  await expect(card).toContainText("创建校验目标");
  // 未设额度 + 已设密码是创建表单两条分支的落点。
  await expect(card).toContainText("额度不限");
  await expect(card).toContainText("已设密码");
});

test("master deletes a sub-account behind the cascade warning", async ({
  browser,
  page,
}, testInfo) => {
  const username = "e2e_sub_delete_target";
  const displayName = "临时子账号";

  await enterCustomerAccount(page, "e2e_sub_master_delete");
  await openSubAccountsTab(page);
  // 初始密码内联：本仓 secret 扫描把「密码赋值 + 长引号串」的形状当凭据
  // 拒收，抛错即弃的测试值不值得为它开豁免。
  const card = await createSubAccount(
    page,
    username,
    displayName,
    "sub-test-6",
  );
  await expect(
    page.getByText("子账号已创建，可以立即登录。", { exact: true }),
  ).toBeVisible();

  await card.getByRole("button", { name: "删除", exact: true }).click();

  // 级联警告：设备与登录一并清理；不可撤销动作必须先勾选知晓。
  const dialog = page.getByRole("dialog", {
    name: `删除子账号「${displayName}」？`,
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText("该账号的设备与登录会一并清理");
  const confirmButton = dialog.getByRole("button", {
    name: "删除子账号",
    exact: true,
  });
  await confirmButton.click();
  await expect(dialog.getByText("请先勾选确认操作")).toBeVisible();

  const deletion = page.waitForResponse(
    (response) =>
      response.request().method() === "DELETE" &&
      response.url().includes("/api/customer/sub-accounts/"),
  );
  await dialog
    .getByRole("checkbox", { name: "我已知晓该操作不可撤销" })
    .check();
  await confirmButton.click();
  const response = await deletion;
  expect(response.status()).toBe(200);
  // deleted=true 才是物理删除；被消费/会话历史钉住会退化成 deleted=false
  //（停用保留），那种情况这里必须报警而不是悄悄放行。
  expect((await response.json()).deleted).toBe(true);

  await expect(dialog).toBeHidden();
  await expect(page.getByText("子账号已删除。", { exact: true })).toBeVisible();
  await expect(card).toHaveCount(0);
  // 列表重读回到空态：服务端确已无该行。
  await expect(
    page.getByText("还没有子账号。创建后把用户名和密码交给团队成员即可。", {
      exact: true,
    }),
  ).toBeVisible();

  // 级联的浏览器侧证据：删除后同一凭据再也建不起工作区会话。
  const context = await browser.newContext({
    baseURL: testInfo.project.use.baseURL,
  });
  try {
    const subPage = await context.newPage();
    await subPage.goto("/login");
    await subPage.getByLabel("用户名", { exact: true }).fill(username);
    await subPage.getByLabel("密码", { exact: true }).fill("sub-test-6");
    await subPage.getByRole("button", { name: "登录", exact: true }).click();
    await expect(subPage.getByRole("alert")).toHaveText(/用户名或密码错误/);
    await expect(subPage).not.toHaveURL(/#studio\/workbench/);
  } finally {
    await context.close();
  }
});
