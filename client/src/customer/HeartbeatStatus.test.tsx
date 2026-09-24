import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HeartbeatStatus } from "./HeartbeatStatus";

describe("HeartbeatStatus (FE-04 / T31)", () => {
  const mockLastHeartbeat = new Date(Date.now() - 15_000).toISOString(); // 15 秒前

  it("displays formatted heartbeat timestamp", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/上次心跳/)).toBeInTheDocument();
  });

  it("shows connection healthy status when within 80% interval", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        intervalSeconds={30}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/连接正常/)).toBeInTheDocument();
  });

  it("shows warning when heartbeat is overdue (> interval)", () => {
    const mockOverdue = new Date(Date.now() - 40_000).toISOString(); // 40 秒前
    const mockOnRefresh = vi.fn();

    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockOverdue}
        intervalSeconds={30}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/心跳已逾期/)).toBeInTheDocument();
  });

  it("reports a long-overdue heartbeat without claiming the server lease expired", () => {
    const mockExpired = new Date(Date.now() - 70_000).toISOString(); // 70 秒前
    const mockOnRefresh = vi.fn();

    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockExpired}
        intervalSeconds={30}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.getByText(/长时间未收到心跳结果/)).toBeInTheDocument();
    expect(screen.queryByText(/会话已过期/)).not.toBeInTheDocument();
  });

  it("provides refresh button that calls onRefresh callback", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        onRefresh={mockOnRefresh}
      />,
    );

    const button = screen.getByLabelText(/立即发送心跳/);
    button.click();
    expect(mockOnRefresh).toHaveBeenCalledTimes(1);
  });

  it("does not display any plaintext tokens or secrets in the UI", () => {
    const mockOnRefresh = vi.fn();
    render(
      <HeartbeatStatus
        lastHeartbeatAt={mockLastHeartbeat}
        onRefresh={mockOnRefresh}
      />,
    );

    expect(screen.queryByLabelText(/session-token/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/device-token/i)).not.toBeInTheDocument();
  });
});
