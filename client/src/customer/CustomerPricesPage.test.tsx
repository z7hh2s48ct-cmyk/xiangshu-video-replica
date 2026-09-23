import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { customerGetConsumptionByBusiness, customerGetPricing } from "../api";
import { CustomerPricesPage } from "./CustomerPricesPage";

vi.mock("../api", () => ({
  customerGetConsumptionByBusiness: vi.fn(),
  customerGetPricing: vi.fn(),
}));

const pricingPayload = {
  version: 3,
  configured: true,
  config: { video_768p: 7, video_2k: 9, oral: 15, points_per_yuan: 100 },
  recharge_rounding: "向下取整",
  prices: [
    {
      subject: "video_768p",
      name: "视频生成",
      specification: "768P",
      unit: "秒",
      unit_credits: 7,
      configurable: true,
    },
  ],
};

const credential = async () => ({
  kind: "session" as const,
  token: crypto.randomUUID(),
});

afterEach(() => {
  vi.resetAllMocks();
});

test("loads server prices and retries failures without showing a fake price", async () => {
  vi.mocked(customerGetConsumptionByBusiness).mockResolvedValue({
    days: 30,
    total_credits: 0,
    items: [],
  });
  vi.mocked(customerGetPricing)
    .mockRejectedValueOnce(new Error("暂不可用"))
    .mockResolvedValueOnce(pricingPayload);
  render(<CustomerPricesPage credential={credential} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("暂不可用");
  expect(screen.queryByText("7 积分 / 秒")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "重新加载价格" }));
  await waitFor(() =>
    expect(screen.getByText("7 积分 / 秒")).toBeInTheDocument(),
  );
  expect(screen.getByText("1 元 = 100 积分")).toBeInTheDocument();
});

test("消费构成读取失败要说出失败并可单独重试，不停在「正在读取消费构成…」（评审 #12）", async () => {
  vi.mocked(customerGetPricing).mockResolvedValue(pricingPayload);
  vi.mocked(customerGetConsumptionByBusiness)
    .mockRejectedValueOnce(new Error("构成读取失败"))
    .mockResolvedValueOnce({
      days: 30,
      total_credits: 100,
      items: [{ business: "video", credits: 100 }],
    });
  render(<CustomerPricesPage credential={credential} />);

  // 失败必须说出来：此前 catch 里只是 setConsumption(null)，界面永久停在加载文案上。
  expect(await screen.findByText("构成读取失败")).toBeVisible();
  expect(screen.queryByText("正在读取消费构成…")).toBeNull();

  fireEvent.click(screen.getByRole("button", { name: "重新加载消费构成" }));
  expect(
    await screen.findByRole("img", { name: /最近 30 天消费构成/ }),
  ).toBeVisible();
  expect(screen.queryByText("构成读取失败")).toBeNull();
  // 构成自己的重试不该把已经拿到的价目表拉回全页 loading。
  expect(screen.getByText("7 积分 / 秒")).toBeInTheDocument();
});
