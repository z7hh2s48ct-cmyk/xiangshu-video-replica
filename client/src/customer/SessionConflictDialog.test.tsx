import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SessionConflictDialog } from "./SessionConflictDialog";

describe("SessionConflictDialog (FE-03 / T30)", () => {
  const baseMockConflict = {
    deviceNameMasked: "iPhone •••• AB12",
    leaseExpiresAt: new Date(Date.now() + 1800_000).toISOString(), // 30 分钟后过期
    slotNo: 2,
  };

  function renderWithProps({
    onCancel,
    onSwitch,
    error,
  }: {
    onCancel?: () => void;
    onSwitch?: () => void;
    error?: string | null;
  }) {
    return render(
      <SessionConflictDialog
        conflict={baseMockConflict}
        error={error}
        onCancel={onCancel ?? vi.fn()}
        onSwitch={onSwitch ?? vi.fn()}
      />,
    );
  }

  it("displays masked device name without revealing plaintext identity", () => {
    renderWithProps({});
    expect(
      screen.getByText(/iPhone \u2022\u2022\u2022\u2022 AB12/i),
    ).toBeInTheDocument();
  });

  it("shows lease expiry time in human-readable Chinese", async () => {
    renderWithProps({});
    await waitFor(() => {
      const text = screen.getByText(/租约到期时间/);
      expect(text).toBeInTheDocument();
    });
  });

  it("does not expose any plaintext secrets or full device fingerprint", () => {
    renderWithProps({});
    expect(screen.queryByLabelText("device-token")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("session-token")).not.toBeInTheDocument();
  });

  it("has cancel button that calls onCancel callback", () => {
    const mockOnCancel = vi.fn();
    renderWithProps({ onCancel: mockOnCancel });
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(mockOnCancel).toHaveBeenCalledTimes(1);
  });

  it("切换失败原因必须可见（F-01 review：不得静默恢复可点）", () => {
    renderWithProps({ error: "网络连接失败，请检查网络后重试" });
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("切换失败：网络连接失败，请检查网络后重试");
  });

  it("无错误时不渲染告警位", () => {
    renderWithProps({});
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("has confirm switch button that calls onSwitch callback", () => {
    const mockOnSwitch = vi.fn();
    renderWithProps({ onSwitch: mockOnSwitch });
    fireEvent.click(screen.getByRole("button", { name: "切换到本设备" }));
    expect(mockOnSwitch).toHaveBeenCalledTimes(1);
  });

  it("requires explicit user confirmation before switching (no silent takeover)", () => {
    const mockOnSwitch = vi.fn();
    const mockOnCancel = vi.fn();
    renderWithProps({ onCancel: mockOnCancel, onSwitch: mockOnSwitch });

    // Dialog exists but no automatic action occurs
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Verify callbacks were NOT called during render
    expect(mockOnSwitch).not.toHaveBeenCalled();
    expect(mockOnCancel).not.toHaveBeenCalled();

    // Only after clicking does state change happen
    fireEvent.click(screen.getByRole("button", { name: "切换到本设备" }));
    expect(mockOnSwitch).toHaveBeenCalledTimes(1);
  });

  it("focuses the safe cancel button when opened", () => {
    renderWithProps({});
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
  });

  it("cancels on Escape without touching the switch path", () => {
    const mockOnCancel = vi.fn();
    const mockOnSwitch = vi.fn();
    renderWithProps({ onCancel: mockOnCancel, onSwitch: mockOnSwitch });

    fireEvent.keyDown(document, { key: "Escape" });

    expect(mockOnCancel).toHaveBeenCalledTimes(1);
    expect(mockOnSwitch).not.toHaveBeenCalled();
  });

  it("keeps Tab focus inside the dialog", () => {
    renderWithProps({});
    const cancel = screen.getByRole("button", { name: "取消" });
    const confirm = screen.getByRole("button", { name: "切换到本设备" });
    cancel.focus();

    // Tab from the last focusable element wraps back to the first.
    confirm.focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(cancel).toHaveFocus();

    // Shift+Tab from the first focusable element wraps to the last.
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(confirm).toHaveFocus();
  });
});
