import { expect } from "@playwright/test";

export async function waitForCustomerWorkspace(page) {
  const profileEntry = page.getByRole("button", {
    name: "用户档案",
    exact: true,
  });
  await expect(profileEntry).toBeVisible({ timeout: 20_000 });
  return profileEntry;
}

export async function openCustomerCenter(page) {
  const profileEntry = await waitForCustomerWorkspace(page);
  const summary = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/customer/center-summary") &&
      response.request().method() === "GET",
  );
  await profileEntry.click();
  expect((await summary).status()).toBe(200);
  await expect(
    page.getByRole("heading", { name: "用户中心", exact: true }),
  ).toBeVisible();
  // Only the first visit creates a default credential. Wait for its lifecycle
  // request before dismissing the one-time display, without reading its value.
  //
  // Token 列表在「Token 卡片化」（审计 P1#5）后由 <tbody><tr> 改成
  // <ul class="uc-token-list"><li class="uc-token">——这里等的是「默认凭据已渲染」，
  // 所以断言卡片数而不是表格行数（旧选择器在卡片化之后恒为 0，会让本文件的三条
  // 用例全部超时）。
  await expect(page.locator(".uc-tokens .uc-token-list > li")).toHaveCount(1);
  await expect(page.getByText("自动生成", { exact: true })).toBeVisible();
  const saved = page.getByRole("button", { name: "已保存，关闭", exact: true });
  if (await saved.isVisible()) await saved.click();

  // 首访还会弹「新手引导」（审计 P2#14，按账号记「看过」）。E2E 每次都注册新账号，
  // 所以它必然出现；它是个模态 <dialog>，不关掉会拦住后面所有点击（报
  // "... intercepts pointer events"）。点「跳过」——与 Esc 等价，见 OnboardingTour。
  const tour = page.getByRole("dialog", { name: "新手引导" });
  if (await tour.isVisible()) {
    await tour.getByRole("button", { name: "跳过", exact: true }).click();
    await expect(tour).toBeHidden();
  }
}

/** A throwaway account exercises the actual public password entry. */
export async function enterCustomerAccount(page, username, register = true) {
  await page.goto(register ? "/register" : "/login");
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill("test-6");
  if (register)
    await page.getByLabel("确认密码", { exact: true }).fill("test-6");
  await page
    .getByRole("button", {
      name: register ? "注册并登录" : "登录",
      exact: true,
    })
    .click();
  await waitForCustomerWorkspace(page);
  await expect(page).toHaveURL(/#studio\/workbench$/);
}
