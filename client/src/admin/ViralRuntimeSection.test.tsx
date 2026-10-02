import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { setAdminCsrfToken } from "../api";
import { ViralRuntimeSection } from "./ViralRuntimeSection";

function response(payload: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const controls = {
  collection_enabled: true,
  import_enabled: true,
  pending_imports: 2,
  running_imports: 1,
  failed_imports: 3,
  pending_refreshes: 1,
  running_refreshes: 1,
  failed_refreshes: 0,
  source_configured: true,
  platforms: [
    {
      platform: "douyin",
      cached_videos: 24,
      last_fetched_at: "2026-09-07 10:00:00",
      refresh_status: "ok",
      last_refresh_error: null,
    },
    {
      platform: "wechat_channels",
      cached_videos: 18,
      last_fetched_at: null,
      refresh_status: "refreshing",
      last_refresh_error: null,
    },
  ],
};

describe("ViralRuntimeSection", () => {
  it("手动采集先读取调用量和部分费用预估，确认真实原因后才入队", async () => {
    setAdminCsrfToken("csrf-estimate");
    const fetchMock = vi.fn((url: string, init?: RequestInit) =>
      response(
        url.includes("/estimate")
          ? {
              enabledKeywords: 2,
              searchCallsMin: 2,
              searchCallsMax: 4,
              searchCostMinFen: 5,
              searchCostMaxFen: 10,
              videoLimit: 12,
              snapshot: "a".repeat(64),
              customerCount: 2,
              customerCreditsPerConfirmedCall: 3,
              customerCreditsMaxEach: 108,
              physicalDataCallsMin: 2,
              physicalDataCallsMax: 36,
              totalCostFen: null,
              note: "搜索部分预估，素材重试费用另计",
            }
          : init?.method === "POST"
            ? { queued: true }
            : controls,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection />);
    fireEvent.click(await screen.findByRole("button", { name: "立即采集" }));
    expect(await screen.findByText(/预计搜索调用 2～4 次/)).toBeInTheDocument();
    expect(screen.getByText(/¥0.05～¥0.10/)).toBeInTheDocument();
    expect(screen.getByText(/此操作会影响客户积分/)).toBeInTheDocument();
    expect(screen.getByText(/当前符合收费条件客户2 位/)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "知晓预估后手动补货" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    await screen.findByText("采集已入队，可查看平台进度。");
    const call = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(call?.[0]).toContain("/api/control/viral/collect");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      confirm: true,
      reason: "知晓预估后手动补货",
      expected_estimate_snapshot: "a".repeat(64),
    });
  });

  it("预算80%和100%提醒区分，未知费用不显示为总计零", async () => {
    const fetchMock = vi.fn(() =>
      response({
        ...controls,
        budget_status: "warning",
        month_spend_fen: 8000.5,
        month_unknown_cost_count: 2,
        month_pending_cost_count: 1,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection readOnly />);
    expect(await screen.findByText(/已知成本已达预算80%/)).toBeInTheDocument();
    expect(screen.getByText(/当前总费用未知/)).toBeInTheDocument();
    expect(screen.getByText(/¥80.0050/)).toBeInTheDocument();
  });

  it("保存采集计划且不覆盖独立关键词配置", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      response(controls),
    );
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-viral");
    render(<ViralRuntimeSection />);
    fireEvent.change(await screen.findByLabelText("默认每词条数"), {
      target: { value: "12" },
    });
    fireEvent.change(screen.getByLabelText("采集频率"), {
      target: { value: "1" },
    });
    fireEvent.change(screen.getByLabelText("执行时间（上海时间）"), {
      target: { value: "09:15" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存采集设置" }));
    expect(
      screen.getByRole("region", { name: "采集设置变更摘要" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/月度预算：不限 → 不限/)).toBeInTheDocument();
    expect(screen.getByText(/最低点赞：不限 → 不限/)).toBeInTheDocument();
    // P0-8：原因由操作人填写，空原因不能提交。
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(await screen.findByText("请填写操作原因")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "新增庭院类关键词备货" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    await screen.findByText("采集设置已保存。");
    const patch = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      per_keyword_limit: 12,
      collection_interval_days: 1,
      collection_time: "09:15",
      reason: "新增庭院类关键词备货",
      confirm: true,
    });
    expect(JSON.parse(String(patch?.[1]?.body))).not.toHaveProperty("keywords");
    expect(JSON.parse(String(patch?.[1]?.body))).not.toHaveProperty(
      "expected_keywords",
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("展示平台业务状态与链接导入说明，并移除视频ID表单", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response(controls)),
    );
    render(<ViralRuntimeSection />);

    expect(await screen.findByText("客户链接导入")).toBeInTheDocument();
    expect(
      screen.getByText("客户粘贴视频链接并导入创作的功能。"),
    ).toBeInTheDocument();
    expect(screen.getByText("最近采集成功")).toBeInTheDocument();
    expect(screen.getByText("采集中")).toBeInTheDocument();
    expect(screen.queryByLabelText("视频 ID")).not.toBeInTheDocument();
  });

  it("采集进行中轮询只刷新运行状态，不覆盖未保存的质量草稿", async () => {
    const serverControls = {
      ...controls,
      keywords: [
        { platform: "douyin", category: "庭院案例", keyword: "服务端旧词" },
      ],
      per_keyword_limit: 10,
      collection_interval_days: 7,
    };
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      response(serverControls),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection />);

    fireEvent.change(await screen.findByLabelText("最低点赞数"), {
      target: { value: "500" },
    });
    fireEvent.change(await screen.findByLabelText("默认每词条数"), {
      target: { value: "21" },
    });

    // 等到采集进行中的 3 秒轮询发生（第 2 次读取）；期间草稿不能被回滚。
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2), {
      timeout: 4500,
    });
    expect(screen.getByLabelText("最低点赞数")).toHaveValue(500);
    expect(screen.getByLabelText("默认每词条数")).toHaveValue(21);
  });

  it("旧轮询结果不能回滚刚保存的采集开关", async () => {
    setAdminCsrfToken("csrf-viral");
    let resolvePolling:
      | ((value: Awaited<ReturnType<typeof response>>) => void)
      | undefined;
    let reads = 0;
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === "PATCH")
        return response({ ...controls, collection_enabled: false });
      reads += 1;
      if (reads === 2)
        return new Promise<Awaited<ReturnType<typeof response>>>((resolve) => {
          resolvePolling = resolve;
        });
      return response(controls);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection />);

    fireEvent.click(await screen.findByRole("button", { name: "暂停采集" }));
    await waitFor(() => expect(reads).toBe(2), { timeout: 4500 });
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "国庆期间暂停采集" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(
      await screen.findByRole("button", { name: "开启采集" }),
    ).toBeInTheDocument();

    await act(async () => {
      resolvePolling?.(await response(controls));
    });
    expect(
      screen.getByRole("button", { name: "开启采集" }),
    ).toBeInTheDocument();
  });

  it("暂停采集时写入操作人填写的原因", async () => {
    setAdminCsrfToken("csrf-viral");
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === "PATCH") {
        return response({ ...controls, collection_enabled: false });
      }
      return response(controls);
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection />);

    fireEvent.click(await screen.findByRole("button", { name: "暂停采集" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "国庆期间暂停采集" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));

    expect(await screen.findByText("采集设置已保存。")).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(patch?.[0]).toContain("/api/control/settings/viral");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      collection_enabled: false,
      import_enabled: true,
      confirm: true,
      reason: "国庆期间暂停采集",
    });
  });
});

