import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ViralPlatformProbes } from "./ViralPlatformProbes";

afterEach(() => {
  vi.unstubAllGlobals();
  setAdminCsrfToken("");
});
it("探测先读最新费用，取消不调用供应商，确认带价格快照和写契约", async () => {
  let price = 1;
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url.includes("/operations/estimate"))
      return new Response(
        JSON.stringify({
          snapshot: String(price).repeat(64),
          unitCostFen: price,
          logicalCallsMax: 1,
          normalRetryCallsMax: 3,
          dataCostMaxFen: 3 * price,
          reusedVideos: 0,
          mediaDownloadsMax: 0,
          totalCostFen: null,
          note: "完整费用未知",
        }),
      );
    if (url.endsWith("/probes"))
      return new Response(JSON.stringify({ items: [] }));
    return new Response(
      JSON.stringify({
        platform: "douyin",
        state: "available",
        checked_at: new Date().toISOString(),
        failure_category: null,
        advice: null,
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("fake-probe-csrf");
  render(<ViralPlatformProbes />);
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "探测抖音" })).toBeEnabled(),
  );
  fireEvent.click(screen.getByRole("button", { name: "探测抖音" }));
  const first = await screen.findByRole("dialog", { name: "确认平台探测费用" });
  expect(first).toHaveTextContent("¥0.01");
  fireEvent.click(within(first).getByRole("button", { name: "取消" }));
  expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/probe"))).toBe(
    false,
  );
  price = 2;
  fireEvent.click(screen.getByRole("button", { name: "探测抖音" }));
  const second = await screen.findByRole("dialog");
  expect(second).toHaveTextContent("¥0.02");
  fireEvent.change(within(second).getByLabelText("操作原因"), {
    target: { value: "虚构通道验收" },
  });
  fireEvent.click(within(second).getByRole("button", { name: "确认探测" }));
  await screen.findByText(/本次探测可用/);
  const writes = fetchMock.mock.calls.filter(([url]) => url.endsWith("/probe"));
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0][1]?.body))).toMatchObject({
    confirm: true,
    reason: "虚构通道验收",
    expected_cost_snapshot: "2".repeat(64),
  });
  expect(writes[0][1]?.headers).toMatchObject({
    "X-Admin-CSRF": "fake-probe-csrf",
  });
});
it("只读不探测，已过期结果不显示可用", async () => {
  const fetchMock = vi.fn(
    async () =>
      new Response(
        JSON.stringify({
          items: [
            {
              platform: "douyin",
              state: "available",
              checked_at: new Date(Date.now() - 11 * 60000).toISOString(),
              failure_category: null,
              advice: null,
            },
          ],
        }),
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(<ViralPlatformProbes readOnly />);
  await screen.findByText(/探测已过期/);
  expect(screen.queryByRole("button", { name: "探测抖音" })).toBeNull();
  expect(fetchMock).toHaveBeenCalledTimes(1);
});
