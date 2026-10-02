import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ViralDiscoveriesPage } from "./ViralDiscoveriesPage";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
it("默认近7天，零结果客户可下钻且子账号详情定位所属主客户", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-01T23:00:00Z"));
  const fetchMock = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    json: async () =>
      url.includes("/customers?")
        ? {
            total: 1,
            countingRule: "真实搜索账号",
            items: [
              {
                user_id: "sub-1",
                customer_user_id: "master-1",
                username: "子账号乙",
                display_name: "庭院公司",
                searches: 2,
                zero_results: 2,
                videos: 0,
                last_searched_at: "2026-10-01T23:00:00Z",
              },
            ],
          }
        : {
            date: "2026-09-26~2026-10-02",
            from: "2026-09-26",
            to: "2026-10-02",
            total: 2,
            users: 1,
            videos: 0,
            keywords: [
              {
                keyword: "零结果需求",
                platform: "douyin",
                searches: 2,
                zero_results: 2,
                users: 1,
                videos: 0,
                discoveries: 0,
              },
            ],
          },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const onCustomer = vi.fn();
  render(<ViralDiscoveriesPage readOnly onCustomer={onCustomer} />);
  fireEvent.click(await screen.findByRole("button", { name: "查看搜索客户" }));
  expect(
    await screen.findByRole("cell", { name: /子账号乙/ }),
  ).toBeInTheDocument();
  expect(screen.getByText(/子账号，详情打开所属主客户/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "查看客户详情" }));
  expect(onCustomer).toHaveBeenCalledWith("master-1");
  await waitFor(() =>
    expect(fetchMock.mock.calls[0][0]).toContain(
      "from=2026-09-26&to=2026-10-02",
    ),
  );
  const customers =
    fetchMock.mock.calls.find(([url]) => url.includes("/customers?"))?.[0] ||
    "";
  expect(customers).toContain("from=2026-09-26");
  expect(customers).toContain("to=2026-10-02");
  expect(customers).toContain("platform=douyin");
  expect(customers).toContain("offset=0");
});
