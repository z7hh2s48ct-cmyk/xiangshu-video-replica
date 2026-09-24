import { fireEvent, render, screen } from "@testing-library/react";
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
  it("保存每周采集关键词及数量上限", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      response(controls),
    );
    vi.stubGlobal("fetch", fetchMock);
    setAdminCsrfToken("csrf-viral");
    render(<ViralRuntimeSection />);
    fireEvent.click(await screen.findByRole("button", { name: "添加关键词" }));
    fireEvent.change(screen.getByLabelText("分类 1"), {
      target: { value: "庭院案例" },
    });
    fireEvent.change(screen.getByLabelText("关键词 1"), {
      target: { value: "农村庭院" },
    });
    fireEvent.change(screen.getByLabelText("每个关键词最多采集"), {
      target: { value: "12" },
    });
    fireEvent.change(screen.getByLabelText("刷新周期"), {
      target: { value: "1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存采集设置" }));
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认更新" }));
    await screen.findByText("定时采集设置已更新。");
    const patch = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      keywords: [
        { platform: "douyin", category: "庭院案例", keyword: "农村庭院" },
      ],
      per_keyword_limit: 12,
      collection_interval_days: 1,
      reason: "更新爆款视频采集设置",
      confirm: true,
    });
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("展示平台缓存、导入队列与运行开关", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response(controls)),
    );
    render(<ViralRuntimeSection />);

    expect(await screen.findByText(/导入任务：排队 2/)).toBeInTheDocument();
    expect(screen.getByText(/数据源：已配置/)).toHaveTextContent(
      /刷新任务：\s*排队 1 \/ 执行 1 \/ 失败 0/,
    );
    expect(screen.getByText(/2026-09-07 10:00:00/)).toHaveTextContent(
      "抖音：已缓存 24条",
    );
    expect(screen.getByText(/最后采集 暂无/)).toHaveTextContent(
      "视频号：已缓存 18条",
    );
  });

  it("直接确认暂停采集并自动记录操作说明", async () => {
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
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认更新" }));

    expect(
      await screen.findByText("爆款视频运行开关已更新。"),
    ).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(patch?.[0]).toContain("/api/control/settings/viral");
    expect(JSON.parse(String(patch?.[1]?.body))).toEqual({
      collection_enabled: false,
      import_enabled: true,
      confirm: true,
      reason: "更新爆款视频采集开关",
    });
  });
});
