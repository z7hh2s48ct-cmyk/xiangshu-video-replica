import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { CustomerApiError } from "../api";
import { EmailBindingSection } from "./EmailBindingSection";

const mocks = vi.hoisted(() => ({
  state: vi.fn(),
  send: vi.fn(),
  verify: vi.fn(),
}));
vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  customerEmailState: mocks.state,
  customerSendEmailBindCode: mocks.send,
  customerVerifyEmailBindCode: mocks.verify,
}));

beforeEach(() => {
  mocks.state.mockReset();
  mocks.send.mockReset();
  mocks.verify.mockReset();
});

const credential = async () => ({ kind: "session" as const, token: "token-1" });

const unbound = {
  email: null,
  verified_at: null,
  can_bind: true,
  service_available: true,
};

test("未绑定时走发码 → 验证 → 显示已绑定", async () => {
  mocks.state.mockResolvedValue(unbound);
  mocks.send.mockResolvedValue({
    sent: true,
    expires_in_minutes: 15,
    resend_after_seconds: 60,
  });
  mocks.verify.mockResolvedValue({
    email: "alice@example.com",
    verified_at: "2026-09-28T04:00:00Z",
    can_bind: true,
    service_available: true,
  });
  render(<EmailBindingSection credential={credential} />);

  expect(await screen.findByText("未绑定邮箱")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "绑定邮箱" }));
  fireEvent.change(screen.getByLabelText("邮箱地址"), {
    target: { value: "alice@example.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送验证码" }));

  await screen.findByText(/验证码已发往 alice@example.com/);
  expect(mocks.send).toHaveBeenCalledWith(
    { kind: "session", token: "token-1" },
    "alice@example.com",
  );
  // 冷却期内不能重发：秒数来自服务端的 resend_after_seconds。
  expect(
    screen.getByRole("button", { name: /重新发送（\d+ 秒）/ }),
  ).toBeDisabled();

  fireEvent.change(screen.getByLabelText("验证码"), {
    target: { value: "123456" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认绑定" }));

  expect(await screen.findByText("alice@example.com")).toBeVisible();
  expect(screen.getByRole("status")).toHaveTextContent("邮箱已绑定");
  expect(mocks.verify).toHaveBeenCalledWith(
    { kind: "session", token: "token-1" },
    { email: "alice@example.com", code: "123456" },
  );
});

test("已绑定展示地址与验证时间，可进入换绑表单", async () => {
  mocks.state.mockResolvedValue({
    email: "alice@example.com",
    verified_at: "2026-09-28T04:00:00Z",
    can_bind: true,
    service_available: true,
  });
  render(<EmailBindingSection credential={credential} />);

  expect(await screen.findByText("alice@example.com")).toBeVisible();
  expect(screen.getByText(/已验证/)).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: "更换邮箱" }));
  // 换绑预填现有地址：不改的话，用户得把地址再打一遍。
  expect(screen.getByLabelText("邮箱地址")).toHaveValue("alice@example.com");
});

test("子账号不进入绑定流程：密码由主账号管理", async () => {
  mocks.state.mockResolvedValue({ ...unbound, can_bind: false });
  render(<EmailBindingSection credential={credential} />);

  expect(await screen.findByText(/子账号无需绑定邮箱/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "绑定邮箱" })).toBeNull();
});

test("发信未开通时说明原因，不摆注定失败的按钮", async () => {
  mocks.state.mockResolvedValue({ ...unbound, service_available: false });
  render(<EmailBindingSection credential={credential} />);

  expect(await screen.findByText(/邮件服务暂未开通/)).toBeVisible();
  expect(screen.queryByRole("button", { name: "绑定邮箱" })).toBeNull();
});

test("服务端 409 冲突文案原样转述，表单保持可改", async () => {
  mocks.state.mockResolvedValue(unbound);
  mocks.send.mockRejectedValue(
    new CustomerApiError({
      message: "该邮箱已绑定其他账号，请换一个邮箱。",
      status: 409,
      code: "EMAIL_TAKEN",
    }),
  );
  render(<EmailBindingSection credential={credential} />);

  await screen.findByText("未绑定邮箱");
  fireEvent.click(screen.getByRole("button", { name: "绑定邮箱" }));
  fireEvent.change(screen.getByLabelText("邮箱地址"), {
    target: { value: "taken@example.com" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送验证码" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "该邮箱已绑定其他账号",
  );
  // 停留在邮箱表单：换个地址就能重试。
  expect(screen.getByLabelText("邮箱地址")).toHaveValue("taken@example.com");
});

test("状态读取失败时展示重试入口", async () => {
  mocks.state.mockRejectedValue(new Error("绑定邮箱状态暂不可用"));
  render(<EmailBindingSection credential={credential} />);

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("绑定邮箱状态暂不可用");
  // RetryButton 按下后重新拉取：状态变为可读。
  mocks.state.mockResolvedValue({
    email: "alice@example.com",
    verified_at: null,
    can_bind: true,
    service_available: true,
  });
  fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
  expect(await screen.findByText("alice@example.com")).toBeVisible();
});
