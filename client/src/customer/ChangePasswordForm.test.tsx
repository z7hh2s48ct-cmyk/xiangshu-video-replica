import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { CustomerApiError } from "../api";
import { ChangePasswordForm } from "./ChangePasswordForm";

const mocks = vi.hoisted(() => ({ change: vi.fn() }));
vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  customerChangePassword: mocks.change,
}));

const credential = async () => ({ kind: "session" as const, token: "token-1" });

function fill(current: string, next: string, repeat: string) {
  fireEvent.change(screen.getByLabelText("当前密码"), {
    target: { value: current },
  });
  fireEvent.change(screen.getByLabelText("新密码"), {
    target: { value: next },
  });
  fireEvent.change(screen.getByLabelText("再次输入新密码"), {
    target: { value: repeat },
  });
}

test("两次输入不一致时不发请求，并给出可读的提示", async () => {
  mocks.change.mockReset();
  render(<ChangePasswordForm credential={credential} onChanged={vi.fn()} />);
  fill("old-pass-9", "new-pass-10", "new-pass-11");

  fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("两次输入需一致");
  expect(mocks.change).not.toHaveBeenCalled();
});

test("新密码与当前密码相同时不发请求", async () => {
  mocks.change.mockReset();
  render(<ChangePasswordForm credential={credential} onChanged={vi.fn()} />);
  fill("same-pass-9", "same-pass-9", "same-pass-9");

  fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

  expect(await screen.findByRole("alert")).toHaveTextContent(
    "不能与当前密码相同",
  );
  expect(mocks.change).not.toHaveBeenCalled();
});

test("成功后清空输入并把被撤销的会话数交给调用方", async () => {
  mocks.change.mockReset();
  mocks.change.mockResolvedValue({ changed: true, sessions_revoked: 1 });
  const onChanged = vi.fn();
  render(<ChangePasswordForm credential={credential} onChanged={onChanged} />);
  fill("old-pass-9", "new-pass-10", "new-pass-10");

  fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

  await waitFor(() => expect(onChanged).toHaveBeenCalledWith(1));
  expect(mocks.change).toHaveBeenCalledWith(
    { kind: "session", token: "token-1" },
    { currentPassword: "old-pass-9", newPassword: "new-pass-10" },
    expect.any(String),
  );
  expect(screen.getByLabelText("新密码")).toHaveValue("");
});

test("改密成功但随后登出失败时，不谎报「密码修改失败」", async () => {
  mocks.change.mockReset();
  mocks.change.mockResolvedValue({ changed: true, sessions_revoked: 1 });
  // 凭据已经换掉了，之后的登出失败是另一回事。
  const onChanged = vi.fn().mockRejectedValue(new Error("网络连接失败"));
  render(<ChangePasswordForm credential={credential} onChanged={onChanged} />);
  fill("old-pass-9", "new-pass-10", "new-pass-10");

  fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("密码已修改");
  expect(alert).not.toHaveTextContent("密码修改失败");
  // 表单清空：旧密码已作废，留着只会引诱用户拿它重试并撞 401。
  expect(screen.getByLabelText("新密码")).toHaveValue("");
});

test("服务端拒绝时留在表单上，不把用户当成功送走", async () => {
  mocks.change.mockReset();
  mocks.change.mockRejectedValue(
    new CustomerApiError({
      message: "当前密码不正确。",
      status: 401,
      code: "INVALID_CREDENTIALS",
    }),
  );
  const onChanged = vi.fn();
  render(<ChangePasswordForm credential={credential} onChanged={onChanged} />);
  fill("wrong-pass-9", "new-pass-10", "new-pass-10");

  fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

  expect(await screen.findByRole("alert")).toHaveTextContent("当前密码不正确");
  expect(onChanged).not.toHaveBeenCalled();
  expect(screen.getByLabelText("当前密码")).toHaveValue("wrong-pass-9");
});
