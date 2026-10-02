import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ViralHomepagePage } from "./ViralHomepagePage";

function install(withRevision = false) {
  let ids = ["one", "two"];
  const fetchMock = vi.fn(async (_url: string, init?: RequestInit) => {
    if (init?.method === "PUT") ids = JSON.parse(String(init.body)).video_ids;
    if (init?.method === "POST") {
      const move = JSON.parse(String(init.body));
      ids.splice(
        move.position - 1,
        0,
        ids.splice(ids.indexOf(move.video_id), 1)[0],
      );
    }
    const items = ids.map((id) => ({
      platform: "douyin",
      video_id: id,
      title: `测试视频${id}`,
      author: "假数据作者",
      likes: 0,
      comments: null,
      shares: null,
      collects: 0,
      homepage_featured: true,
      homepage_live: true,
      homepage_starts_at: null,
      homepage_ends_at: null,
      homepage_featured_at: null,
      collection_published: true,
      media_status: "SUCCEEDED",
      storage_uri: "fake://video",
      cover_key: null,
      cover_url: null,
      availability: "AVAILABLE",
    }));
    return {
      ok: true,
      status: 200,
      json: async () => ({
        platform: "douyin",
        revision: withRevision ? "r".repeat(64) : undefined,
        items,
        preview: items,
        preview_limit: 12,
        capacity: null,
        counting_rule: "预览12条不是容量",
      }),
    };
  });
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("fake-csrf");
  return fetchMock;
}
afterEach(() => {
  vi.unstubAllGlobals();
  setAdminCsrfToken("");
});
describe("首页编排", () => {
  it("新编排提交目标位置与revision，不发送完整首页数组", async () => {
    const fetchMock = install(true);
    render(<ViralHomepagePage />);
    await screen.findAllByText("测试视频two");
    fireEvent.click(screen.getAllByRole("button", { name: "上移" })[1]);
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
      ).toBe(true),
    );
    const call = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(String(call?.[0])).toContain("/viral/homepage/move");
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body).toMatchObject({
      video_id: "two",
      position: 1,
      expected_revision: "r".repeat(64),
    });
    expect(body).not.toHaveProperty("video_ids");
    expect(body).not.toHaveProperty("expected_video_ids");
  });
  it("上移提交完整顺序与旧快照并刷新客户预览", async () => {
    const fetchMock = install();
    render(<ViralHomepagePage />);
    await screen.findAllByText("测试视频two");
    fireEvent.click(screen.getAllByRole("button", { name: "上移" })[1]);
    await screen.findByText(/首页顺序已保存/);
    const writes = fetchMock.mock.calls.filter(
      ([, init]) => init?.method === "PUT",
    );
    expect(writes).toHaveLength(1);
    expect(JSON.parse(String(writes[0][1]?.body))).toMatchObject({
      platform: "douyin",
      video_ids: ["two", "one"],
      expected_video_ids: ["one", "two"],
      confirm: true,
    });
    expect(
      document.querySelectorAll(".admin-homepage-order strong")[0],
    ).toHaveTextContent("two");
    expect(
      document.querySelectorAll(".admin-homepage-preview strong")[0],
    ).toHaveTextContent("two");
  });
  it("排期使用北京时间，下线早于上线时不写接口", async () => {
    const fetchMock = install();
    render(<ViralHomepagePage />);
    await screen.findAllByText("测试视频one");
    fireEvent.click(screen.getAllByRole("button", { name: "设置排期" })[0]);
    fireEvent.change(screen.getByLabelText("上线时间（北京时间）"), {
      target: { value: "2026-10-02T08:00" },
    });
    fireEvent.change(screen.getByLabelText("下线时间（北京时间）"), {
      target: { value: "2026-10-01T08:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存排期" }));
    await screen.findByText("下线时间必须晚于上线时间。");
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
    ).toBe(false);
    fireEvent.change(screen.getByLabelText("下线时间（北京时间）"), {
      target: { value: "2026-10-03T08:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存排期" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([, init]) => init?.method === "PATCH"),
      ).toBe(true),
    );
    const write = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(JSON.parse(String(write?.[1]?.body))).toMatchObject({
      starts_at: "2026-10-02T00:00:00.000Z",
      ends_at: "2026-10-03T00:00:00.000Z",
    });
  });
  it("审计员可看健康提示和预览但没有顺序排期写操作", async () => {
    const fetchMock = install();
    render(<ViralHomepagePage readOnly />);
    await screen.findAllByText("测试视频one");
    expect(
      screen.queryByRole("button", { name: "上移" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "设置排期" }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByText("封面缺失")).toHaveLength(2);
    expect(
      fetchMock.mock.calls.every(
        ([, init]) => !init?.method || init.method === "GET",
      ),
    ).toBe(true);
  });
});

it("首页待准备素材在确认前读取费用，取消无写入，再确认携带最新快照", async () => {
  let price = 1;
  const video = {
    platform: "douyin",
    video_id: "pending-one",
    title: "首页待准备",
    author: "测试作者",
    availability: "AVAILABLE",
    media_status: "FAILED",
    homepage_featured: false,
    homepage_starts_at: null,
    homepage_ends_at: null,
    storage_uri: null,
    cover_key: null,
  };
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url.includes("/operations/estimate"))
      return new Response(
        JSON.stringify({
          snapshot: String(price).repeat(64),
          unitCostFen: price,
          logicalCallsMax: 0,
          normalRetryCallsMax: 0,
          dataCostMaxFen: 0,
          reusedVideos: 0,
          mediaDownloadsMax: 1,
          totalCostFen: null,
          note: "存储与流量未知",
        }),
      );
    if (url.endsWith("/archive"))
      return new Response(JSON.stringify({ status: "PENDING" }));
    return new Response(
      JSON.stringify({
        platform: "douyin",
        items: [],
        preparing: [video],
        preview: [],
        capacity: null,
        counting_rule: "按就绪内容",
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("fake-csrf");
  render(<ViralHomepagePage />);
  fireEvent.click(
    await screen.findByRole("button", { name: "准备 / 重试素材" }),
  );
  const first = await screen.findByRole("dialog", {
    name: "确认首页素材准备费用",
  });
  expect(first).toHaveTextContent("¥0.01");
  fireEvent.click(screen.getByRole("button", { name: "取消" }));
  expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/archive"))).toBe(
    false,
  );
  price = 2;
  fireEvent.click(screen.getByRole("button", { name: "准备 / 重试素材" }));
  await screen.findByRole("dialog");
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "虚构首页准备验收" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认准备" }));
  await screen.findByText(/已提交素材准备/);
  const writes = fetchMock.mock.calls.filter(([url]) =>
    url.endsWith("/archive"),
  );
  expect(writes).toHaveLength(1);
  expect(JSON.parse(String(writes[0][1]?.body))).toMatchObject({
    confirm: true,
    expected_cost_snapshot: "2".repeat(64),
  });
});
