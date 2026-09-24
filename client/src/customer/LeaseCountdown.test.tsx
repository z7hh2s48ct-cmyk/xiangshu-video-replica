import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LeaseCountdown } from "./LeaseCountdown";

describe("LeaseCountdown (FE-04 / T31)", () => {
  // A fixed clock keeps the warning/expired boundaries deterministic — the
  // lease math is relative to Date.now(), and a drifting wall clock (or a
  // shared fake-timer pool) would otherwise flake at the <5min edge.
  const FIXED_NOW = new Date("2026-08-24T12:00:00Z").getTime();
  const mockExpiresAt = new Date(FIXED_NOW + 1800_000).toISOString(); // 30 分钟后过期

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("displays formatted expiration timestamp", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    expect(screen.getByText(/本次会话有效至/)).toBeInTheDocument();
  });

  it("shows countdown timer updating every second", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    // Initial display should show time remaining
    expect(screen.getByText(/剩余 \d+ 分钟/)).toBeInTheDocument();
  });

  it("changes to warning state when lease < 5 minutes", () => {
    const mockNearExpiry = new Date(
      FIXED_NOW + 4 * 60_000 + 59_000,
    ).toISOString(); // 4 分 59 秒 — 严格小于 5 分钟边界
    const mockOnRefresh = vi.fn();

    render(
      <LeaseCountdown expiresAt={mockNearExpiry} onRefresh={mockOnRefresh} />,
    );

    expect(screen.getByText(/即将过期/)).toBeInTheDocument();
  });

  it("changes to expired state when lease <= 0", () => {
    const mockExpired = new Date(FIXED_NOW - 1000).toISOString(); // 已过期
    const mockOnRefresh = vi.fn();

    render(
      <LeaseCountdown expiresAt={mockExpired} onRefresh={mockOnRefresh} />,
    );

    expect(screen.getByText(/已过期/)).toBeInTheDocument();
  });

  it("provides refresh/renew button that calls onRefresh callback", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    const button = screen.getByLabelText(/续约会话/);
    button.click();
    expect(mockOnRefresh).toHaveBeenCalledTimes(1);
  });

  it("does not display any plaintext tokens or secrets in the UI", () => {
    const mockOnRefresh = vi.fn();
    render(
      <LeaseCountdown expiresAt={mockExpiresAt} onRefresh={mockOnRefresh} />,
    );

    expect(screen.queryByLabelText(/session-token/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/device-token/i)).not.toBeInTheDocument();
  });
});
