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

/** 已准备到云端的视频号行：默认覆盖「封面 / 头像 / 统计 / 发布时间」全字段。 */
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
 * 本页挂载即并发三个读接口（列表、概览、采集配置），写接口又分准备 /
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
    if (url.includes("/operations/estimate"))
      return respond({
        snapshot: "a".repeat(64),
        unitCostFen: null,
        logicalCallsMax: 1,
        normalRetryCallsMax: 3,
        dataCostMaxFen: null,
        reusedVideos: 0,
        mediaDownloadsMax: 1,
        totalCostFen: null,
        note: "总费用未知；不扣客户积分。",
      });
    if (url.includes("/details?"))
      return respond({
        video: row(),
        media: {
          audio: { status: "SUCCEEDED", ready: true },
          video: { status: "NOT_STARTED", ready: false },
        },
        copy: null,
        sourceDescription: null,
        originalUrl: null,
        sourceKeywords: [],
        relatedVideos: [],
        business: {
          window: "全部已记录历史",
          countingRule: "测试按钱包主体去重",
          detailAccounts: 0,
          copyAccounts: 0,
          favoriteAccounts: 0,
          chargedCredits: 0,
          revenueFen: 0,
          knownRevenueFen: 0,
          unknownRevenueOperations: 0,
          collectionCostFen: null,
          costNote: "成本待核对",
          customers: [],
          customerTotal: 0,
          offset: 0,
          limit: 20,
        },
      });

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
  return fetchMock.mock.calls.filter(
    ([url, init]) => isWrite(init) && !url.includes("/operations/estimate"),
  );
}

it("分类目录覆盖当前页之外，未分类和批次筛选准确请求并保留导航范围", async () => {
  const fetchMock = installFetch({
    list: () => ({
      items: [row()],
      total: 52,
      categories: ["当前页", "分页以外"],
    }),
  });
  const onScopeChange = vi.fn();
  render(
    <ViralVideosPage
      initialListQuery="cohortStage=copy&collectedFrom=2026-09-01&collectedTo=2026-09-30&platform=wechat_channels"
      onScopeChange={onScopeChange}
    />,
  );
  await screen.findByRole("option", { name: "分页以外" });
  expect(screen.queryByRole("button", { name: "批量屏蔽" })).toBeNull();
  fireEvent.change(screen.getByLabelText("分类"), {
    target: { value: "__uncategorized__" },
  });
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(
        ([url]) =>
          url.includes("uncategorized=true") &&
          url.includes("cohort_stage=copy") &&
          url.includes("collected_from=2026-09-01"),
      ),
    ).toBe(true),
  );
  expect(onScopeChange.mock.lastCall?.[0].listQuery).toContain(
    "category=__uncategorized__",
  );
  expect(writesOf(fetchMock)).toHaveLength(0);
});

