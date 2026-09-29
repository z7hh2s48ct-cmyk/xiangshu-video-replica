import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AccountAccessPage } from "./AccountAccessPage";

describe("account gate", () => {
  it("uses the official logo and submits a six-character registration password", async () => {
    const submit = vi.fn().mockResolvedValue(undefined);
    render(<AccountAccessPage onSubmit={submit} onHome={vi.fn()} />);
    // 品牌图形为 src/assets/brand/ 模块导入：小 SVG 内联为 data URI，
    // 超阈值时是哈希文件名，两种形态都接受。
    expect(screen.getByAltText("众墅之家").getAttribute("src")).toMatch(
      /^data:image\/svg\+xml|zhongshu-logo-mark\.svg$/,
    );
    fireEvent.click(screen.getByRole("button", { name: "去注册" }));
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "abc123" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "abc123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "注册并登录" }));
    await waitFor(() =>
      expect(submit).toHaveBeenCalledWith({
        mode: "register",
        username: "alice",
        password: "abc123",
        // 注册不提供记住密码，提交里恒为 false
        remember: false,
      }),
    );
  });

  it("登录时可勾选记住密码，选择随提交一起上报", async () => {
    const submit = vi.fn().mockResolvedValue(undefined);
    render(<AccountAccessPage onSubmit={submit} onHome={vi.fn()} />);

    // 默认不勾：记住口令是用户的显式选择，不能替他决定。
    const remember = screen.getByLabelText("记住密码");
    expect(remember).not.toBeChecked();

    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "abc123" },
    });
    fireEvent.click(remember);
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() =>
      expect(submit).toHaveBeenCalledWith({
        mode: "login",
        username: "alice",
        password: "abc123",
        remember: true,
      }),
    );
  });

  it("已记住的账号会预填，且复选框保持勾选", () => {
    render(
      <AccountAccessPage
        onSubmit={vi.fn()}
        onHome={vi.fn()}
        remembered={{ username: "alice", password: "abc123" }}
      />,
    );

    expect(screen.getByLabelText("用户名")).toHaveValue("alice");
    expect(screen.getByLabelText("密码")).toHaveValue("abc123");
    expect(screen.getByLabelText("记住密码")).toBeChecked();
  });

  it("注册模式不提供记住密码：新账号的口令刚由用户设定，没有免输价值", () => {
    render(<AccountAccessPage onSubmit={vi.fn()} onHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "去注册" }));
    expect(screen.queryByLabelText("记住密码")).toBeNull();
  });

  it("rejects mismatched confirmation without contacting the server", () => {
    const submit = vi.fn();
    render(<AccountAccessPage onSubmit={submit} onHome={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "去注册" }));
    fireEvent.change(screen.getByLabelText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByLabelText("密码"), {
      target: { value: "abc123" },
    });
    fireEvent.change(screen.getByLabelText("确认密码"), {
      target: { value: "abc456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "注册并登录" }));
    expect(screen.getByRole("alert")).toHaveTextContent("两次输入的密码不一致");
    expect(submit).not.toHaveBeenCalled();
  });

  it("登录页提供找回密码入口，注册模式不提供", () => {
    const forgot = vi.fn();
    render(
      <AccountAccessPage
        onSubmit={vi.fn()}
        onHome={vi.fn()}
        onForgotPassword={forgot}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "忘记密码？" }));
    expect(forgot).toHaveBeenCalledTimes(1);

    // 注册模式收回入口：注册的人手里还没有账号，去「找回」只会绕圈。
    fireEvent.click(screen.getByRole("button", { name: "去注册" }));
    expect(screen.queryByRole("button", { name: "忘记密码？" })).toBeNull();
  });

  it("未注入找回回调时不渲染忘记密码入口", () => {
    render(<AccountAccessPage onSubmit={vi.fn()} onHome={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "忘记密码？" })).toBeNull();
  });
});
