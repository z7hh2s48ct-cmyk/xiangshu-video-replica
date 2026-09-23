import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { LegacyCreditPolicyManager } from "./LegacyCreditPolicyManager";

vi.mock("../api.admin", () => ({
  getLegacyCreditPolicy: vi.fn().mockResolvedValue({
    version: 2,
    mode: "convert",
    numerator: 3,
    denominator: 2,
  }),
  getLegacyCreditConversion: vi.fn().mockResolvedValue({
    user_id: "u",
    before_credits: 100,
    after_credits: 150,
    converted: false,
    policy: { version: 2, mode: "convert", numerator: 3, denominator: 2 },
  }),
  saveLegacyCreditPolicy: vi.fn(),
  applyLegacyCreditConversion: vi.fn(),
}));
it("shows the frozen preview and prevents auditors from converting", async () => {
  render(<LegacyCreditPolicyManager userId="u" readOnly />);
  expect(await screen.findByText(/100 积分 → 150 积分/)).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "执行本账号转换" }),
  ).not.toBeInTheDocument();
});
