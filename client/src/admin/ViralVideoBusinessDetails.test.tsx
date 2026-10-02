import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import type { CollectedViralVideo } from "../api.admin";
import { ViralResourceCosts } from "./ViralResourceCosts";
import { ViralVideoBusinessDetails } from "./ViralVideoBusinessDetails";

afterEach(() => vi.unstubAllGlobals());

it("资源事件保持未知，账单核对精确金额且未知结果重试沿用原原因与幂等键；审计员只读", async () => {
  const record = {
    id: "resource-opaque",
    platform: "douyin",
    video_id: "opaque/video=id",
    label: "素材下载",
    kind: "download",
    unit: "byte",
    quantity: 128,
    state: "SUCCEEDED",
    created_at: "2026-10-02T00:00:00Z",
    cost_fen: null,
    evidence: null,
  };
  const data = {
    records: [record],
    total: 1,
    offset: 0,
    limit: 25,
    components: [],
    knownCostFen: 0,
    costFen: null,
    coverageComplete: false,
    missingEvidence: ["云端实际流量缺证据"],
    countingRule: "不分摊",
  };
  const writes: Array<{ body: Record<string, unknown>; key: string | null }> =
    [];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      writes.push({
        body: JSON.parse(String(init.body)),
        key: new Headers(init.headers).get("Idempotency-Key"),
      });
      if (writes.length === 1) throw new Error("模拟未知网络结果");
      return new Response(JSON.stringify({ id: "evidence-1" }));
    }
    return new Response(JSON.stringify(data));
  });
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("fake-resource-csrf");
  const view = render(
    <ViralResourceCosts platform="douyin" videoId="opaque/video=id" />,
  );
  await screen.findByText("128 字节", { exact: false });
  expect(screen.getByText(/完整成本待核对/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "核对账单" }));
  fireEvent.change(screen.getByLabelText("账单金额（元）"), {
    target: { value: "0.123456" },
  });
  fireEvent.change(screen.getByLabelText("账单编号与明细行"), {
    target: { value: "FAKE-BILL-1:line-3" },
  });
  fireEvent.change(screen.getByLabelText(/操作原因/), {
    target: { value: "核对实际下载明细" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存账单证据" }));
  await screen.findByText(/模拟未知网络结果/);
  fireEvent.change(screen.getByLabelText(/操作原因/), {
    target: { value: "改变表单原因不能改已发请求" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存账单证据" }));
  await vi.waitFor(() => expect(writes).toHaveLength(2));
  expect(writes[1]).toEqual(writes[0]);
  expect(writes[0].body.cost_fen).toBe("12.3456");
  expect(writes[0].body.reason).toBe("核对实际下载明细");
  view.unmount();
  render(
    <ViralResourceCosts readOnly platform="douyin" videoId="opaque/video=id" />,
  );
  await screen.findByText("128 字节", { exact: false });
  expect(screen.queryByRole("button", { name: "核对账单" })).toBeNull();
});
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
