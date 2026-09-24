import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { AccountCreditPanel } from "./AccountCreditPanel";

vi.mock("../api.admin", () => ({
  getAccountCreditSummary: vi.fn().mockResolvedValue({
    user_id: "u1",
    available_credits: 500,
    reserved_credits: 10,
    total_consumed_credits: 90,
    software_consumed_credits: 30,
    other_consumed_credits: 10,
    tokens: [
      {
        id: "k1",
        label: "工作脚本",
        key_prefix: "masked",
        credential_version: 2,
        total_consumed_credits: 50,
        revoked_at: null,
      },
    ],
  }),
  reconcileAccountRecharge: vi.fn(),
}));
it("shows one account ledger and token consumption without a secret control", async () => {
  render(<AccountCreditPanel userId="u1" readOnly />);
  expect(await screen.findByText("工作脚本")).toBeInTheDocument();
  expect(screen.getByText("50 积分")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "核验原订单并补发" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /复制/ }),
  ).not.toBeInTheDocument();
});
