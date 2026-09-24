import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ViralCollectionBilling } from "./ViralCollectionBilling";
import { ViralVideosPage } from "./ViralVideosPage";

const video = {
  platform: "wechat_channels",
  video_id: "opaque/video=id",
  title: "庭院施工案例",
  author: "作者甲",
  category: "施工",
  duration_ms: 37000,
  likes: 500,
  comments: 4,
  shares: 2,
  collects: 1,
  published_at: 1788700000,
  created_at: "2026-09-13T08:00:00Z",
  homepage_featured: false,
  collection_published: true,
  media_status: "SUCCEEDED",
  storage_uri: "cos://archive/video.mp4",
};

function setup() {
  let deleted = false;
  let featured = false;
  const fetchMock = vi.fn(
    async (
      _url: string,
      init?: RequestInit,
    ): Promise<{
      ok: boolean;
      status: number;
      json: () => Promise<unknown>;
    }> => {
      if (init?.method === "PATCH") {
        const payload = JSON.parse(String(init.body));
        featured = payload.action === "feature";
        deleted = payload.action === "delete";
      }
      return {
        ok: true,
        status: 200,
        json: async () => ({
          items: deleted ? [] : [{ ...video, homepage_featured: featured }],
          total: deleted ? 0 : 1,
        }),
      };
    },
  );
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("csrf-curation-test");
  return fetchMock;
}

