import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SessionDisplacedNotice } from "./SessionDisplacedNotice";

describe("SessionDisplacedNotice (FE-03 / T30)", () => {
  it("displays clear message that session was replaced by another device", () => {
    const mockOnRestart = vi.fn();
    render(<SessionDisplacedNotice onRestart={mockOnRestart} />);

    expect(screen.getByText(/session replaced/i)).toBeInTheDocument();
  });

  it("indicates the other device is now active", () => {
    const mockOnRestart = vi.fn();
    render(<SessionDisplacedNotice onRestart={mockOnRestart} />);

    expect(screen.getByText(/now active/i)).toBeInTheDocument();
  });

  it("provides clear instructions to view current session on new device", () => {
    const mockOnRestart = vi.fn();
    render(<SessionDisplacedNotice onRestart={mockOnRestart} />);

    expect(
      screen.getByText(/to see your current session/i),
    ).toBeInTheDocument();
  });

  it("has restart button that calls onRestart callback", () => {
    const mockOnRestart = vi.fn();
    render(<SessionDisplacedNotice onRestart={mockOnRestart} />);

    fireEvent.click(screen.getByRole("button", { name: /view my session/i }));
    expect(mockOnRestart).toHaveBeenCalledTimes(1);
  });
});
