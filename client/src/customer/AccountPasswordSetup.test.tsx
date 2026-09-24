import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { AccountPasswordSetup } from "./AccountPasswordSetup";

vi.mock("../api", () => ({
  customerPasswordState: vi.fn().mockResolvedValue({
    user_id: "u",
    username: "legacy",
    has_password: false,
  }),
  customerSetInitialPassword: vi.fn(),
}));
it("shows initial password setup only for credential-less accounts", async () => {
  render(
    <AccountPasswordSetup
      credential={async () => ({ kind: "session", token: "local" })}
      onComplete={() => {}}
    />,
  );
  expect(await screen.findByLabelText("登录用户名")).toHaveValue("legacy");
  expect(screen.getByLabelText("设置登录密码")).toHaveAttribute(
    "minlength",
    "6",
  );
});

it("输入密码时实时给出强度提示（P1#12）", async () => {
  render(
    <AccountPasswordSetup
      credential={async () => ({ kind: "session", token: "local" })}
      onComplete={() => {}}
    />,
  );
  const input = await screen.findByLabelText("设置登录密码");

  // 还没输入时不显示强度
  expect(screen.queryByText(/强度：/)).toBeNull();

  fireEvent.change(input, { target: { value: "abcdef" } });
  expect(screen.getByText(/强度：弱/)).toBeVisible();

  fireEvent.change(input, { target: { value: "Abcdefg1!xyz" } });
  expect(screen.getByText(/强度：强/)).toBeVisible();
});
