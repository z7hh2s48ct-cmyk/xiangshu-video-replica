import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test } from "@playwright/test";
import {
  enterCustomerAccount,
  openCustomerCenter,
  waitForCustomerWorkspace,
} from "./workspace-navigation.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const runDir = process.env.CUSTOMER_E2E_RUN_DIR ?? path.join(__dirname, "run");

/** 读 globalSetup 落盘的运行状态（URL、种子码、E2E 库 DSN 与设备域密钥）。 */
function runState() {
  return JSON.parse(readFileSync(path.join(runDir, "run.json"), "utf8"));
}

/**
 * E2E 的「收件箱」：验证码只出现在邮件里，而邮件发不出去（假凭据），
 * 于是回到数据库侧用 read_email_code.py 从摘要反推（见该脚本 docstring）。
 * 真实链路——签发、冷却、消费、作废——一个都没被替身顶掉，被顶掉的
 * 只有 SMTP/SES 传输本身。
 */
function readEmailCode(username, purpose) {
  const state = runState();
  const result = spawnSync(
    state.python,
    [path.join(__dirname, "read_email_code.py"), username, purpose],
    {
      env: {
        ...process.env,
        CUSTOMER_E2E_DATABASE_URL: state.dsn,
        VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY: state.fingerprintKey,
      },
      encoding: "utf8",
      timeout: 60_000,
    },
  );
  if (result.error || result.status !== 0) {
    throw new Error(
      `read_email_code.py failed: ${result.error ?? (result.stderr || result.stdout)}`,
    );
  }
  const match = /EMAIL_CODE=(\d{6})/.exec(result.stdout);
  if (!match) {
    throw new Error(`read_email_code.py printed no code:\n${result.stdout}`);
  }
  return match[1];
}

/** 用户中心 → 账号设置 → 绑定邮箱卡片：发码、读码、验证，走完整 UI。 */
async function bindEmailViaUi(page, username, email) {
  await openCustomerCenter(page);
  await page.getByRole("tab", { name: "账号设置", exact: true }).click();
  const section = page.locator(".uc-email");
  await expect(section.getByText("未绑定邮箱", { exact: true })).toBeVisible();

  await section.getByRole("button", { name: "绑定邮箱", exact: true }).click();
  await section.getByLabel("邮箱地址").fill(email);
  await section
    .getByRole("button", { name: "发送验证码", exact: true })
    .click();
  await expect(section.getByText(`验证码已发往 ${email}。`)).toBeVisible();

  await section
    .getByLabel("验证码")
    .fill(readEmailCode(username, "bind_email"));
  await section.getByRole("button", { name: "确认绑定", exact: true }).click();
  await expect(
    section.getByText("邮箱已绑定。忘记密码时验证码会发到这个邮箱。"),
  ).toBeVisible();
  await expect(section.getByText(email, { exact: true })).toBeVisible();
}

test("账号设置里绑定邮箱：验证码核对通过后地址才写进账号", async ({ page }) => {
  const username = "e2e_bind_flow";
  const email = `${username}@e2e.example.com`;
  await enterCustomerAccount(page, username);
  await bindEmailViaUi(page, username, email);

  const section = page.locator(".uc-email");
  await expect(section.getByText(/已验证/)).toBeVisible();
  // 换绑预填现有地址：重绑一次不需要把地址再打一遍。
  await section.getByRole("button", { name: "更换邮箱", exact: true }).click();
  await expect(section.getByLabel("邮箱地址")).toHaveValue(email);
});

test("绑定邮箱后可自助找回：重置撤销旧会话，只有新密码能登录", async ({
  page,
}) => {
  const username = "e2e_reset_flow";
  const email = `${username}@e2e.example.com`;
  await enterCustomerAccount(page, username);
  await bindEmailViaUi(page, username, email);

  // 已登录时 /forgot 会被会话守卫导回工作台（RootApp 的 workspace 屏优先）。
  // 清掉本机 cookie 相当于换一个浏览器访问找回页——服务端那行会话仍然在线，
  // 正是重置要下线的「其他设备上的登录」，必须出现在完成页的计数里。
  await page.context().clearCookies();
  await page.goto("/forgot");
  await expect(page.getByRole("heading", { name: "找回密码" })).toBeVisible();
  await page.getByLabel("用户名或邮箱").fill(username);
  await page.getByRole("button", { name: "发送验证码", exact: true }).click();
  await expect(page.getByRole("heading", { name: "设置新密码" })).toBeVisible();
  await expect(page.getByText(/如果该账号已绑定邮箱/)).toBeVisible();

  const code = readEmailCode(username, "reset_password");
  await page.getByLabel("验证码").fill(code);
  // exact："设置新密码" 的 section / "确认新密码" 都含 "新密码" 子串。
  await page.getByLabel("新密码", { exact: true }).fill("reset-pass-6");
  await page.getByLabel("确认新密码", { exact: true }).fill("reset-pass-6");
  await page.getByRole("button", { name: "重置密码", exact: true }).click();

  await expect(page.getByRole("heading", { name: "密码已重置" })).toBeVisible();
  // 注册时留下的浏览器会话仍在线：重置必须把它撤销并在完成页如实转述。
  await expect(page.getByText(/其他设备上的 \d+ 处登录已下线/)).toBeVisible();

  await page.locator(".account-submit").click();
  await expect(page.getByRole("heading", { name: "登录账号" })).toBeVisible();
  await page.getByLabel("用户名", { exact: true }).fill(username);

  // 旧密码已是死路。
  await page.getByLabel("密码", { exact: true }).fill("test-6");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();

  // 新密码是唯一的钥匙。
  await page.getByLabel("密码", { exact: true }).fill("reset-pass-6");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await waitForCustomerWorkspace(page);
  await expect(page).toHaveURL(/#studio\/workbench$/);
});

test("陌生账号与已绑账号得到同一句话，界面上不回答账号是否存在", async ({
  page,
}) => {
  await page.goto("/forgot");
  await page.getByLabel("用户名或邮箱").fill("e2e_ghost_account");
  await page.getByRole("button", { name: "发送验证码", exact: true }).click();

  await expect(page.getByRole("heading", { name: "设置新密码" })).toBeVisible();
  await expect(page.getByText(/如果该账号已绑定邮箱/)).toBeVisible();
});
