import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { adminRead, listCustomers, submitBillingQuote } from "../api.admin";
import { BillingQuoteCalculator } from "./BillingQuoteCalculator";

vi.mock("../api.admin", () => ({
  adminRead: vi.fn(),
  listCustomers: vi.fn(),
  submitBillingQuote: vi.fn(),
}));

const catalog = {
  services: [
    {
      service: "video_768p",
      name: "视频生成 · 768P",
      unit: "second",
      provider: "metaso",
    },
    { service: "oral", name: "数字人口播", unit: "second", provider: "hifly" },
  ],
};

describe("BillingQuoteCalculator", () => {
  test.each([
    ["manual", "专项折扣"],
    ["recharge_package", "套餐权益"],
    ["global", "全局折扣"],
  ] as const)(
    "auditor quotes the fixed customer with %s",
    async (kind, label) => {
      vi.mocked(submitBillingQuote).mockResolvedValue({
        service: "oral",
        label: "数字人口播",
        units: "0.4",
        unit: "second",
        credits: "1",
        unit_credits: "2.5",
        final_unit_credits: "2",
        unit_nominal_fen: "0.002",
        discount_basis_points: 8000,
        discount_kind: kind,
        nominal_fen: "0.001",
        cost_fen: null,
        gross_fen: null,
        unit_rounding: "exact",
        consumption_rounding: "ceil",
      });
      render(
        <BillingQuoteCalculator
          readOnly
          userId="exact-customer"
          customerLabel="当前公司"
        />,
      );
      await screen.findByRole("option", { name: "数字人口播" });
      expect(
        screen.queryByPlaceholderText("用户名或公司名"),
      ).not.toBeInTheDocument();
      expect(screen.getByText("试算客户：当前公司")).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("业务"), {
        target: { value: "oral" },
      });
      fireEvent.change(screen.getByLabelText(/数量/), {
        target: { value: "0.4" },
      });
      const button = screen.getByRole("button", { name: "试算" });
      expect(button).toBeEnabled();
      fireEvent.click(button);
      const table = await screen.findByRole("table", { name: "试算结果" });
      expect(submitBillingQuote).toHaveBeenCalledWith({
        service: "oral",
        units: 0.4,
        userId: "exact-customer",
      });
      expect(table).toHaveTextContent(`8 折（${label}）`);
      expect(table).toHaveTextContent("单位最终售价2 积分 / 秒");
      expect(table).toHaveTextContent("< ¥0.01");
      expect(table).toHaveTextContent("待核对（未配置成本单价）");
    },
  );

  beforeEach(() => {
    vi.mocked(adminRead).mockReset();
    vi.mocked(listCustomers).mockReset();
    vi.mocked(submitBillingQuote).mockReset();
    vi.mocked(adminRead).mockImplementation(async () => catalog as never);
    vi.mocked(listCustomers).mockImplementation(
      async () =>
        ({
          items: [
            {
              user_id: "cust-1",
              username: "customer-1",
              display_name: "客户一公司",
            },
          ],
          total: 1,
          limit: 5,
          offset: 0,
        }) as never,
    );
  });

  test("quotes against the backend pricing with a picked customer", async () => {
    vi.mocked(submitBillingQuote).mockResolvedValue({
      service: "video_768p",
      label: "视频生成 · 768P",
      units: "10",
      unit: "second",
      credits: "25",
      unit_credits: "2.5",
      discount_basis_points: 8500,
      discount_rate: "0.85",
      discount_source: "manual",
      discount_kind: "manual",
      final_unit_credits: "3",
      unit_nominal_fen: "3",
      unit_rounding: "exact",
      consumption_rounding: "ceil",
      nominal_fen: "25",
      cost_fen: "12.5",
      gross_fen: "12.5",
    });

    render(<BillingQuoteCalculator />);

    // 客户：关键字搜索 → 候选 → 选中（试算必须带客户维度才能体现专项折扣）。
    fireEvent.change(await screen.findByPlaceholderText("用户名或公司名"), {
      target: { value: "客户一" },
    });
    fireEvent.click(screen.getByRole("button", { name: "搜客户" }));
    fireEvent.click(await screen.findByRole("button", { name: /客户一公司/ }));

    fireEvent.change(screen.getByLabelText("业务"), {
      target: { value: "video_768p" },
    });
    fireEvent.change(screen.getByLabelText(/数量/), {
      target: { value: "10" },
    });
    fireEvent.click(screen.getByRole("button", { name: "试算" }));

    await waitFor(() =>
      expect(submitBillingQuote).toHaveBeenCalledWith({
        service: "video_768p",
        units: 10,
        userId: "cust-1",
      }),
    );

    const table = await screen.findByRole("table", { name: "试算结果" });
    expect(table).toHaveTextContent("8.5 折（专项折扣）");
    expect(table).toHaveTextContent("单位最终售价3 积分 / 秒");
    expect(table).toHaveTextContent("25 积分");
    expect(table).toHaveTextContent("¥0.25");
    expect(table).toHaveTextContent("¥0.13");
  });

  test("keeps unpriced costs honest instead of showing a made-up zero", async () => {
    vi.mocked(submitBillingQuote).mockResolvedValue({
      service: "oral",
      label: "数字人口播",
      units: "5",
      unit: "second",
      credits: "10",
      unit_credits: "2",
      discount_basis_points: null,
      discount_rate: null,
      discount_source: null,
      nominal_fen: "10",
      cost_fen: null,
      gross_fen: null,
    });

    render(<BillingQuoteCalculator />);
    fireEvent.change(await screen.findByPlaceholderText("用户名或公司名"), {
      target: { value: "" },
    });
    fireEvent.change(screen.getByLabelText("业务"), {
      target: { value: "oral" },
    });
    fireEvent.click(screen.getByRole("button", { name: "试算" }));

    const table = await screen.findByRole("table", { name: "试算结果" });
    expect(table).toHaveTextContent("未命中折扣");
    expect(table).toHaveTextContent("待核对（未配置成本单价）");
  });

  test("blocks a quote without a service or with a non-positive quantity", async () => {
    render(<BillingQuoteCalculator />);
    await screen.findByPlaceholderText("用户名或公司名");

    fireEvent.change(screen.getByLabelText(/数量/), { target: { value: "0" } });
    // jsdom 的约束校验会拦截无效表单的提交按钮点击；这里直接派发 submit
    // 事件来覆盖组件自身的兜底校验（浏览器里两层都会生效）。
    fireEvent.submit(
      screen
        .getByRole("button", { name: "试算" })
        .closest("form") as HTMLFormElement,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("请选择业务");
    expect(submitBillingQuote).not.toHaveBeenCalled();
  });
});
