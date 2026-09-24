import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const failure = "本机已退出，但服务端未能确认释放会话，请检查网络后重试";
vi.mock("./customer/useCustomerSession", () => ({
  // 登录页挂载前会读一次「记住密码」；桩里返回 null 表示没记住过。
  customerCredentialStore: () => ({
    loadRememberedLogin: async () => null,
  }),
  useCustomerSession: () => ({
    screen: "login",
    error: new Error("本机已退出，但服务端未能确认释放会话，请检查网络后重试"),
    isBusy: false,
    conflict: null,
    loginWithPassword: vi.fn(),
  }),
}));

import { RootApp } from "./RootApp";

describe("上线前诊断：退出异常提示必须到达用户", () => {
  it.each(["/", "/login"])("在 %s 展示会话释放失败原因", (pathname) => {
    window.history.replaceState({}, "", pathname);
    render(<RootApp path={pathname} />);
    expect(screen.queryByText(failure)).not.toBeNull();
  });
});
