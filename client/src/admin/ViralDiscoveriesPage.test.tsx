import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ViralDiscoveriesPage } from "./ViralDiscoveriesPage";

const detailVideo = {
  platform: "douyin",
  video_id: "search-hit-1",
  title: "用户搜到的爆款",
  author: "作者甲",
  category: "施工",
  duration_ms: 30_000,
  likes: 900,
  comments: 12,
  shares: 3,
  collects: 8,
  published_at: 1_788_700_000,
  created_at: "2026-09-22T08:00:00Z",
  homepage_featured: false,
  collection_published: true,
  cover_key: "viral/covers/douyin/search-hit-1",
  cover_required: true,
  media_status: "SUCCEEDED",
  storage_uri: "cos://archive/video.mp4",
};

function jsonResponse(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

function setupAggregate() {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.includes("/api/control/viral/discoveries/detail")) {
      return jsonResponse({ date: "2026-09-22", total: 0, items: [] });
    }
    return jsonResponse({
      date: "2026-09-22",
      total: 5,
      users: 3,
      videos: 2,
      keywords: [
        {
          keyword: "农村建房",
          platform: "douyin",
          discoveries: 4,
          users: 3,
          videos: 2,
        },
      ],
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("csrf-discoveries-test");
  return fetchMock;
}

describe("ViralDiscoveriesPage", () => {
  it("展示可用库存和真实每日趋势，选择分类加入采集并导航实时补货", async () => {
    setAdminCsrfToken("csrf-demand");
    const fetchMock = vi.fn(async (_url: string, init?: RequestInit) =>
      jsonResponse(
        init?.method === "POST"
          ? { added: 1, duplicates: 0, total: 1 }
          : {
              date: "2026-09-22",
              total: 4,
              users: 2,
              videos: 3,
              categories: ["推荐", "庭院案例"],
              inventoryRule: "仅当前客户可用视频",
              priorityRule: "按搜索库存压力排序",
              keywords: [
                {
                  keyword: "缺货需求词",
                  platform: "wechat_channels",
                  searches: 4,
                  zero_results: 1,
                  discoveries: 3,
                  users: 2,
                  videos: 3,
                  inventory: 0,
                  configured: false,
                  collectionEnabled: null,
                  trend: [
                    { date: "2026-09-21", searches: null, partial: false },
                    { date: "2026-09-22", searches: 4, partial: true },
                  ],
                },
              ],
            },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const onSearch = vi.fn();
    render(<ViralDiscoveriesPage onSearch={onSearch} />);
    expect(await screen.findByText("0 条可用")).toBeInTheDocument();
    expect(screen.getByText("尚未配置采集")).toBeInTheDocument();
    expect(screen.getByText("2026-09-21：历史未知")).toBeInTheDocument();
    expect(
      screen.getByText("2026-09-22：4 页次（开始记录当天，未覆盖全天）"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "实时搜索补货" }));
    expect(onSearch).toHaveBeenCalledWith("缺货需求词", "wechat_channels");
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "加入采集" }));
    fireEvent.change(screen.getByLabelText("采集分类"), {
      target: { value: "庭院案例" },
    });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "优先补齐客户需求" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    await screen.findByText("关键词已加入定时采集，下一轮生效。");
    const saved = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(JSON.parse(String(saved?.[1]?.body))).toEqual({
      keywords: [
        {
          platform: "wechat_channels",
          keyword: "缺货需求词",
          category: "庭院案例",
          enabled: true,
          limit: null,
        },
      ],
      confirm: true,
      reason: "优先补齐客户需求",
    });
  });

  it.each([
    ["7d", "30d"],
    ["30d", "custom"],
  ])("%s切换%s使旧明细响应失效", async (first, next) => {
    let resolveOld: (value: ReturnType<typeof jsonResponse>) => void = () => {};
    const fetchMock = vi.fn((url: string) => {
      if (url.includes("/detail"))
        return new Promise<ReturnType<typeof jsonResponse>>((resolve) => {
          resolveOld = resolve;
        });
      const params = new URL(url, "http://localhost").searchParams;
      return Promise.resolve(
        jsonResponse({
          from: params.get("from") ?? params.get("date"),
          to: params.get("to") ?? params.get("date"),
          total: 1,
          users: 1,
          videos: 1,
          keywords: [
            {
              keyword: "旧窗口词",
              platform: "douyin",
              searches: 1,
              discoveries: 1,
              users: 1,
              videos: 1,
            },
          ],
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralDiscoveriesPage />);
    fireEvent.change(screen.getByLabelText("时间范围"), {
      target: { value: first },
    });
    fireEvent.click(await screen.findByRole("button", { name: "查看明细" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => url.includes("/detail")),
      ).toBe(true),
    );
    fireEvent.change(screen.getByLabelText("时间范围"), {
      target: { value: next },
    });
    await act(async () =>
      resolveOld(
        jsonResponse({
          total: 1,
          items: [
            {
              keyword: "旧窗口词",
              platform: "douyin",
              videoId: "old",
              discoveries: 1,
              users: 1,
              lastSearchedAt: "2026-09-22",
              video: { ...detailVideo, title: "旧窗口错误回写" },
            },
          ],
        }),
      ),
    );
    expect(screen.queryByText("旧窗口错误回写")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "搜索发现明细" }),
    ).not.toBeInTheDocument();
  });
  it("快速换关键词与翻页中切窗口不会被迟到响应覆盖", async () => {
    const pending: Array<(value: ReturnType<typeof jsonResponse>) => void> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/detail"))
          return new Promise<ReturnType<typeof jsonResponse>>((resolve) =>
            pending.push(resolve),
          );
        return Promise.resolve(
          jsonResponse({
            from: "2026-09-20",
            to: "2026-09-26",
            total: 2,
            users: 1,
            videos: 1,
            keywords: ["词甲", "词乙"].map((keyword) => ({
              keyword,
              platform: "douyin",
              searches: 1,
              discoveries: 1,
              users: 1,
              videos: 1,
            })),
          }),
        );
      }),
    );
    render(<ViralDiscoveriesPage />);
    await screen.findByText("词甲");
    fireEvent.click(screen.getAllByRole("button", { name: "查看明细" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "查看明细" })[1]);
    const result = (title: string) =>
      jsonResponse({
        total: 40,
        items: [
          {
            keyword: title,
            platform: "douyin",
            videoId: title,
            discoveries: 1,
            users: 1,
            lastSearchedAt: "2026-09-22",
            video: { ...detailVideo, title },
          },
        ],
      });
    await act(async () => pending[1](result("最新词乙视频")));
    await act(async () => pending[0](result("迟到词甲视频")));
    expect(screen.queryByText("迟到词甲视频")).not.toBeInTheDocument();
    expect(screen.getByText("最新词乙视频")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    fireEvent.change(screen.getByLabelText("时间范围"), {
      target: { value: "30d" },
    });
    await act(async () => pending[2](result("旧窗口下一页")));
    expect(screen.queryByText("旧窗口下一页")).not.toBeInTheDocument();
  });
  it("近七天聚合与下钻使用同一窗口，零结果真实计数与历史未知分开", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/detail")) return jsonResponse({ total: 0, items: [] });
      return jsonResponse({
        date: "2026-09-24~2026-09-30",
        from: "2026-09-24",
        to: "2026-09-30",
        total: 3,
        users: 1,
        videos: 0,
        keywords: [
          {
            keyword: "空结果词",
            platform: "douyin",
            discoveries: 0,
            searches: 3,
            zero_results: 3,
            users: 1,
            videos: 0,
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralDiscoveriesPage />);
    fireEvent.change(screen.getByLabelText("时间范围"), {
      target: { value: "7d" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "查看明细" }));
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) =>
        url.includes("/detail"),
      );
      expect(call?.[0]).toContain("from=2026-09-24&to=2026-09-30");
      expect(call?.[0]).not.toContain("date=");
    });
    expect(
      screen.getByRole("columnheader", { name: "零结果页次" }),
    ).toBeInTheDocument();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("读取每日聚合并按词下钻明细，明细行带准备与上首页操作", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/api/control/viral/discoveries/detail")) {
        expect(url).toContain("keyword=%E5%86%9C%E6%9D%91%E5%BB%BA%E6%88%BF");
        return jsonResponse({
          date: "2026-09-22",
          total: 1,
          items: [
            {
              keyword: "农村建房",
              platform: "douyin",
              videoId: "search-hit-1",
              discoveries: 4,
              users: 3,
              lastSearchedAt: "2026-09-22T09:30:00+00:00",
              video: detailVideo,
            },
          ],
        });
      }
      // 聚合与明细都是管理端只读请求（fetch 缺省 GET）。
      expect(init?.method).toBeUndefined();
      return jsonResponse({
        date: "2026-09-22",
        total: 5,
        users: 3,
        videos: 2,
        keywords: [
          {
            keyword: "农村建房",
            platform: "douyin",
            discoveries: 4,
            users: 3,
            videos: 2,
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-discoveries-test");
    render(<ViralDiscoveriesPage />);

    expect(await screen.findByText("农村建房")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看明细" }));

    expect(await screen.findByText("用户搜到的爆款")).toBeInTheDocument();
    expect(screen.getByText("覆盖 3 个用户")).toBeInTheDocument();
    // 已归档视频可以直接上首页。
    expect(screen.getByRole("button", { name: "展示到首页" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "准备到云端" })).toBeNull();
  });

  it("已删除视频只保留发现记录，不提供任何操作", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/api/control/viral/discoveries/detail")) {
        return jsonResponse({
          date: "2026-09-22",
          total: 1,
          items: [
            {
              keyword: "农村建房",
              platform: "douyin",
              videoId: "deleted-hit",
              discoveries: 2,
              users: 2,
              lastSearchedAt: "2026-09-22T09:30:00+00:00",
              video: null,
            },
          ],
        });
      }
      return jsonResponse({
        date: "2026-09-22",
        total: 2,
        users: 2,
        videos: 1,
        keywords: [
          {
            keyword: "农村建房",
            platform: "douyin",
            discoveries: 2,
            users: 2,
            videos: 1,
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-discoveries-test");
    render(<ViralDiscoveriesPage />);

    fireEvent.click(await screen.findByRole("button", { name: "查看明细" }));

    expect(await screen.findByText("视频已不在内容池")).toBeInTheDocument();
    expect(screen.getByText("已被删除，仅保留记录")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "展示到首页" })).toBeNull();
  });

  it("未归档视频禁止上首页，先准备走写契约", async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/api/control/viral/discoveries/detail")) {
        return jsonResponse({
          date: "2026-09-22",
          total: 1,
          items: [
            {
              keyword: "农村建房",
              platform: "douyin",
              videoId: "search-hit-2",
              discoveries: 1,
              users: 1,
              lastSearchedAt: "2026-09-22T09:30:00+00:00",
              video: {
                ...detailVideo,
                video_id: "search-hit-2",
                media_status: "NOT_STARTED",
                storage_uri: null,
              },
            },
          ],
        });
      }
      if (init?.method === "POST") {
        if (url.includes("/operations/estimate"))
          return jsonResponse({
            snapshot: "confirmed-discovery-cost",
            logicalCallsMax: 1,
            normalRetryCallsMax: 3,
            unitCostFen: 10,
            dataCostMaxFen: 30,
            mediaDownloadsMax: 1,
            reusedVideos: 0,
            note: "最多3次调用；存储流量未知",
          });
        return jsonResponse({ task_id: "task-1", queued: true }, 202);
      }
      return jsonResponse({
        date: "2026-09-22",
        total: 1,
        users: 1,
        videos: 1,
        keywords: [
          {
            keyword: "农村建房",
            platform: "douyin",
            discoveries: 1,
            users: 1,
            videos: 1,
          },
        ],
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-discoveries-test");
    render(<ViralDiscoveriesPage />);

    fireEvent.click(await screen.findByRole("button", { name: "查看明细" }));
    const feature = await screen.findByRole("button", {
      name: "展示到首页",
    });
    expect(feature).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "准备到云端" }));
    expect(await screen.findByText("¥0.3000")).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(
        ([url, init]) =>
          init?.method === "POST" && !url.includes("/operations/estimate"),
      ),
    ).toHaveLength(0);
    fireEvent.change(await screen.findByLabelText(/操作原因/), {
      target: { value: "搜索发现上首页" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    await waitFor(() => {
      const write = fetchMock.mock.calls.find(
        ([url, init]) =>
          init?.method === "POST" && !url.includes("/operations/estimate"),
      );
      expect(write?.[0]).toContain(
        "/api/control/viral/videos/douyin/search-hit-2/archive",
      );
      expect(JSON.parse(String(write?.[1]?.body))).toMatchObject({
        confirm: true,
        reason: "搜索发现上首页",
        expected_cost_snapshot: "confirmed-discovery-cost",
      });
    });
  });

  it("setupAggregate 基线：聚合数据可渲染", async () => {
    setupAggregate();
    render(<ViralDiscoveriesPage />);
    await screen.findByText("农村建房");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "查看明细" })).toBeEnabled(),
    );
  });
});
