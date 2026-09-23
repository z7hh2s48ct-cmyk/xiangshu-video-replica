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
  it("定时采集入口已关闭，仅展示历史运行记录", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response(controls)),
    );
    render(<ViralRuntimeSection />);

    expect(await screen.findByText(/定时采集已停用/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "暂停采集" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "恢复采集" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "添加关键词" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "保存采集设置" }),
    ).not.toBeInTheDocument();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    setAdminCsrfToken("");
  });

  it("展示内容池数量与历史任务只读状态", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response(controls)),
    );
    render(<ViralRuntimeSection />);

    expect(await screen.findByText(/任务状态：导入排队 2/)).toHaveTextContent(
      /刷新排队\s*1 \/ 执行 1 \/ 失败\s*0/,
    );
    expect(screen.getByText(/2026-09-07 10:00:00/)).toHaveTextContent(
      "抖音：内容池 24 条",
    );
    expect(screen.getByText(/历史最后采集 暂无/)).toHaveTextContent(
      "视频号：内容池 18 条",
    );
  });

  it("保留 Web 回退和存量任务所需的导入开关", async () => {
    setAdminCsrfToken("csrf-viral");
    const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
      response(
        init?.method === "PATCH"
          ? { ...controls, import_enabled: false }
          : controls,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ViralRuntimeSection />);

    fireEvent.click(await screen.findByRole("button", { name: "暂停导入" }));
    fireEvent.click(screen.getByRole("button", { name: "确认更新" }));
    expect(
      await screen.findByText("爆款视频导入开关已更新。"),
    ).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "PATCH",
    );
    expect(JSON.parse(String(patch?.[1]?.body))).toMatchObject({
      collection_enabled: false,
      import_enabled: false,
      reason: "更新爆款视频导入开关",
      confirm: true,
    });
  });
});
