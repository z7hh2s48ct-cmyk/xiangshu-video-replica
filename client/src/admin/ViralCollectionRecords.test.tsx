import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ViralCollectionRecords } from "./ViralCollectionRecords";

afterEach(() => vi.unstubAllGlobals());
it("本轮任务通过真实批次身份读取，队列复用不冒用下一轮状态", async () => {
  const fetchMock = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    json: async () =>
      url.includes("/old-batch/tasks")
        ? {
            related_task_id: "queue-original",
            current_queue: null,
            note: "队列已复用",
            keywords: [
              {
                platform: "douyin",
                keyword: "旧批次词",
                status: "FAILED",
                video_count: null,
                attempt_count: 2,
                failure_category: "服务响应超时",
                advice: "先核对费用再重试",
              },
            ],
          }
        : {
            total: 1,
            countingRule: "独立记录",
            items: [
              {
                id: "old-batch",
                platform: "douyin",
                trigger_kind: "manual",
                created_at: "2026-10-01T01:00:00Z",
                started_at: null,
                completed_at: null,
                run_status: "FAILED",
                keywords: ["旧批次词"],
                videos: 0,
                new_count: 0,
                ready: 0,
                calls: 0,
                known_cost_fen: 0,
                cost_fen: null,
                unknown_cost_count: 1,
                pending_cost_count: 0,
                failure_reason: "中断",
                advice: "检查费用",
              },
            ],
          },
  }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ViralCollectionRecords />);
  fireEvent.click(await screen.findByRole("button", { name: "查看本轮任务" }));
  expect(
    await screen.findByText("关联任务 queue-original"),
  ).toBeInTheDocument();
  expect(screen.getByText(/队列已复用，本轮结果/)).toBeInTheDocument();
  expect(
    screen.getByText(/服务响应超时；先核对费用再重试/),
  ).toBeInTheDocument();
  expect(
    fetchMock.mock.calls.some(([url]) => url.includes("/old-batch/tasks")),
  ).toBe(true);
});
it("采集设置记录区保留触发方式、真实新增、未知成本并服务端分页", async () => {
  const fetchMock = vi.fn(async (url: string) => ({
    ok: true,
    status: 200,
    json: async () => ({
      total: 21,
      countingRule: "真实执行记录，历史新增未知",
      items: [
        {
          id: url.includes("offset=20") ? "manual-run" : "realtime-run",
          platform: "douyin",
          trigger_kind: url.includes("offset=20") ? "manual" : "realtime",
          created_at: "2026-10-01T01:00:00Z",
          started_at: "2026-10-01T01:00:01Z",
          completed_at: "2026-10-01T01:00:02Z",
          keywords: ["隔离记录"],
          run_status: "FAILED",
          videos: 3,
          new_count: null,
          ready: 1,
          succeeded: 1,
          failed: 1,
          failed_video_count: 2,
          calls: 4,
          known_cost_fen: "8.5",
          cost_fen: null,
          unknown_cost_count: 1,
          pending_cost_count: 0,
          failure_reason: "原因待核对",
          advice: "请检查服务配置",
        },
      ],
    }),
  }));
  vi.stubGlobal("fetch", fetchMock);
  render(<ViralCollectionRecords />);
  expect(
    await screen.findByRole("cell", { name: /后台实时搜索/ }),
  ).toBeInTheDocument();
  expect(screen.getByText(/实际新增 历史未知/)).toBeInTheDocument();
  expect(screen.getByText(/接口总成本 待核对/)).toBeInTheDocument();
  expect(screen.getByText("请检查服务配置")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByRole("cell", { name: /手动采集/ });
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(([url]) => url.includes("offset=20")),
    ).toBe(true),
  );
});

it("日期与列表页外的记录可直接定位，失败视频链接按平台和精确ID打开详情", async () => {
  const fetchMock = vi.fn(
    async (url: string) =>
      new Response(
        JSON.stringify(
          url.includes("/tasks")
            ? {
                related_task_id: "stable-task",
                current_queue: null,
                keywords: [],
                videos: [
                  {
                    platform: "douyin",
                    video_id: "old/video=id",
                    title: "跨页失败视频",
                    status: "FAILED",
                    failure_category: "服务响应超时",
                    advice: "核对费用后重试",
                  },
                ],
                note: "逐轮结果保留",
              }
            : { items: [], total: 0, countingRule: "独立记录" },
        ),
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(<ViralCollectionRecords initialBatchId="historical-batch" />);
  await screen.findByText("跨页失败视频");
  expect(
    fetchMock.mock.calls.some(([url]) =>
      url.includes("historical-batch/tasks"),
    ),
  ).toBe(true);
  expect(
    screen.getByRole("link", { name: "查看视频与准备任务" }),
  ).toHaveAttribute(
    "href",
    "#admin/viralVideos?platform=douyin&videoId=old%2Fvideo%3Did",
  );
  expect(screen.getByText(/服务响应超时/)).toHaveTextContent("核对费用后重试");
});
