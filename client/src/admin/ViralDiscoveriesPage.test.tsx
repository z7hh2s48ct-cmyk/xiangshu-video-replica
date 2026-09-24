import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("读取每日聚合并按词下钻明细，明细行带转存与上首页操作", async () => {
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
    expect(screen.queryByRole("button", { name: "转存到云端" })).toBeNull();
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

  it("未归档视频禁止上首页，先转存走写契约", async () => {
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
    fireEvent.click(screen.getByRole("button", { name: "转存到云端" }));
    fireEvent.change(screen.getByLabelText(/操作原因/), {
      target: { value: "搜索发现上首页" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    await waitFor(() => {
      const write = fetchMock.mock.calls.find(
        ([, init]) => init?.method === "POST",
      );
      expect(write?.[0]).toContain(
        "/api/control/viral/videos/douyin/search-hit-2/archive",
      );
      expect(JSON.parse(String(write?.[1]?.body))).toMatchObject({
        confirm: true,
        reason: "搜索发现上首页",
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
