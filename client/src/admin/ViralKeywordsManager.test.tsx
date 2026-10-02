import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setAdminCsrfToken } from "../api";
import { ViralKeywordsManager } from "./ViralKeywordsManager";

const keyword = {
  platform: "douyin",
  category: "庭院案例",
  keyword: "农村庭院",
  enabled: true,
  limit: 3,
};
const page = {
  items: [
    {
      ...keyword,
      collected: 2,
      featured: 1,
      details: 2,
      copies: 1,
      uses: 3,
      effect: "unknown",
      lastRun: null,
    },
  ],
  categories: ["庭院案例", "装修案例"],
  from: "2026-09-02",
  to: "2026-10-01",
  measurementStartedAt: "2026-09-20T00:00:00+08:00",
  coverageComplete: false,
  rule: "实际成功使用，视频去重",
  effectRule: "覆盖不足为未知",
};
function response(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}
function reason() {
  fireEvent.change(screen.getByLabelText("操作原因"), {
    target: { value: "核对实际需求备货" },
  });
  fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
}
afterEach(() => {
  vi.unstubAllGlobals();
  setAdminCsrfToken("");
});

describe("ViralKeywordsManager", () => {
  it("只读展示真实指标与历史未知，并隐藏写入入口", async () => {
    const fetch = vi.fn(() => response(page));
    vi.stubGlobal("fetch", fetch);
    render(<ViralKeywordsManager readOnly />);
    expect(await screen.findByText("农村庭院")).toBeInTheDocument();
    expect(screen.getByText("覆盖不足为未知")).toBeInTheDocument();
    expect(screen.getByText("尚无执行记录")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "新增 / 批量粘贴" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "编辑农村庭院" }),
    ).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("批量粘贴去空行和重复词，保存分类、独立数量及审计原因", async () => {
    setAdminCsrfToken("csrf-keyword");
    const fetch = vi.fn((_url: string, init?: RequestInit) =>
      response(
        init?.method === "POST" ? { added: 2, duplicates: 0, total: 3 } : page,
      ),
    );
    vi.stubGlobal("fetch", fetch);
    render(<ViralKeywordsManager />);
    fireEvent.click(
      await screen.findByRole("button", { name: "新增 / 批量粘贴" }),
    );
    fireEvent.change(screen.getByLabelText("关键词（每行一个）"), {
      target: { value: " 老房改造 \n\n庭院改造\n老房改造 " },
    });
    fireEvent.change(screen.getByLabelText("分类"), {
      target: { value: "装修案例" },
    });
    fireEvent.change(screen.getByLabelText("每次条数（留空使用默认）"), {
      target: { value: "4" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(await screen.findByText("请填写操作原因")).toBeInTheDocument();
    expect(
      fetch.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(0);
    reason();
    await screen.findByText("新增2个关键词，跳过1个重复词。下一轮采集生效。");
    const call = fetch.mock.calls.find(([, init]) => init?.method === "POST");
    expect(call?.[0]).toContain("/api/control/viral/keywords/batch");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      keywords: ["老房改造", "庭院改造"].map((word) => ({
        ...keyword,
        category: "装修案例",
        keyword: word,
        limit: 4,
      })),
      reason: "核对实际需求备货",
      confirm: true,
    });
  });
  it("独立暂停仅提交目标配置，保留数量，不发送指标", async () => {
    setAdminCsrfToken("csrf-keyword");
    const fetch = vi.fn((_url: string, init?: RequestInit) =>
      response(init?.method === "PATCH" ? { updated: true } : page),
    );
    vi.stubGlobal("fetch", fetch);
    render(<ViralKeywordsManager />);
    fireEvent.click(
      await screen.findByRole("button", { name: "暂停农村庭院" }),
    );
    reason();
    await waitFor(() =>
      expect(
        fetch.mock.calls.some(([, init]) => init?.method === "PATCH"),
      ).toBe(true),
    );
    const call = fetch.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({
      expected: keyword,
      replacement: { ...keyword, enabled: false },
      reason: "核对实际需求备货",
      confirm: true,
    });
  });
  it("并发冲突保留编辑草稿与幂等键，允许操作者检查后重试", async () => {
    setAdminCsrfToken("csrf-keyword");
    const fetch = vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "PATCH"
        ? response(
            {
              detail: {
                code: "VIRAL_KEYWORD_CONFLICT",
                message: "词条已由其他管理员修改",
              },
            },
            409,
          )
        : response(page),
    );
    vi.stubGlobal("fetch", fetch);
    render(<ViralKeywordsManager />);
    fireEvent.click(
      await screen.findByRole("button", { name: "编辑农村庭院" }),
    );
    fireEvent.change(screen.getByLabelText("关键词"), {
      target: { value: "我保留的草稿" },
    });
    reason();
    await screen.findAllByText(/词条已由其他管理员修改/);
    expect(screen.getByLabelText("关键词")).toHaveValue("我保留的草稿");
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    await waitFor(() =>
      expect(
        fetch.mock.calls.filter(([, init]) => init?.method === "PATCH"),
      ).toHaveLength(2),
    );
    const calls = fetch.mock.calls.filter(
      ([, init]) => init?.method === "PATCH",
    );
    expect(new Headers(calls[0][1]?.headers).get("Idempotency-Key")).toBe(
      new Headers(calls[1][1]?.headers).get("Idempotency-Key"),
    );
    expect(JSON.parse(String(calls[0][1]?.body)).expected).toEqual(keyword);
  });
});
