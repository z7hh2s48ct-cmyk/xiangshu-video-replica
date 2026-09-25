import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ViralCollectionBilling } from "./ViralCollectionBilling";
import { ViralVideosPage } from "./ViralVideosPage";

/** 已归档到云端的视频号行：默认覆盖「封面 / 头像 / 统计 / 发布时间」全字段。 */
function row(overrides: Record<string, unknown> = {}) {
  return {
    platform: "wechat_channels",
    video_id: "opaque/video=id",
    title: "庭院施工案例",
    author: "作者甲",
    author_avatar: "https://cdn.example.com/avatar.png",
    verified: true,
    category: "施工",
    tags: ["庭院", "自建房"],
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
    cover_url: "/api/viral/covers/wechat_channels/opaque",
    archive_status: "SUCCEEDED",
    ...overrides,
  };
}

const overview = {
  content_total: 12,
  archive_ready: 7,
  homepage_featured: 3,
  pending_archive: 2,
  archive_failed: 2,
  added_today: 5,
  last_created_at: "2026-09-25T02:00:00Z",
  collection_enabled: true,
  keyword_count: { douyin: 2, wechat_channels: 1 },
  next_collection_at: "2026-09-26T01:00:00Z",
  collection_interval_days: 1,
  last_fetched_at: "2026-09-25T01:00:00Z",
};

const controls = {
  collection_enabled: true,
  import_enabled: true,
  keywords: [{ platform: "douyin", category: "推荐", keyword: "农村自建房" }],
  per_keyword_limit: 20,
  collection_interval_days: 1,
};

function respond(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body };
}

/** 读接口不带 method（走 fetch 默认 GET），只有显式写了方法才算一次写请求。 */
const WRITE_METHODS = new Set(["POST", "PATCH", "PUT", "DELETE"]);

function isWrite(init: RequestInit | undefined) {
  return WRITE_METHODS.has(String(init?.method ?? "").toUpperCase());
}

type Write = (
  url: string,
  init: RequestInit | undefined,
) => ReturnType<typeof respond> | undefined;

/**
 * 本页挂载即并发三个读接口（列表、概览、采集配置），写接口又分转存 /
 * 策展 / 批量 / 互动补采，一刀切的 mock 会把它们混成同一个响应，
 * 因此按 URL 分发，未覆盖的写请求一律当作成功。
 */
function installFetch(
  overrides: {
    list?: () => unknown;
    overview?: () => unknown;
    controls?: () => unknown;
    search?: () => unknown;
    write?: Write;
  } = {},
) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.includes("/preview")) return respond({ url: "https://cdn/x.mp4" });
    if (url.includes("/api/control/viral/search")) {
      return respond(
        overrides.search?.() ?? {
          items: [],
          cursor: null,
          hasMore: false,
          keyword: "",
          platform: "douyin",
          timeRange: "week",
        },
      );
    }
    if (isWrite(init)) {
      return overrides.write?.(url, init) ?? respond({ ok: true });
    }
    if (url.includes("/api/control/viral/overview"))
      return respond(overrides.overview?.() ?? overview);
    if (url.includes("/api/control/settings/viral"))
      return respond(overrides.controls?.() ?? controls);
    if (url.includes("/api/control/viral/videos?"))
      return respond(overrides.list?.() ?? { items: [row()], total: 1 });
    return respond({ items: [], total: 0 });
  });
  vi.stubGlobal("fetch", fetchMock);
  setAdminCsrfToken("csrf-curation-test");
  return fetchMock;
}

function writesOf(fetchMock: ReturnType<typeof installFetch>) {
  return fetchMock.mock.calls.filter(([, init]) => isWrite(init));
}

