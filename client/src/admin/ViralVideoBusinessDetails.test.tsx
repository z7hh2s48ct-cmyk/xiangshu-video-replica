import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { CollectedViralVideo } from "../api.admin";
import { ViralVideoBusinessDetails } from "./ViralVideoBusinessDetails";

afterEach(() => vi.unstubAllGlobals());
it("收入、已知接口成本与未知总成本分开，跨页客户按钱包稳定ID跳转", async () => {
  const video = {
    platform: "douyin",
    video_id: "opaque/video=id",
    title: "测试视频",
  } as CollectedViralVideo;
  const customer = (id: string) => ({
    walletOwnerId: id,
    name: "同名客户",
    detailAuthorized: true,
    copyAuthorized: false,
    chargedCredits: 1,
    revenueFen: 10,
    knownRevenueFen: 10,
    unknownRevenueOperations: 0,
    lastUsedAt: "2026-10-02T00:00:00Z",
    actors: [{ id: "sub-actor", name: "操作子账号" }],
  });
  const fetchMock = vi.fn(
    async (url: string) =>
      new Response(
        JSON.stringify({
          video,
          media: {
            video: { ready: true, status: "SUCCEEDED" },
            audio: { ready: false, status: "PENDING" },
          },
          copy: null,
          sourceDescription: null,
          originalUrl: null,
          sourceKeywords: ["来源词"],
          relatedVideos: [],
          business: {
            window: "全部已记录历史",
            countingRule: "按钱包主体",
            detailAccounts: 21,
            copyAccounts: 0,
            favoriteAccounts: 0,
            chargedCredits: 21,
            revenueFen: 210,
            knownRevenueFen: 210,
            unknownRevenueOperations: 0,
            collectionCostFen: null,
            attributedDataCalls: 2,
            knownDataCostFen: 2,
            unknownDataCostCalls: 1,
            pendingDataCostCalls: 0,
            costNote: "共享搜索不平摊，历史/存储/流量未知",
            customerTotal: 21,
            offset: url.includes("offset=20") ? 20 : 0,
            limit: 20,
            customers: [
              customer(
                url.includes("offset=20") ? "wallet-ab" : "wallet-a/opaque",
              ),
            ],
          },
        }),
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  const onCustomer = vi.fn();
  render(
    <ViralVideoBusinessDetails
      video={video}
      onRelated={vi.fn()}
      onCustomer={onCustomer}
    />,
  );
  await screen.findByText("¥2.10");
  expect(screen.getByText(/已知接口成本/)).toHaveTextContent("¥0.02");
  expect(screen.getByText("待核对")).toBeInTheDocument();
  fireEvent.click(screen.getByText(/客户使用明细/));
  fireEvent.click(screen.getByRole("button", { name: "查看客户" }));
  expect(onCustomer).toHaveBeenLastCalledWith("wallet-a/opaque");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByText("同名客户");
  await vi.waitFor(() =>
    expect(
      fetchMock.mock.calls.some(([url]) => url.includes("offset=20")),
    ).toBe(true),
  );
  fireEvent.click(screen.getByRole("button", { name: "查看客户" }));
  expect(onCustomer).toHaveBeenLastCalledWith("wallet-ab");
});
