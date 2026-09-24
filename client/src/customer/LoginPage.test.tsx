import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { CustomerApiError } from "../api";
import { LoginPage } from "./LoginPage";

describe("LoginPage", () => {
  it("offers a device-credential login without asking for a code", () => {
    const onRetryLogin = vi.fn();
    render(
      <LoginPage
        onRetryLogin={onRetryLogin}
        isBusy={false}
        error={null}
        conflict={null}
      />,
    );

    // The stored device credential is the login material — the user never
    // retypes the activation code here.
    expect(
      screen.getByRole("button", { name: "使用本机设备登录" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "众墅之家" })).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveClass("customer-access-shell");
    expect(screen.queryByLabelText(/激活码/)).toBeNull();
    expect(screen.queryByLabelText(/内部访问令牌/)).toBeNull();
  });

  it("retries the login when the button is clicked", () => {
    const onRetryLogin = vi.fn();
    render(
      <LoginPage
        onRetryLogin={onRetryLogin}
        isBusy={false}
        error={null}
        conflict={null}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "使用本机设备登录" }));
    expect(onRetryLogin).toHaveBeenCalledTimes(1);
  });

  it("disables the retry while a login is in flight", () => {
    render(
      <LoginPage
        onRetryLogin={vi.fn()}
        isBusy={true}
        error={null}
        conflict={null}
      />,
    );

    expect(screen.getByRole("button", { name: "正在登录…" })).toBeDisabled();
  });

  it("shows the masked other-device conflict without a switch action", () => {
    // T29 shows the conflict read-only; the explicit takeover (switch) flow
    // belongs to T30 and must not be offered prematurely.
    render(
      <LoginPage
        onRetryLogin={vi.fn()}
        isBusy={false}
        error={
          new CustomerApiError({
            message: "另一台设备在线",
            status: 409,
            code: "OTHER_DEVICE_ONLINE",
          })
        }
        conflict={{
          deviceNameMasked: "张**的 iPad",
          slotNo: 2,
          leaseExpiresAt: "2026-08-24T12:05:00Z",
        }}
      />,
    );

    expect(screen.getByText(/张\*\*的 iPad/)).toBeInTheDocument();
    expect(screen.getByText(/2 号设备槽/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /切换|顶替|接管/ })).toBeNull();
  });

  it("surfaces a non-conflict login failure with its message", () => {
    render(
      <LoginPage
        onRetryLogin={vi.fn()}
        isBusy={false}
        error={
          new CustomerApiError({
            message: "网络连接失败，请检查网络",
            transportKind: "network",
          })
        }
        conflict={null}
      />,
    );

    expect(screen.getByText("网络连接失败，请检查网络")).toBeInTheDocument();
  });
});
