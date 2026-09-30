import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { WalletTransaction } from "../api";
import { customerGetPriceVersion } from "../api";
import {
  priceVersionText,
  pricingLines,
  TransactionPricingBreakdown,
} from "./TransactionPricingBreakdown";

vi.mock("../api", () => ({ customerGetPriceVersion: vi.fn() }));

function ledgerRow(
  overrides: Partial<WalletTransaction> = {},
): WalletTransaction {
  return {
    id: "tx-1",
    user_id: "user-1",
    type: "SETTLE",
    available_delta: 0,
    reserved_delta: -3,
    recharge_order_id: null,
    task_id: "task-1",
    billing_round: 1,
    created_at: "2026-09-22 10:00:00",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("pricingLines", () => {
  it("explains a settled charge by unit price, usage, discount and rounding", () => {
    const lines = pricingLines(
      ledgerRow({
        service: "asr",
        service_name: "语音转写",
        pricing: {
          service: "asr",
          version: 3,
          unit: "second",
          units: "3.000000",
          unit_credits: "2.000000",
          unit_rounding: "ceil",
          discount_basis_points: 9500,
          consumption_rounding: "floor",
          credits: 5,
          enabled: true,
          free_reason: null,
        },
      }),
    );

    expect(lines).toEqual([
      "单价：2 积分/秒，不足 1 秒按 1 秒计",
      "提交用量：3 秒",
      "折扣：95%（9.5 折）",
      "消费取整：向下取整（最低 1 积分）",
      "暂扣上限：5 积分",
      "实际扣费按成功交付用量结算，差额同笔退回",
    ]);
  });

  it("keeps decimals that carry value and trims only formatting zeros", () => {
    const fractional = pricingLines(
      ledgerRow({
        pricing: {
          unit: "second",
          units: "12.500000",
          unit_credits: "0.500000",
          unit_rounding: "exact",
          discount_basis_points: 10000,
          consumption_rounding: "ceil",
          credits: 7,
          enabled: true,
          version: 2,
        },
      }),
    );

    expect(fractional).toContain("单价：0.5 积分/秒");
    expect(fractional).toContain("提交用量：12.5 秒");
    // 整十的单价不能被去尾零误伤成 "1"。
    expect(
      pricingLines(
        ledgerRow({
          pricing: { unit: "image", unit_credits: "100.000000", credits: 100 },
        }),
      ),
    ).toContain("单价：100 积分/张");
  });

  it("says why a row was free instead of printing a zero chain", () => {
    const lines = pricingLines(
      ledgerRow({
        type: "RESERVE",
        reserved_delta: 0,
        billing_round: null,
        pricing: {
          service: "quality_inspection",
          version: 0,
          unit: "call",
          units: "1.000000",
          unit_credits: "0",
          unit_rounding: "ceil",
          discount_basis_points: 10000,
          consumption_rounding: "ceil",
          credits: 0,
          enabled: false,
          free_reason: "platform_service",
        },
      }),
    );

    expect(lines).toEqual(["未计费：平台承担，不向客户计费"]);
  });

  it("omits the parts the snapshot never recorded", () => {
    const lines = pricingLines(
      ledgerRow({
        billing_round: null,
        actor_user_id: "user-1",
        pricing: {
          unit: "call",
          units: "1.000000",
          unit_credits: "4.000000",
          enabled: true,
          credits: 4,
        },
      }),
    );

    expect(lines).toEqual([
      "单价：4 积分/次",
      "提交用量：1 次",
      "暂扣上限：4 积分",
      "实际扣费按成功交付用量结算，差额同笔退回",
    ]);
  });

  it("names the billing round and the sub account that spent the credits", () => {
    const lines = pricingLines(
      ledgerRow({
        type: "RESERVE",
        reserved_delta: 30,
        billing_round: 3,
        actor_user_id: "sub-2",
        actor_name: "剪辑助手",
        pricing: {
          version: 1,
          unit: "second",
          units: "30.000000",
          unit_credits: "1.000000",
          unit_rounding: "ceil",
          discount_basis_points: 10000,
          consumption_rounding: "ceil",
          credits: 30,
          enabled: true,
        },
      }),
    );

    expect(lines).toContain("单价：1 积分/秒，不足 1 秒按 1 秒计");
    expect(lines).toContain("折扣：无折扣（按价目表单价）");
    expect(lines).toContain("消费取整：向上取整（最低 1 积分）");
    expect(lines).toContain("计费轮次：第 3 轮");
    expect(lines).toContain("提交时按暂扣上限先扣留额度");
    expect(lines).toContain("操作人：剪辑助手");
    // 子账号没有名字时退回稳定标识，绝不显示成自己的操作。
    expect(
      pricingLines(
        ledgerRow({ actor_user_id: "sub-2", actor_name: null, pricing: null }),
      ),
    ).toEqual([]);
    expect(
      pricingLines(
        ledgerRow({
          actor_user_id: "sub-2",
          actor_name: null,
          pricing: { unit: "call", credits: 4, enabled: true },
        }),
      ),
    ).toContain("操作人：sub-2");
  });

  it("stays silent for rows without a frozen snapshot", () => {
    expect(pricingLines(ledgerRow())).toEqual([]);
    expect(pricingLines(ledgerRow({ pricing: null }))).toEqual([]);

    const { container } = render(
      <TransactionPricingBreakdown transaction={ledgerRow()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});

describe("TransactionPricingBreakdown", () => {
  it("renders the basis behind a collapsed summary carrying the price version", () => {
    render(
      <TransactionPricingBreakdown
        transaction={ledgerRow({
          pricing: {
            version: 3,
            unit: "second",
            units: "3.000000",
            unit_credits: "2.000000",
            unit_rounding: "ceil",
            discount_basis_points: 9500,
            consumption_rounding: "floor",
            credits: 5,
            enabled: true,
          },
        })}
      />,
    );

    const summary = screen.getByText("计费依据 · 费率 V3");
    expect(summary.tagName).toBe("SUMMARY");
    expect(summary.closest("details")?.open).toBe(false);
    expect(
      screen.getByText("单价：2 积分/秒，不足 1 秒按 1 秒计"),
    ).toBeInTheDocument();
  });

  it("drops the version tag when the snapshot carries no priced tariff", () => {
    render(
      <TransactionPricingBreakdown
        transaction={ledgerRow({
          pricing: {
            version: 0,
            unit: "call",
            units: "1.000000",
            unit_credits: "0",
            credits: 0,
            enabled: false,
            free_reason: "unconfigured",
          },
        })}
      />,
    );

    expect(screen.getByText("计费依据")).toBeInTheDocument();
    expect(screen.getByText("未计费：该科目尚未配置价目")).toBeInTheDocument();
  });
});

describe("priceVersionText", () => {
  it("names the version, the price, the effective date and the live version", () => {
    const historical = {
      service: "asr",
      name: "语音转写",
      unit: "second",
      version: 3,
      current_version: 5,
      found: true,
      current: false,
      enabled: true,
      unit_credits: "2.000000",
      unit_rounding: "ceil",
      effective_at: "2026-09-20T04:00:00+00:00",
    };
    expect(priceVersionText(historical)).toBe(
      "V3 价目：2 积分/秒，不足 1 秒按 1 秒计，2026-09-20 生效（当前版本 V5）",
    );
  });
  it("marks a still-current version as the live one", () => {
    const current = {
      service: "asr",
      name: "语音转写",
      unit: "second",
      version: 5,
      current_version: 5,
      found: true,
      current: true,
      enabled: true,
      unit_credits: "2.000000",
      unit_rounding: "ceil",
      effective_at: "2026-09-20T04:00:00+00:00",
    };
    expect(priceVersionText(current)).toBe(
      "V5 价目：2 积分/秒，不足 1 秒按 1 秒计，2026-09-20 生效（当前版本）",
    );
  });
  it("says an unrecoverable version cannot be looked up", () => {
    const unavailable = {
      service: "asr",
      name: "语音转写",
      unit: "second",
      version: 9,
      current_version: 5,
      found: false,
      current: false,
      enabled: null,
      unit_credits: null,
      unit_rounding: null,
      effective_at: null,
    };
    expect(priceVersionText(unavailable)).toBe(
      "V9 的价目已不可回查（可能是系统上线前的历史数据）。",
    );
  });
});

describe("price version lookup", () => {
  const credential = () =>
    Promise.resolve({ kind: "session" as const, token: "token-1" });
  const settled = ledgerRow({
    service: "asr",
    pricing: {
      service: "asr",
      version: 3,
      unit: "second",
      units: "3.000000",
      unit_credits: "2.000000",
      unit_rounding: "ceil",
      discount_basis_points: 9500,
      consumption_rounding: "floor",
      credits: 5,
      enabled: true,
    },
  });

  it("looks a settled charge's price up on demand instead of on render", async () => {
    vi.mocked(customerGetPriceVersion).mockResolvedValue({
      service: "asr",
      name: "语音转写",
      unit: "second",
      version: 3,
      current_version: 5,
      found: true,
      current: false,
      enabled: true,
      unit_credits: "2.000000",
      unit_rounding: "ceil",
      effective_at: "2026-09-20T04:00:00+00:00",
    });
    render(
      <TransactionPricingBreakdown
        credential={credential}
        transaction={settled}
      />,
    );
    expect(customerGetPriceVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/V3 价目/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查当时价目" }));
    await waitFor(() =>
      expect(customerGetPriceVersion).toHaveBeenCalledWith(
        { kind: "session", token: "token-1" },
        "asr",
        3,
      ),
    );
    const result = await screen.findByText(/V3 价目：2 积分\/秒/);
    expect(result).toHaveTextContent("不足 1 秒按 1 秒计");
    expect(result).toHaveTextContent("2026-09-20 生效");
    expect(result).toHaveTextContent("当前版本 V5");
    expect(
      screen.queryByRole("button", { name: "查当时价目" }),
    ).not.toBeInTheDocument();
  });

  it("marks the looked-up price as current when the charge matches the live version", async () => {
    vi.mocked(customerGetPriceVersion).mockResolvedValue({
      service: "asr",
      name: "语音转写",
      unit: "second",
      version: 5,
      current_version: 5,
      found: true,
      current: true,
      enabled: true,
      unit_credits: "2.000000",
      unit_rounding: "ceil",
      effective_at: "2026-09-20T04:00:00+00:00",
    });
    render(
      <TransactionPricingBreakdown
        credential={credential}
        transaction={ledgerRow({
          service: "asr",
          pricing: {
            service: "asr",
            version: 5,
            unit: "second",
            credits: 5,
            enabled: true,
          },
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "查当时价目" }));
    expect(await screen.findByText(/（当前版本）/)).toBeInTheDocument();
  });

  it("hides the lookup unless the row carries both a version and a credential", () => {
    const { container, rerender } = render(
      <TransactionPricingBreakdown transaction={settled} />,
    );
    expect(
      screen.queryByRole("button", { name: "查当时价目" }),
    ).not.toBeInTheDocument();
    rerender(
      <TransactionPricingBreakdown
        credential={credential}
        transaction={ledgerRow()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("surfaces a failed lookup without breaking the breakdown", async () => {
    vi.mocked(customerGetPriceVersion).mockRejectedValue(
      new Error("查询失败，请稍后重试。"),
    );
    render(
      <TransactionPricingBreakdown
        credential={credential}
        transaction={settled}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "查当时价目" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "查询失败，请稍后重试。",
    );
  });

  it("tells the customer to sign in again when the session has expired", async () => {
    render(
      <TransactionPricingBreakdown
        credential={() => Promise.resolve(null)}
        transaction={settled}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "查当时价目" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "登录状态已失效",
    );
  });
});
