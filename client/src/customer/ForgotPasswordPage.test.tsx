import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import { CustomerApiError } from "../api";
import { ForgotPasswordPage } from "./ForgotPasswordPage";

const mocks = vi.hoisted(() => ({ forgot: vi.fn(), reset: vi.fn() }));
vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  customerForgotPassword: mocks.forgot,
  customerResetPassword: mocks.reset,
}));

beforeEach(() => {
  mocks.forgot.mockReset();
  mocks.reset.mockReset();
});

const accepted = {
  accepted: true,
  message: "如果该账号已绑定邮箱，验证码已发送，请查收邮件。",
  resend_after_seconds: 60,
};

async function enterCodeStep() {
  fireEvent.change(screen.getByLabelText("用户名或邮箱"), {
    target: { value: "alice" },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送验证码" }));
  await screen.findByText(/如果该账号已绑定邮箱/);
}

test("走完找回流程：发码 → 重置 → 完成页，并回传返回登录", async () => {
  mocks.forgot.mockResolvedValue(accepted);
  mocks.reset.mockResolvedValue({ reset: true, sessions_revoked: 2 });
  const onBack = vi.fn();
  render(<ForgotPasswordPage onBack={onBack} />);

  await enterCodeStep();
  expect(mocks.forgot).toHaveBeenCalledWith("alice");
  // 冷却期内不能重发：秒数来自服务端的 resend_after_seconds。
  expect(
    screen.getByRole("button", { name: /重新获取验证码（\d+ 秒）/ }),
  ).toBeDisabled();

  fireEvent.change(screen.getByLabelText("验证码"), {
    target: { value: "123456" },
  });
  fireEvent.change(screen.getByLabelText("新密码"), {
    target: { value: "new-pass-1" },
  });
  fireEvent.change(screen.getByLabelText("确认新密码"), {
    target: { value: "new-pass-1" },
  });
  fireEvent.click(screen.getByRole("button", { name: "重置密码" }));

  expect(
    await screen.findByRole("heading", { name: "密码已重置" }),
  ).toBeInTheDocument();
  expect(mocks.reset).toHaveBeenCalledWith({
    account: "alice",
    code: "123456",
    newPassword: "new-pass-1",
  });
  // 撤销的会话数如实转述：重置密码的人往往正想踢掉别的登录。
  expect(screen.getByText(/2 处登录已下线/)).toBeVisible();

  fireEvent.click(screen.getAllByRole("button", { name: "返回登录" })[0]);
  expect(onBack).toHaveBeenCalledTimes(1);
});

test("两次新密码不一致时不发请求", async () => {
  mocks.forgot.mockResolvedValue(accepted);
  render(<ForgotPasswordPage onBack={vi.fn()} />);

  await enterCodeStep();
  fireEvent.change(screen.getByLabelText("验证码"), {
    target: { value: "123456" },
  });
  fireEvent.change(screen.getByLabelText("新密码"), {
    target: { value: "abc123" },
  });
  fireEvent.change(screen.getByLabelText("确认新密码"), {
    target: { value: "abc456" },
  });
  fireEvent.click(screen.getByRole("button", { name: "重置密码" }));

  expect(screen.getByRole("alert")).toHaveTextContent("两次输入的密码不一致");
  expect(mocks.reset).not.toHaveBeenCalled();
});

test("纯空白账号按未填写处理，不触碰服务端", () => {
  render(<ForgotPasswordPage onBack={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("用户名或邮箱"), {
    target: { value: "   " },
  });
  fireEvent.click(screen.getByRole("button", { name: "发送验证码" }));

  expect(screen.getByRole("alert")).toHaveTextContent("请输入用户名或邮箱");
  expect(mocks.forgot).not.toHaveBeenCalled();
});

test("错码时停留在设置页并原样转述服务端文案", async () => {
  mocks.forgot.mockResolvedValue(accepted);
  mocks.reset.mockRejectedValue(
    new CustomerApiError({
      message: "验证码不正确或已失效，请检查后重试或重新获取。",
      status: 400,
      code: "INVALID_CODE",
    }),
  );
  render(<ForgotPasswordPage onBack={vi.fn()} />);

  await enterCodeStep();
  fireEvent.change(screen.getByLabelText("验证码"), {
    target: { value: "000000" },
  });
  fireEvent.change(screen.getByLabelText("新密码"), {
    target: { value: "new-pass-1" },
  });
  fireEvent.change(screen.getByLabelText("确认新密码"), {
    target: { value: "new-pass-1" },
  });
  fireEvent.click(screen.getByRole("button", { name: "重置密码" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "验证码不正确或已失效",
  );
  // 失败不前进：留在设置页，冷却由服务端语义决定（这里是本地校验之外的重试）。
  expect(
    screen.getByRole("heading", { name: "设置新密码" }),
  ).toBeInTheDocument();
});