describe("ViralVideosPage", () => {
  it("未知费用先明确确认，取消搜索不调用付费接口", async () => {
    const fetchMock = installFetch();
    render(
      <ViralVideosPage
        initialSearch={{ keyword: "预估测试", platform: "douyin" }}
      />,
    );
    const panel = await screen.findByRole("region", { name: "实时搜索上游" });
    fireEvent.click(
      panel.querySelector('button[type="submit"]') as HTMLButtonElement,
    );
    await screen.findByRole("button", { name: "确认费用并执行" });
    expect(screen.getByLabelText("操作前费用预估")).toHaveTextContent("未知");
    expect(
      fetchMock.mock.calls.some(([url]) => url.includes("/viral/search")),
    ).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(
      fetchMock.mock.calls.some(([url]) => url.includes("/viral/search")),
    ).toBe(false);
  });
  it("需求补货打开既有实时搜索并预填平台关键词，进入页面不外呼", async () => {
    const fetchMock = installFetch();
    render(
      <ViralVideosPage
        initialSearch={{ keyword: "需求缺口词", platform: "wechat_channels" }}
      />,
    );
    expect(await screen.findByLabelText("关键词")).toHaveValue("需求缺口词");
    expect(screen.getByLabelText("平台")).toHaveValue("wechat_channels");
    expect(screen.getByRole("tab", { name: "实时搜索入库" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      fetchMock.mock.calls.some(([url]) =>
        url.includes("/api/control/viral/search"),
      ),
    ).toBe(false);
  });
  it.each([
    [0, 2],
    [1, 1],
    [2, 0],
  ])("批量完成%s条、排队%s条按后端独立计数展示", async (count, queued) => {
    const videos = [row(), row({ video_id: "second", title: "第二支视频" })];
    const fetchMock = installFetch({
      list: () => ({ items: videos, total: 2 }),
      write: (url) =>
        url.includes("curation:batch")
          ? respond({
              action: "feature",
              count,
              queued_count: queued,
              items: videos.map((video, index) => ({
                ...video,
                deleted: false,
                queued_for_preparation: index < queued,
                homepage_featured: index >= queued,
              })),
            })
          : undefined,
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    fireEvent.click(screen.getByLabelText("选择「庭院施工案例」"));
    fireEvent.click(screen.getByLabelText("选择「第二支视频」"));
    fireEvent.click(screen.getByRole("button", { name: "批量展示到首页" }));
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    if (queued > 0)
      await screen.findByText(
        `已排队准备 ${queued} 条，尚未上首页；其余 ${count} 条已完成操作。`,
      );
    else await screen.findByText("已批量展示 2 条视频到首页。");
    expect(writesOf(fetchMock)).toHaveLength(1);
    expect(screen.queryByText(/其余 -/)).not.toBeInTheDocument();
  });
  it("20条未准备内容一次确认上首页且不要求原因输入", async () => {
    const videos = Array.from({ length: 20 }, (_, i) =>
      row({
        video_id: `twenty-${i}`,
        title: `批量内容${i}`,
        media_status: "NOT_STARTED",
        storage_uri: null,
      }),
    );
    const fetchMock = installFetch({
      list: () => ({ items: videos, total: 20 }),
      write: () =>
        respond({
          action: "feature",
          count: 0,
          queued_count: 20,
          items: videos.map((video) => ({
            ...video,
            queued_for_preparation: true,
            homepage_featured: false,
            deleted: false,
          })),
        }),
    });
    render(<ViralVideosPage />);
    await screen.findByText("批量内容0");
    fireEvent.click(screen.getByLabelText("选择本页全部视频"));
    fireEvent.click(screen.getByRole("button", { name: "批量展示到首页" }));
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    await screen.findByText(
      "已排队准备 20 条，尚未上首页；其余 0 条已完成操作。",
    );
    expect(writesOf(fetchMock)).toHaveLength(1);
    expect(
      JSON.parse(String(writesOf(fetchMock)[0][1]?.body)).items,
    ).toHaveLength(20);
  });
  it("批量失败不显示完成或排队成功数量", async () => {
    const videos = [row(), row({ video_id: "second", title: "第二支视频" })];
    installFetch({
      list: () => ({ items: videos, total: 2 }),
      write: () =>
        respond(
          {
            detail: {
              code: "VIRAL_COLLECTION_BUSY",
              message: "已有准备任务，请稍后再试。",
            },
          },
          409,
        ),
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    fireEvent.click(screen.getByLabelText("选择「庭院施工案例」"));
    fireEvent.click(screen.getByLabelText("选择「第二支视频」"));
    fireEvent.click(screen.getByRole("button", { name: "批量展示到首页" }));
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    await screen.findAllByText(/批量操作爆款视频失败/);
    expect(screen.queryByText(/其余 .*条已完成/)).not.toBeInTheDocument();
  });
  it("卡片与表格切换保留零值与未知，详情不把缺失客户使用伪装成零", async () => {
    installFetch({
      list: () => ({
        items: [row({ likes: 0, comments: null, cover_url: null })],
        total: 1,
      }),
    });
    render(<ViralVideosPage readOnly />);
    await screen.findByRole("button", { name: "详情" });
    expect(screen.getByRole("button", { name: "卡片" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "表格" }));
    expect(screen.getByRole("button", { name: "表格" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    fireEvent.click(screen.getByRole("button", { name: "详情" }));
    const dialog = await screen.findByRole("dialog", { name: "视频详情" });
    await screen.findByText("客户使用与收入");
    expect(dialog).toHaveTextContent("客户使用与收入");
    expect(dialog).toHaveTextContent("播放量：暂无数据");
    expect(dialog).toHaveTextContent("未展示");
  });
  it("准备失败在确认框内显示原因，重试保留原幂等键", async () => {
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
              message: "该平台已有后台任务，请完成后再准备。",
            },
          },
          409,
        ),
    });
    render(<ViralVideosPage />);
    fireEvent.click(await screen.findByRole("button", { name: "准备素材" }));
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    const dialog = await screen.findByRole("dialog", {
      name: "准备单条视频素材",
    });
    await waitFor(() =>
      expect(dialog).toHaveTextContent("该平台已有后台任务，请完成后再准备。"),
    );
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    await waitFor(() => expect(writesOf(fetchMock)).toHaveLength(2));
    const [first, second] = writesOf(fetchMock);
    expect(first[1]?.headers).toEqual(second[1]?.headers);
    expect(JSON.parse(String(first[1]?.body))).toEqual(
      JSON.parse(String(second[1]?.body)),
    );
  });

  it("单条准备携带写入合同并显示后台排队，不能重复点击", async () => {
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
    fireEvent.click(await screen.findByRole("button", { name: "准备素材" }));
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    expect(
      await screen.findByRole("button", { name: "后台准备中" }),
    ).toBeDisabled();
    expect(
      screen.getByText("待准备", { selector: "span" }),
    ).toBeInTheDocument();
    const post = writesOf(fetchMock).find(([url]) => url.includes("/archive"));
    expect(post?.[0]).toContain("opaque%2Fvideo%3Did/archive");
    expect(JSON.parse(String(post?.[1]?.body))).toMatchObject({
      reason: "准备素材或调整顺序",
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
    const drawer = await screen.findByRole("dialog", { name: "视频详情" });
    await screen.findByText("视频编号");
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
      within(tiles).getByText("待准备 / 失败").closest("div"),
    ).toHaveTextContent("2 / 2");
    expect(
      screen.getByText("尚未配置关键词，未配置时后台不会采集。"),
    ).toBeInTheDocument();
    const collect = screen.getByRole("button", { name: "立即采集" });
    expect(collect).toBeDisabled();
    expect(collect).toHaveAttribute(
      "title",
      expect.stringContaining("采集设置"),
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
      await screen.findByRole("dialog", { name: "立即采集爆款视频" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "本月选题补采" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
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
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    await screen.findByText("首页展示设置已更新。");
    await screen.findByText("展示中");
    const patch = writesOf(fetchMock).find(([url]) =>
      url.includes("/curation"),
    );
    expect(patch?.[0]).toContain("opaque%2Fvideo%3Did/curation");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      action: "feature",
      confirm: true,
      reason: "上首页",
    });
    expect(patch?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-curation-test",
    });

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "不适合当前选题" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
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
      await screen.findByRole("dialog", { name: "批量展示 2 条到首页" }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
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
      reason: "上首页",
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
    // 素材已准备也不给展示：服务端会 409 拒绝，前端先挡住并说明原因。
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
    const drawer = await screen.findByRole("dialog", { name: "视频详情" });
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
      screen.queryByRole("tab", { name: "实时搜索入库" }),
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
    fireEvent.click(
      await screen.findByRole("button", { name: "确认费用并执行" }),
    );
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
    fireEvent.click(screen.getByRole("tab", { name: "实时搜索入库" }));
    expect(screen.getByRole("tab", { name: "实时搜索入库" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.queryByRole("tabpanel", { name: "视频库列表" })).toBeNull();

    const panel = screen.getByRole("region", { name: "实时搜索上游" });
    fireEvent.change(within(panel).getByLabelText("关键词"), {
      target: { value: "农村自建房" },
    });
    fireEvent.click(
      panel.querySelector('button[type="submit"]') as HTMLButtonElement,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "确认费用并执行" }),
    );

    expect(await screen.findByText("实时搜索命中视频")).toBeInTheDocument();
    // 未准备素材允许一次上首页：后台先准备，就绪后发布。
    expect(screen.getByRole("button", { name: "准备素材" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "展示到首页" })).toBeEnabled();
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
    expect(screen.getByText(/未成功 1 次；处理中 0 次/)).toBeInTheDocument();
    expect(screen.getByText(/失败 1 笔；待扣 0 笔/)).toBeInTheDocument();
    expect(screen.getByText(/已知成本 ¥0.00000125/)).toBeInTheDocument();
    expect(fetchMock.mock.calls[0][0]).not.toContain("user_id=other");
    fireEvent.click(screen.getByRole("button", { name: "客户扣费明细" }));
    expect(await screen.findByText("客户甲")).toBeInTheDocument();
    expect(screen.getByText("余额不足，扣费失败")).toBeInTheDocument();
    expect(screen.getByText("3 / 0 积分")).toBeInTheDocument();
  });

  it("首页顺序在编排页维护，不再提供叠加置顶", async () => {
    installFetch({
      list: () => ({ items: [row({ homepage_featured: true })], total: 1 }),
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    expect(screen.queryByRole("button", { name: "置顶" })).toBeNull();
    expect(screen.getByRole("link", { name: "首页编排" })).toHaveAttribute(
      "href",
      "#admin/viralHomepage",
    );
  });

  it.each([1, 2])("准备%d条只排队，不承诺发布或完成", async (count) => {
    const rows = Array.from({ length: count }, (_, i) =>
      row({
        video_id: `prepare-${i}`,
        media_status: "NOT_STARTED",
        storage_uri: null,
      }),
    );
    const fetchMock = installFetch({
      list: () => ({ items: rows, total: count }),
      write: (_url, _init) =>
        respond({
          count: 0,
          queued_count: count,
          queued_for_preparation: true,
          items: rows.map((v) => ({ ...v, queued_for_preparation: true })),
        }),
    });
    render(<ViralVideosPage />);
    await screen.findAllByText("庭院施工案例");
    fireEvent.click(screen.getByLabelText("选择本页全部视频"));
    fireEvent.click(screen.getByRole("button", { name: "批量准备素材" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      `准备 ${count} 条视频的素材`,
    );
    expect(await screen.findByRole("dialog")).toHaveTextContent("不自动上首页");
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    await screen.findByText(/不会自动上首页，准备完成后请另行选择/);
    const writes = writesOf(fetchMock);
    expect(writes).toHaveLength(1);
    expect(JSON.parse(String(writes[0][1]?.body)).action).toBe("prepare");
    expect(screen.queryByText(/其余.*已完成/)).toBeNull();
  });

  it("行内隐藏视频需确认原因并调用可用状态接口", async () => {
    let hidden = false;
    const fetchMock = installFetch({
      list: () => ({
        items: [row({ availability: hidden ? "HIDDEN" : "AVAILABLE" })],
        total: 1,
      }),
      write: (url, init) => {
        if (
          url.includes("/availability") &&
          JSON.parse(String(init?.body)).status === "HIDDEN"
        )
          hidden = true;
        return undefined;
      },
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");

    fireEvent.click(screen.getByRole("button", { name: "隐藏" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "内容不符合上架要求" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    await screen.findByText("视频已隐藏，前台不再展示。");

    const patch = writesOf(fetchMock).find(([url]) =>
      url.includes("/availability"),
    );
    expect(patch?.[0]).toContain("/availability");
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      status: "HIDDEN",
      confirm: true,
      reason: "内容不符合上架要求",
    });
    expect(patch?.[1]?.headers).toMatchObject({
      "X-Admin-CSRF": "csrf-curation-test",
    });
    await screen.findByRole("button", { name: "恢复显示" });
  });

  it("已隐藏视频显示恢复显示按钮并恢复为可用", async () => {
    let restored = false;
    const fetchMock = installFetch({
      list: () => ({
        items: [row({ availability: restored ? "AVAILABLE" : "HIDDEN" })],
        total: 1,
      }),
      write: (url) => {
        if (url.includes("/availability")) restored = true;
        return undefined;
      },
    });
    render(<ViralVideosPage />);
    await screen.findByText("庭院施工案例");
    expect(
      screen.getByRole("button", { name: "恢复显示" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "隐藏" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "恢复显示" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "内容复核通过" },
    });
    fireEvent.click(await screen.findByRole("button", { name: "确认操作" }));
    await screen.findByText(
      "视频已恢复到库中，客户可见性仍按发布时间、素材和首页排期判断。",
    );

    const patch = writesOf(fetchMock).find(([url]) =>
      url.includes("/availability"),
    );
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      status: "AVAILABLE",
      confirm: true,
      reason: "内容复核通过",
    });
    await screen.findByRole("button", { name: "隐藏" });
  });
});

it("可精确打开列表页外的任务视频，查询编码ID且不发起准备或供应商请求", async () => {
  const video = row({ video_id: "off-page/opaque=id", title: "列表页外目标" });
  const fetchMock = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url.includes("/details"))
      return respond({
        video,
        media: {
          video: { status: "FAILED", ready: false },
          audio: { status: "PENDING", ready: false },
        },
        copy: null,
        sourceKeywords: [],
        relatedVideos: [],
        originalUrl: null,
        business: {
          window: "历史",
          countingRule: "按主体",
          detailAccounts: 0,
          copyAccounts: 0,
          favoriteAccounts: 0,
          chargedCredits: 0,
          revenueFen: 0,
          knownRevenueFen: 0,
          unknownRevenueOperations: 0,
          collectionCostFen: null,
          costNote: "历史/存储未知",
          customers: [],
          customerTotal: 0,
          offset: 0,
          limit: 20,
        },
      });
    if (url.includes("overview")) return respond(overview);
    if (url.includes("runtime")) return respond(controls);
    return respond({ items: [], total: 0 });
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <ViralVideosPage
      initialVideo={{
        platform: "wechat_channels",
        video_id: "off-page/opaque=id",
      }}
    />,
  );
  await screen.findByText("列表页外目标");
  expect(
    fetchMock.mock.calls.some(([url]) =>
      url.includes("off-page%2Fopaque%3Did/details"),
    ),
  ).toBe(true);
  expect(
    fetchMock.mock.calls.every(
      ([, init]) => !init?.method || init.method === "GET",
    ),
  ).toBe(true);
});