describe("ViralVideosPage", () => {
  it("转存失败在确认框内显示原因，重试保留原幂等键", async () => {
    const fetchMock = installFetch({
      list: () => ({
        items: [row({ media_status: "NOT_STARTED", storage_uri: null })],
        total: 1,
      }),
      write: () =>
        respond(
          {
            detail: {
              code: "VIRAL_ARCHIVE_BUSY",
              message: "该平台已有后台任务，请完成后再转存。",
            },
          },
          409,
        ),
    });
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
    await waitFor(() => expect(writesOf(fetchMock)).toHaveLength(2));
    const [first, second] = writesOf(fetchMock);
    expect(first[1]?.headers).toEqual(second[1]?.headers);
    expect(screen.getByLabelText(/操作原因/)).toHaveValue("验证失败重试");
  });

  it("单条转存携带写入合同并显示后台排队，不能重复点击", async () => {
    let queued = false;
    const fetchMock = installFetch({
      list: () => ({
        items: [
          row({
            media_status: "NOT_STARTED",
            storage_uri: null,
            archive_status: queued ? "PENDING" : null,
          }),
        ],
        total: 1,
      }),
      write: () => {
        queued = true;
        return respond({ ok: true }, 202);
      },
    });
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
    const post = writesOf(fetchMock).find(([url]) => url.includes("/archive"));
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

  it("列表直接给出封面与头像，点标题打开详情抽屉且不预取视频", async () => {
    const fetchMock = installFetch();
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    // 封面与头像都必须先经站内地址绝对化，再交给 <img>，否则桌面端会渲染空图。
    expect(document.querySelector(".admin-viral-thumb img")).toHaveAttribute(
      "src",
      "http://127.0.0.1:8000/api/viral/covers/wechat_channels/opaque",
    );
    expect(document.querySelector(".admin-viral-author img")).toHaveAttribute(
      "src",
      "https://cdn.example.com/avatar.png",
    );
    expect(screen.getByText("发布").closest("span")).toHaveTextContent(
      "2026/09/06",
    );

    fireEvent.click(screen.getByRole("button", { name: "庭院施工案例" }));
    const drawer = screen.getByRole("dialog", { name: "视频详情" });
    expect(drawer).toHaveTextContent("opaque/video=id");
    expect(drawer).toHaveTextContent("cos://archive/video.mp4");
    expect(drawer).toHaveTextContent("作者甲");
    expect(drawer).toHaveTextContent("#庭院 #自建房");
    // 打开详情只读本地已有字段：视频地址要等点「预览视频」才外呼。
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/preview")),
    ).toBe(false);

    fireEvent.click(within(drawer).getByRole("button", { name: "预览视频" }));
    await waitFor(() => expect(drawer.querySelector("video")).toBeTruthy());
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "视频详情" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("概览数字与采集关键词来自服务端，采集停用后不能手动触发", async () => {
    const fetchMock = installFetch({
      overview: () => ({ ...overview, collection_enabled: false }),
      controls: () => ({ ...controls, keywords: [] }),
    });
    render(<ViralVideosPage />);
    const tiles = await screen.findByLabelText("爆款视频库概览");
    expect(
      within(tiles).getByText("内容池总量").closest("div"),
    ).toHaveTextContent("12");
    expect(
      within(tiles).getByText("待转存 / 失败").closest("div"),
    ).toHaveTextContent("2 / 2");
    expect(
      screen.getByText("尚未配置关键词，未配置时后台不会采集。"),
    ).toBeInTheDocument();
    const collect = screen.getByRole("button", { name: "立即采集" });
    expect(collect).toBeDisabled();
    expect(collect).toHaveAttribute(
      "title",
      expect.stringContaining("系统设置"),
    );
    // 概览与采集配置各读一次即可，不轮询。
    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls.filter((url) => url.includes("/viral/overview"))).toHaveLength(
      1,
    );
    expect(urls.filter((url) => url.includes("/settings/viral"))).toHaveLength(
      1,
    );
  });

  it("立即采集走独立确认框并带上人工填写的原因", async () => {
    const fetchMock = installFetch();
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    fireEvent.click(screen.getByRole("button", { name: "立即采集" }));
    expect(
      screen.getByRole("dialog", { name: "立即采集爆款视频" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "本月选题补采" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    expect(await screen.findByText(/已触发立即采集/)).toBeInTheDocument();
    const post = writesOf(fetchMock).find(([url]) =>
      url.includes("/viral/collect"),
    );
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      reason: "本月选题补采",
      confirm: true,
    });
  });

  it("先显示采集数据，人工确认后才展示首页，并能删除", async () => {
    let featured = false;
    let deleted = false;
    const fetchMock = installFetch({
      list: () =>
        deleted
          ? { items: [], total: 0 }
          : { items: [row({ homepage_featured: featured })], total: 1 },
      write: (url, init) => {
        if (!url.includes("/curation")) return undefined;
        const payload = JSON.parse(String(init?.body));
        featured = payload.action === "feature";
        deleted = payload.action === "delete";
        return undefined;
      },
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    expect(screen.getByText("未展示")).toBeInTheDocument();
    expect(writesOf(fetchMock)).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "展示到首页" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "人工筛选通过" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    await screen.findByText("首页展示设置已更新。");
    await screen.findByText("展示中");
    const patch = writesOf(fetchMock).find(([url]) =>
      url.includes("/curation"),
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

  it("勾选多条后批量策展整批提交一次请求", async () => {
    const fetchMock = installFetch({
      list: () => ({
        items: [row(), row({ video_id: "second", title: "第二支视频" })],
        total: 2,
      }),
      write: (url) =>
        url.includes("curation:batch")
          ? respond({ action: "feature", count: 2, items: [] })
          : undefined,
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    fireEvent.click(screen.getByLabelText("选择「庭院施工案例」"));
    fireEvent.click(screen.getByLabelText("选择「第二支视频」"));
    fireEvent.click(screen.getByRole("button", { name: "批量展示到首页" }));
    expect(
      screen.getByRole("dialog", { name: "批量展示 2 条到首页" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "批量上首页" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认操作" }));
    expect(
      await screen.findByText("已批量展示 2 条视频到首页。"),
    ).toBeInTheDocument();
    const batch = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("curation:batch"),
    );
    expect(JSON.parse(String(batch?.[1]?.body))).toMatchObject({
      action: "feature",
      items: [
        { platform: "wechat_channels", video_id: "opaque/video=id" },
        { platform: "wechat_channels", video_id: "second" },
      ],
      confirm: true,
      reason: "批量上首页",
    });
  });

  it("链接导入素材标注字段待补全，且不能展示到首页", async () => {
    const fetchMock = installFetch({
      list: () => ({
        items: [
          row({
            platform: "douyin",
            video_id: "imported-1",
            title: "链接导入的参考视频",
            category: "链接导入",
            // 链接导入只保证媒体本身：互动字段缺失时如实留空，不拿 0 顶替。
            comments: null,
            shares: null,
            collects: null,
          }),
        ],
        total: 1,
      }),
    });
    render(<ViralVideosPage />);
    await screen.findByText("链接导入的参考视频");

    expect(
      screen.getByText("字段待补全（链接导入仅保证媒体本身）"),
    ).toBeInTheDocument();
    // 归档就绪也不给展示：服务端会 409 拒绝，前端先挡住并说明原因。
    const feature = screen.getByRole("button", { name: "展示到首页" });
    expect(feature).toBeDisabled();
    expect(feature).toHaveAttribute(
      "title",
      expect.stringContaining("不进首页"),
    );

    // 勾选后批量展示也会被整批拒绝，事先提醒一句。
    fireEvent.click(screen.getByLabelText("选择「链接导入的参考视频」"));
    expect(screen.getByText(/所选含 1 条链接导入素材/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "链接导入的参考视频" }));
    const drawer = screen.getByRole("dialog", { name: "视频详情" });
    expect(
      within(drawer).getByRole("button", { name: "展示到首页" }),
    ).toBeDisabled();
    expect(within(drawer).getByText(/字段待补全/)).toBeInTheDocument();
    // 标注与禁用都是本地判定，不该因此发出任何写请求。
    expect(writesOf(fetchMock)).toHaveLength(0);
  });

  it("只读账号可以查看数据，不能设置首页或删除", async () => {
    installFetch();
    render(<ViralVideosPage readOnly />);
    await screen.findByText("庭院施工案例");
    expect(
      screen.queryByRole("button", { name: "展示到首页" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "删除" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "实时搜索入库" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "详情" })).toBeInTheDocument();
  });

  it("仅点击补齐本页互动才调用写接口，保留真实零值和未提供字段", async () => {
    const fetchMock = installFetch({
      list: () => ({
        items: [row({ comments: null, shares: null, collects: null })],
        total: 1,
      }),
      write: () =>
        respond({
          likes: 276,
          comments: 0,
          shares: null,
          collects: 279,
          statistics_checked_at: "2026-09-15T00:00:00Z",
          statistics_retry_at: null,
          statistics_status: "partial",
        }),
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    expect(writesOf(fetchMock)).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "补齐本页互动" }));
    expect(await screen.findByText(/接口部分提供 1 条/)).toBeInTheDocument();
    expect(screen.getByText("276")).toBeInTheDocument();
    expect(screen.getByText("评论").closest("div")).toHaveTextContent("评论0");
    expect(screen.getByText("未提供")).toBeInTheDocument();
    const write = writesOf(fetchMock).find(([url]) =>
      url.includes("/statistics"),
    );
    expect(write?.[0]).toContain("opaque%2Fvideo%3Did/statistics");
    expect(write?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-curation-test",
    });
    expect(JSON.parse(String(write?.[1]?.body))).toMatchObject({
      confirm: true,
    });
  });

  it("实时搜索把命中视频并入内容池并展示可操作结果", async () => {
    const fetchMock = installFetch({
      list: () => ({ items: [], total: 0 }),
      search: () => ({
        items: [
          row({
            platform: "douyin",
            video_id: "upstream-hit-1",
            title: "实时搜索命中视频",
            media_status: "NOT_STARTED",
            storage_uri: null,
          }),
        ],
        cursor: "cursor-next",
        hasMore: true,
        keyword: "农村自建房",
        platform: "douyin",
        timeRange: "week",
      }),
    });
    render(<ViralVideosPage />);
    await screen.findByText("暂无符合条件的采集视频。");
    fireEvent.click(screen.getByRole("button", { name: "实时搜索入库" }));

    const panel = screen.getByRole("region", { name: "实时搜索上游" });
    fireEvent.change(within(panel).getByLabelText("关键词"), {
      target: { value: "农村自建房" },
    });
    fireEvent.click(
      panel.querySelector('button[type="submit"]') as HTMLButtonElement,
    );

    expect(await screen.findByText("实时搜索命中视频")).toBeInTheDocument();
    // 未归档的命中视频：允许转存，暂不能上首页。
    expect(screen.getByRole("button", { name: "转存到云端" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "展示到首页" })).toBeDisabled();
    // 请求走管理端写契约：CSRF + 幂等键 + confirm/reason。
    const searchCall = fetchMock.mock.calls.find(([url]) =>
      String(url).includes("/api/control/viral/search"),
    );
    expect(JSON.parse(String(searchCall?.[1]?.body))).toMatchObject({
      keyword: "农村自建房",
      platform: "douyin",
      time_range: "week",
      confirm: true,
    });
    expect(String(JSON.parse(String(searchCall?.[1]?.body)).reason)).toContain(
      "农村自建房",
    );
    expect(searchCall?.[1]?.headers).toMatchObject({
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
});
