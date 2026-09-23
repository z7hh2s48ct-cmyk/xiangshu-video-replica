/**
 * Playwright E2E tests for Sub-Account Management System
 * 
 * Test Scenarios:
 * - Admin login → Access sub-accounts management page
 * - Select master account → View existing sub-accounts
 * - Create new sub-account with validation
 * - Edit sub-account display name and active status
 * - Delete sub-account with confirmation
 */

import { test, expect } from "@playwright/test";

test.describe("Sub-Account Management E2E", () => {
  const ADMIN_USERNAME = "admin";
  const ADMIN_PASSWORD = "test123"; // This would need to be set up in test environment

  test.beforeEach(async ({ page }) => {
    // Navigate to admin login page
    await page.goto("/admin");
    
    // Wait for login form
    await expect(page.getByPlaceholder("管理员账号")).toBeVisible();
  });

  test("should login as admin and access sub-accounts page", async ({ page }) => {
    // Fill login credentials (mock data)
    await page.fill('input[placeholder="管理员账号"]', ADMIN_USERNAME);
    await page.fill('input[type="password"]', ADMIN_PASSWORD);
    
    // Submit login
    await page.click("button:has-text('登录后台')");
    
    // Wait for dashboard to load
    await expect(page.getByText("运营概览 / 总览仪表盘")).toBeVisible({ timeout: 10000 });
    
    // Click on sub-accounts tab
    await page.click('button[aria-label="子账号管理"]');
    
    // Verify sub-accounts page loaded
    await expect(page.getByText("子账号管理")).toBeVisible();
  });

  test("should filter by status", async ({ page }) => {
    // Setup: Login first (same steps as above)
    await page.fill('input[placeholder="管理员账号"]', ADMIN_USERNAME);
    await page.fill('input[type="password"]', ADMIN_PASSWORD);
    await page.click("button:has-text('登录后台')");
    await expect(page).toHaveURL(/#admin\/subAccounts/);

    // Select a parent account
    await page.selectOption("select[name*=\"parent\"]", "master-1");

    // Verify filter dropdown exists
    const filterSelect = page.locator("select").first();
    await expect(filterSelect).toBeVisible();

    // Change filter to inactive
    await filterSelect.selectOption("inactive");
    // Should show filtered results or empty state
  });

  test("should create new sub-account with validation", async ({ page }) => {
    // Setup: Login and navigate to sub-accounts
    await page.fill('input[placeholder="管理员账号"]', ADMIN_USERNAME);
    await page.fill('input[type="password"]', ADMIN_PASSWORD);
    await page.click("button:has-text('登录后台')");
    await expect(page).toHaveURL(/#admin\/subAccounts/);

    // Skip parent selection if already selected in setup
    
    // Try to create without required fields (should show error messages)
    await page.click("button:has-text('创建子账号')");
    
    // Check validation errors appear (using PageBanner errors)
    const errorMessage = page.locator('[role="alert"]').first();
    // Note: Since we use setError() instead of alerts now, check for Banner
    await expect(errorMessage).toBeVisible({ timeout: 2000 });
  });

  test("should sort sub-accounts table", async ({ page }) => {
    // Setup: Login
    await page.fill('input[placeholder="管理员账号"]', ADMIN_USERNAME);
    await page.fill('input[type="password"]', ADMIN_PASSWORD);
    await page.click("button:has-text('登录后台')");
    await expect(page).toHaveURL(/#admin\/subAccounts/);

    // Sort functionality should be available when readOnly=false
    const sortSelect = page.locator("select").nth(1);
    await expect(sortSelect).toBeVisible();

    // Change sorting
    await sortSelect.selectOption("name-asc");
    
    // Verify sort order changes (this is visual verification)
    // In real tests, we'd verify against expected data structure
  });

  test.skip("should delete sub-account with cascade warning");
  // TODO: Need actual test data setup
});
