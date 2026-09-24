import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PairingApprovalCard } from "./PairingApprovalCard";

describe("PairingApprovalCard (FE-03 / T30)", () => {
  const mockPendingPairing = {
    id: "test-pairing-id",
    deviceFingerprint: "Android •••• XY78",
    slotNo: 2,
    createdAt: new Date(Date.now() - 3600_000).toISOString(), // 1 小时前创建
  };

  const mockOnApprove = vi.fn();
  const mockOnDelete = vi.fn();
  const mockOnReject = vi.fn();

  function renderWithProps(props?: {
    onApprove?: () => void;
    onDelete?: () => void;
    onReject?: () => void;
  }) {
    render(
      <PairingApprovalCard
        pairing={mockPendingPairing}
        onApprove={props?.onApprove ?? mockOnApprove}
        onDelete={props?.onDelete ?? mockOnDelete}
        onReject={props?.onReject ?? mockOnReject}
      />,
    );
  }

  it("displays masked device fingerprint without exposing plaintext identity", () => {
    renderWithProps();
    expect(
      screen.getByText(/Android \u2022\u2022\u2022\u2022 XY78/i),
    ).toBeInTheDocument();
  });

  it("shows the requested device slot clearly", () => {
    renderWithProps();
    expect(screen.getByText(/设备 2/)).toBeInTheDocument();
  });

  it("does not expose any plaintext secrets or tokens", () => {
    renderWithProps();
    expect(screen.queryByLabelText("device-token")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("session-token")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("pairing-secret")).not.toBeInTheDocument();
  });

  it("has approve button that calls onApprove callback with pairing ID", () => {
    renderWithProps();
    fireEvent.click(screen.getByRole("button", { name: "确认绑定" }));
    expect(mockOnApprove).toHaveBeenCalledWith("test-pairing-id");
  });

  it("has reject button that calls onReject callback", () => {
    renderWithProps();
    fireEvent.click(screen.getByRole("button", { name: "暂不处理" }));
    expect(mockOnReject).toHaveBeenCalledTimes(1);
  });

  it("can delete an invalid pairing request explicitly", () => {
    renderWithProps();
    fireEvent.click(screen.getByRole("button", { name: "删除无效请求" }));
    expect(mockOnDelete).toHaveBeenCalledWith("test-pairing-id");
  });

  it("requires explicit confirmation before approving (no silent approval)", () => {
    const mockApprove = vi.fn();
    const mockReject = vi.fn();
    renderWithProps({ onApprove: mockApprove, onReject: mockReject });

    // Card exists but no automatic action occurs
    expect(screen.getByRole("article")).toBeInTheDocument();

    // Verify callbacks were NOT called during render
    expect(mockApprove).not.toHaveBeenCalled();
    expect(mockReject).not.toHaveBeenCalled();

    // Only after clicking approve does state change happen
    fireEvent.click(screen.getByRole("button", { name: "确认绑定" }));
    expect(mockApprove).toHaveBeenCalledWith("test-pairing-id");
  });

  it("indicates pending status visually with appropriate messaging", () => {
    renderWithProps();
    expect(screen.getByText(/待确认 · 设备 2/)).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "新的设备绑定请求" }),
    ).toBeInTheDocument();
  });
});
