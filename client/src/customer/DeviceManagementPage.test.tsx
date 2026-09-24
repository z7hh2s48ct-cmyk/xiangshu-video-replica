import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CustomerDeviceListResponse } from "../api";
import { DeviceManagementPage } from "./DeviceManagementPage";

describe("DeviceManagementPage (FE-04 / T31)", () => {
  const mockDevices: CustomerDeviceListResponse = {
    slots: [
      {
        slot_no: 1,
        device: {
          id: "device-1",
          slot_no: 1,
          display_name: "iPhone •••• AB12",
          platform: "windows",
          status: "BOUND",
          bound_at: new Date(Date.now() - 86400_000).toISOString(),
          last_active_at: new Date().toISOString(),
          unbound_at: null,
          revoked_at: null,
          is_current: true,
        },
      },
      { slot_no: 2, device: null },
    ],
    history: [],
    pending_pairings: [],
  };

  const mockOnUnbind = vi.fn();
  const mockOnRecharge = vi.fn();
  const mockOnPairDevice = vi.fn();

  function renderWithProps(props?: {
    onUnbind?: () => void;
    onRecharge?: () => void;
    onPairDevice?: () => void;
  }) {
    render(
      <DeviceManagementPage
        devices={mockDevices}
        isOnline={true}
        leaseExpiresAt={new Date(Date.now() + 3600_000).toISOString()} // 1 小时后过期
        onPairDevice={props?.onPairDevice ?? mockOnPairDevice}
        onUnbind={props?.onUnbind ?? mockOnUnbind}
        onRecharge={props?.onRecharge ?? mockOnRecharge}
      />,
    );
  }

  it("displays current online status at the top of the page", () => {
    renderWithProps();
    expect(screen.getByText("本机在线")).toBeInTheDocument();
  });

  it("shows two-slot status with current slot #", () => {
    renderWithProps();
    expect(screen.getByText("设备 1")).toBeInTheDocument();
    expect(screen.getByText("设备 2")).toBeInTheDocument();
  });

  it("displays masked device name without exposing full fingerprint", () => {
    renderWithProps();
    expect(screen.getByText(/iPhone •••• AB12/i)).toBeInTheDocument();
  });

  it("空槽位的绑定入口走页内回调而不是 <a href> 整页导航（F-01 review）", () => {
    renderWithProps();
    const bindButton = screen.getByRole("button", { name: "绑定其他设备" });
    expect(bindButton).toBeInTheDocument();
    // Tauri 桌面壳里 <a href> 会整页重载、丢失状态机；必须是回调按钮
    expect(
      screen.queryByRole("link", { name: "绑定其他设备" }),
    ).not.toBeInTheDocument();
    fireEvent.click(bindButton);
    expect(mockOnPairDevice).toHaveBeenCalledTimes(1);
  });

  it("has unbind button for each device slot that calls onUnbind callback", async () => {
    renderWithProps();
    fireEvent.click(screen.getByRole("button", { name: "解绑当前设备" }));

    // Should prompt user to confirm action first
    await waitFor(() => {
      expect(mockOnUnbind).toHaveBeenCalledWith("device-1");
    });
  });

  it("shows lease expiry countdown for active session", () => {
    renderWithProps();
    expect(screen.getByText(/本次登录有效至/)).toBeInTheDocument();
  });

  it("provides recharge button that calls onRecharge callback", () => {
    renderWithProps();
    fireEvent.click(screen.getByRole("button", { name: "充值积分" }));
    expect(mockOnRecharge).toHaveBeenCalledTimes(1);
  });

  it("does not provide second master activation code entry point (wallet panel only)", () => {
    renderWithProps();
    // No input field for entering a second master activation code
    expect(
      screen.queryByLabelText(/second-master-code/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByPlaceholderText(/master-code/i),
    ).not.toBeInTheDocument();
  });

  it("shows appropriate messaging when no second device is available yet", () => {
    renderWithProps();
    expect(screen.getByText(/还没有绑定设备/)).toBeInTheDocument();
  });
});