describe("ViralVideosPage", () => {
  it("转存失败在确认框内显示原因，重试保留原幂等键", async () => {
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => ({
      ok: init?.method !== "POST",
      status: init?.method === "POST" ? 409 : 200,
      json: async () =>
        init?.method === "POST"
          ? {
              detail: {
                code: "VIRAL_ARCHIVE_BUSY",
                message: "该平台已有后台任务，请完成后再转存。",
              },
            }
          : {
              items: [
                { ...video, media_status: "NOT_STARTED", storage_uri: null },
              ],
              total: 1,
            },
    }));
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-curation-test");
    render(<ViralVideosPage />);
    fireEvent.click(await screen.findByRole("button", { name: "转存到云端" }));
    fireEvent.change(screen.getByLabelText(/操作原因/), {
      target: { value: "验证失败重试" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    const dialog = screen.getByRole("dialog", { name: "转存单条视频" });
    await waitFor(() =>
      expect(dialog).toHaveTextContent("该平台已有后台任务，请完成后再转存。"),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
      ).toHaveLength(2),
    );
    const posts = fetchMock.mock.calls.filter(
      ([, init]) => init?.method === "POST",
    );
    expect(posts[0][1]?.headers).toEqual(posts[1][1]?.headers);
    expect(screen.getByLabelText(/操作原因/)).toHaveValue("验证失败重试");
  });

  it("单条转存携带写入合同并显示后台排队，不能重复点击", async () => {
    let queued = false;
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") queued = true;
      return {
        ok: true,
        status: queued ? 202 : 200,
        json: async () => ({
          items: [
            {
              ...video,
              media_status: "NOT_STARTED",
              storage_uri: null,
              archive_status: queued ? "PENDING" : null,
            },
          ],
          total: 1,
        }),
      };
    });
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-curation-test");
    render(<ViralVideosPage />);
    fireEvent.click(await screen.findByRole("button", { name: "转存到云端" }));
    fireEvent.change(screen.getByLabelText(/操作原因/), {
      target: { value: "单条转存验收" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    expect(
      await screen.findByRole("button", { name: "后台转存中" }),
    ).toBeDisabled();
    expect(screen.getByText("转存排队中")).toBeInTheDocument();
    const post = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(post?.[0]).toContain("opaque%2Fvideo%3Did/archive");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      reason: "单条转存验收",
      confirm: true,
    });
    expect(post?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-curation-test",
      "Idempotency-Key": expect.any(String),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });
  it("采集账单区分接口次数、客户扣费笔数和余额不足记录", async () => {
    const fetchMock = vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () =>
        url.includes("/charges?")
          ? {
              items: [
                {
                  request_id: "request-1",
                  user_id: "customer-1",
                  username: "客户甲",
                  state: "INSUFFICIENT_CREDITS",
                  due_credits: 3,
                  charged_credits: 0,
                },
              ],
              total: 1,
            }
          : {
              items: [
                {
                  id: "batch-1",
                  platform: "douyin",
                  created_at: "2026-09-13T08:00:00Z",
                  config: { keywords: [{ keyword: "庭院" }] },
                  pricing: { credits: 3, version: 1 },
                  request_count: 4,
                  confirmed_count: 3,
                  uncertain_count: 1,
                  pending_requests: 0,
                  customer_count: 2,
                  pending_charges: 0,
                  failed_count: 1,
                  charged_credits: 15,
                  known_cost_fen: "0.000125",
                  known_revenue_fen: "15",
                  profit_fen: null,
                  unknown_cost_count: 1,
                  unknown_revenue_count: 0,
                },
              ],
              total: 1,
            },
    }));
    vi.stubGlobal("fetch", fetchMock);
    render(
      <ViralCollectionBilling query="start=2026-09-01&end=2026-09-30&user_id=other" />,
    );
    expect(
      await screen.findByText(/登记 4 次；已确认 3 次/),
    ).toBeInTheDocument();
    expect(screen.getByText(/失败 1 笔；待扣 0 笔/)).toBeInTheDocument();
    expect(screen.getByText(/已知成本 ¥0.00000125/)).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).not.toContain("user_id=other");
    fireEvent.click(screen.getByRole("button", { name: "客户扣费明细" }));
    expect(await screen.findByText("客户甲")).toBeInTheDocument();
    expect(screen.getByText("余额不足，扣费失败")).toBeInTheDocument();
    expect(screen.getByText("3 / 0 积分")).toBeInTheDocument();
  });
  it("先显示采集数据，人工确认后才展示首页，并能删除", async () => {
    const fetchMock = setup();
    render(<ViralVideosPage />);
    expect(await screen.findByText("庭院施工案例")).toBeInTheDocument();
    expect(screen.getByText("未展示")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "展示到首页" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "人工筛选通过" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    await screen.findByText("首页展示设置已更新。");
    await screen.findByRole("button", { name: "取消首页展示" });
    const patch = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(patch?.[0]).toContain("opaque%2Fvideo%3Did/curation");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      action: "feature",
      confirm: true,
      reason: "人工筛选通过",
    });
    expect(patch?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-curation-test",
    });
    fireEvent.click(screen.getByRole("button", { name: "查看详情" }));
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "不适合当前选题" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    await screen.findByText("视频已删除，前台不再展示。");
    await waitFor(() =>
      expect(screen.queryByText("庭院施工案例")).not.toBeInTheDocument(),
    );
  });
  it("只读账号可以查看数据，不能设置首页或删除", async () => {
    setup();
    render(<ViralVideosPage readOnly />);
    await screen.findByText("庭院施工案例");
    expect(
      screen.queryByRole("button", { name: "展示到首页" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "删除" }),
    ).not.toBeInTheDocument();
  });

  it("列表收起技术明细，展开可查看完整信息且不会自动请求预览", async () => {
    const fetchMock = setup();
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    expect(screen.queryByText(video.video_id)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "删除" }),
    ).not.toBeInTheDocument();
    const details = screen.getByRole("button", { name: "查看详情" });
    expect(details).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(details);
    expect(screen.getByText(video.video_id)).toBeInTheDocument();
    expect(screen.getByText(video.storage_uri)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "收起详情" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "收起详情" }));
    expect(screen.queryByText(video.video_id)).not.toBeInTheDocument();
  });

  it("仅点击补齐本页互动才调用写接口，保留真实零值和未提供字段", async () => {
    const fetchMock = setup();
    fetchMock.mockImplementation(async (_url, init) => ({
      ok: true,
      status: 200,
      json: async () =>
        init?.method === "POST"
          ? {
              likes: 276,
              comments: 0,
              shares: null,
              collects: 279,
              statistics_checked_at: "2026-09-15T00:00:00Z",
              statistics_retry_at: null,
              statistics_status: "partial",
            }
          : {
              items: [
                { ...video, comments: null, shares: null, collects: null },
              ],
              total: 1,
            },
    }));
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "补齐本页互动" }));
    expect(await screen.findByText(/接口部分提供 1 条/)).toBeInTheDocument();
    expect(screen.getByText("276")).toBeInTheDocument();
    expect(screen.getByText("评论").closest("div")).toHaveTextContent("评论0");
    expect(screen.getByText("未提供")).toBeInTheDocument();
    const write = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(write?.[0]).toContain("opaque%2Fvideo%3Did/statistics");
    expect(write?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-curation-test",
    });
    expect(JSON.parse(String(write?.[1]?.body))).toMatchObject({
      confirm: true,
    });
  });
});