it("预算按元编辑、按分提交，清空质量和预算明确传 null", async () => {
  setAdminCsrfToken("csrf-viral");
  const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
    response({
      ...controls,
      monthly_budget_fen: 10000,
      quality_min_likes: 500,
      quality_duration_min_ms: 1000,
      quality_duration_max_ms: 60000,
      ...(init?.method === "PATCH" ? JSON.parse(String(init.body)) : {}),
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(<ViralRuntimeSection />);
  expect(await screen.findByLabelText("月度采集预算（元）")).toHaveValue(100);
  expect(screen.getByLabelText("最短时长（秒）")).toHaveValue(1);
  expect(screen.getByLabelText("最长时长（秒）")).toHaveValue(60);
  fireEvent.change(screen.getByLabelText("月度采集预算（元）"), {
    target: { value: "100.75" },
  });
  fireEvent.click(screen.getByRole("button", { name: "保存采集设置" }));
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "调整月度预算" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
  await screen.findByText("采集设置已保存。");
  const patches = () =>
    fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH");
  expect(JSON.parse(String(patches()[0][1]?.body)).monthly_budget_fen).toBe(
    10075,
  );
  for (const name of [
    "月度采集预算（元）",
    "最低点赞数",
    "最短时长（秒）",
    "最长时长（秒）",
  ]) {
    fireEvent.change(screen.getByLabelText(name), { target: { value: "" } });
  }
  fireEvent.click(screen.getByRole("button", { name: "保存采集设置" }));
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "恢复不限制规则" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
  await waitFor(() => expect(patches()).toHaveLength(2));
  expect(JSON.parse(String(patches()[1][1]?.body))).toMatchObject({
    monthly_budget_fen: null,
    quality_min_likes: null,
    quality_duration_min_ms: null,
    quality_duration_max_ms: null,
  });
  vi.unstubAllGlobals();
  setAdminCsrfToken("");
});

it.each(["100.755", "0", "-1"])(
  "非法预算 %s 不提交，也不解除旧限额",
  async (input) => {
    setAdminCsrfToken("csrf-viral");
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      response({ ...controls, monthly_budget_fen: 10000 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection />);
    expect(await screen.findByLabelText("月度采集预算（元）")).toHaveValue(100);
    fireEvent.change(screen.getByLabelText("月度采集预算（元）"), {
      target: { value: input },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存采集设置" }));
    fireEvent.change(screen.getByLabelText("操作原因"), {
      target: { value: "虚构输入校验" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    await screen.findAllByText(
      "月度预算请输入大于零、最多两位小数的金额；清空才会解除限额。",
    );
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "PATCH"),
    ).toHaveLength(0);
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  },
);
