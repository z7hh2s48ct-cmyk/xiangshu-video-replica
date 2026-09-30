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
      discount_source: "customer_discount:1",
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
    expect(table).toHaveTextContent("8.5 折（客户权益）");
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
      screen.getByRole("button", { name: "试算" }).closest("form") as HTMLFormElement,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("请选择业务");
    expect(submitBillingQuote).not.toHaveBeenCalled();
  });
});
