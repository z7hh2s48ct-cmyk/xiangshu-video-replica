import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { CustomerPricing } from "../api";
import {
  adminRead,
  getCustomerPricing,
  updateCustomerPricing,
} from "../api.admin";
import { CustomerPricingManager } from "./CustomerPricingManager";

vi.mock("../api.admin", () => ({
  adminRead: vi.fn(),
  getCustomerPricing: vi.fn(),
  updateCustomerPricing: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

const payload: CustomerPricing = {
  version: 2,
  configured: true,
  config: {
    video_768p: 3,
    video_2k: 7,
    oral: 11,
    points_per_yuan: 100,
    discount_basis_points: 9500,
    consumption_rounding: "floor",
  },
  prices: [],
  recharge_rounding: "向下取整",
};

test("shows and saves exchange, discount and consumption rounding without publishing feature tariffs", async () => {
  vi.mocked(getCustomerPricing).mockResolvedValue(payload);
  vi.mocked(updateCustomerPricing).mockResolvedValue({
    ...payload,
    version: 3,
  });
  render(<CustomerPricingManager />);
  await waitFor(() =>
    expect(screen.getByLabelText("每 1 元充值获得积分")).toHaveValue(100),
  );
  expect(screen.getByLabelText("全科目折扣（%）")).toHaveValue(95);
  expect(screen.getByLabelText("消费取整方式")).toHaveValue("floor");
  fireEvent.change(screen.getByLabelText("每 1 元充值获得积分"), {
    target: { value: "5" },
  });
  fireEvent.change(screen.getByLabelText("全科目折扣（%）"), {
    target: { value: "87.5" },
  });
  fireEvent.change(screen.getByLabelText("消费取整方式"), {
    target: { value: "ceil" },
  });
  expect(screen.queryByLabelText("调整原因")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "保存价格配置" }));
  await waitFor(() =>
    expect(updateCustomerPricing).toHaveBeenCalledWith(
      {
        points_per_yuan: 5,
        discount_basis_points: 8750,
        consumption_rounding: "ceil",
      },
      2,
      "更新客户报价配置（换算/折扣/取整）",
      expect.any(String),
    ),
  );
});

test("rejects discount percentages outside 0.01-100 before saving", async () => {
  vi.mocked(getCustomerPricing).mockResolvedValue(payload);
  render(<CustomerPricingManager />);
  await waitFor(() =>
    expect(screen.getByLabelText("全科目折扣（%）")).toHaveValue(95),
  );
  // 直接派发 submit：浏览器原生校验（min/step）会先拦住非法值，这里验证 JS 兜底层。
  const form = screen
    .getByRole("button", { name: "保存价格配置" })
    .closest("form") as HTMLFormElement;

  fireEvent.change(screen.getByLabelText("全科目折扣（%）"), {
    target: { value: "0" },
  });
  fireEvent.submit(form);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "折扣请输入 0.01–100 的百分比",
  );
  expect(updateCustomerPricing).not.toHaveBeenCalled();

  fireEvent.change(screen.getByLabelText("全科目折扣（%）"), {
    target: { value: "100.001" },
  });
  fireEvent.submit(form);
  expect(updateCustomerPricing).not.toHaveBeenCalled();
});

test("states that per-customer discounts are not wired into billing yet", async () => {
  vi.mocked(getCustomerPricing).mockResolvedValue(payload);
  render(<CustomerPricingManager />);
  expect(
    await screen.findByText(/按客户单独定制的折扣.*尚未接入扣费链路/),
  ).toBeInTheDocument();
});

const pricingHistory = {
  current_version: 3,
  items: [
    {
      version: 3,
      config: {
        points_per_yuan: 100,
        discount_basis_points: 9500,
        consumption_rounding: "floor",
      },
      source: "audit",
      current: true,
      actor_user_id: "u-1",
      actor_username: "price_admin",
      reason: "下调活动折扣",
      effective_at: "2026-09-21T02:00:00+00:00",
    },
    {
      version: 2,
      config: null,
      source: "current",
      current: false,
      actor_user_id: null,
      actor_username: null,
      reason: null,
      effective_at: null,
    },
  ],
};

test("configuration history lists versions with operator, reason and snapshot", async () => {
  vi.mocked(getCustomerPricing).mockResolvedValue(payload);
  vi.mocked(adminRead).mockResolvedValue(pricingHistory);
  render(<CustomerPricingManager />);
  await waitFor(() =>
    expect(screen.getByLabelText("每 1 元充值获得积分")).toHaveValue(100),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看配置历史" }));
  expect(adminRead).toHaveBeenCalledWith(
    "/api/control/billing/pricing-history",
    "读取报价配置历史失败",
  );
  const table = await screen.findByRole("table", { name: "报价配置版本历史" });
  const rows = within(table).getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  expect(within(rows[0]).getByText("V3")).toBeInTheDocument();
  expect(within(rows[0]).getByText("当前")).toBeInTheDocument();
  expect(within(rows[0]).getByText("price_admin")).toBeInTheDocument();
  expect(within(rows[0]).getByText("下调活动折扣")).toBeInTheDocument();
  expect(within(rows[0]).getByText("95%")).toBeInTheDocument();
  expect(within(rows[0]).getByText("向下取整")).toBeInTheDocument();
  expect(within(rows[0]).getByText("审计记录")).toBeInTheDocument();
  expect(within(rows[1]).getByText("当前行（无审计）")).toBeInTheDocument();
  expect(within(rows[1]).getAllByText("—").length).toBeGreaterThanOrEqual(4);
});

test("configuration history failures surface an alert", async () => {
  vi.mocked(getCustomerPricing).mockResolvedValue(payload);
  vi.mocked(adminRead).mockRejectedValue(
    new Error("读取报价配置历史失败：服务暂不可用（503）"),
  );
  render(<CustomerPricingManager />);
  await waitFor(() =>
    expect(screen.getByLabelText("每 1 元充值获得积分")).toHaveValue(100),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看配置历史" }));
  expect(await screen.findByText(/服务暂不可用/)).toBeInTheDocument();
});
