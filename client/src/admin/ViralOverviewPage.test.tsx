import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ViralOverviewPage } from "./ViralOverviewPage";

afterEach(() => vi.unstubAllGlobals());
it("同批次漏斗不使用库存；未知收支和历史不伪装为零", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () =>
        url.includes("content-overview")
          ? {
              from: "2026-09-28",
              to: "2026-10-02",
              measurementStartedAt: "2026-10-01T19:00:00Z",
              cohortRule: "同一采集批次",
              historyNote: "更早历史无法还原",
              funnel: [
                {
                  stage: "collected",
                  name: "本期采集",
                  count: 2,
                  conversion: null,
                },
                {
                  stage: "prepared",
                  name: "素材就绪",
                  count: 1,
                  conversion: 0.5,
                },
              ],
              directCopyWithoutDetail: 0,
              finance: {
                knownRevenueFen: 100,
                unknownRevenueCount: 1,
                revenueFen: null,
                knownCostFen: 40,
                unknownCostCount: 1,
                costFen: null,
                grossFen: null,
                grossRate: null,
                countingRule: "不分摊",
              },
              topVideos: [],
              bestKeywords: [],
              worstKeywords: [],
              keywordRule: "起计以来",
            }
          : {
              content_total: 999,
              archive_ready: 888,
              homepage_featured: 777,
              pending_archive: 0,
              archive_failed: 0,
              collection_enabled: true,
            },
    })),
  );
  render(<ViralOverviewPage />);
  await screen.findByText("2 条");
  expect(screen.getByText("999 条")).toBeInTheDocument();
  expect(screen.getByText("转化 50.0%")).toBeInTheDocument();
  expect(screen.getAllByText("待核对")).toHaveLength(3);
  expect(screen.getByText(/更早历史无法还原/)).toBeInTheDocument();
  expect(screen.queryByText(/NaN/)).toBeNull();
  const href =
    screen.getByRole("link", { name: "2 条" }).getAttribute("href") ?? "";
  const listQuery =
    new URLSearchParams(href.split("?")[1]).get("listQuery") ?? "";
  expect(new URLSearchParams(listQuery).get("cohortStage")).toBe("collected");
  expect(new URLSearchParams(listQuery).get("collectedFrom")).toBe(
    "2026-09-28",
  );
  fireEvent.change(screen.getByLabelText("采集起始日期"), {
    target: { value: "2026-09-01" },
  });
  fireEvent.change(screen.getByLabelText("采集结束日期"), {
    target: { value: "2026-09-30" },
  });
  fireEvent.change(screen.getByLabelText("采集漏斗平台"), {
    target: { value: "wechat_channels" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查看区间" }));
  await waitFor(() =>
    expect(
      vi
        .mocked(fetch)
        .mock.calls.some(([url]) =>
          String(url).includes(
            "from=2026-09-01&to=2026-09-30&platform=wechat_channels",
          ),
        ),
    ).toBe(true),
  );
});
