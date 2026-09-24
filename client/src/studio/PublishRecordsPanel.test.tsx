import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { PublishRecordItem } from "../api";
import {
  isoFromLocalDateTimeInput,
  localDateTimeInputFromIso,
  validatePublishSchedule,
} from "./ContentPages";
import {
  isActivePublishRecord,
  PUBLISH_RECORDS_POLL_MS,
  PublishRecordsPanel,
} from "./PublishRecordsPanel";

const api = vi.hoisted(() => ({
  listPublishRecords: vi.fn(),
  cancelPublishRecord: vi.fn(),
  retryPublishRecord: vi.fn(),
  syncPublishRecord: vi.fn(),
  deletePublishRecord: vi.fn(),
}));
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  ...api,
}));

function record(overrides: Partial<PublishRecordItem>): PublishRecordItem {
  return {
    id: "rec",
    platform: "douyin",
    account_id: "acc",
    account_username: "张工说乡墅",
    video_asset_id: "video",
    cover_asset_id: null,
    title: "建房预算",
    description: "",
    tags: [],
    scheduled_at: null,
    status: "queued",
    delivery_mode: null,
    platform_item_id: null,
    platform_short_url: null,
    platform_status: null,
    stats: null,
    stats_synced_at: null,
    sync_requested: false,
    error_message: null,
    published_at: null,
    attempt_count: 0,
    created_at: "2026-09-17T02:00:00+00:00",
    updated_at: "2026-09-17T02:00:00+00:00",
    ...overrides,
  };
}

describe("PublishRecordsPanel", () => {
  beforeEach(() => {
    Object.values(api).forEach((mock) => {
      mock.mockReset();
    });
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("renders status, stats and the actions each state allows", async () => {
    api.listPublishRecords.mockResolvedValue([
      record({
        id: "q",
        status: "queued",
        scheduled_at: "2026-09-18T01:00:00Z",
      }),
      record({
        id: "p",
        status: "published",
        published_at: "2026-09-17T03:00:00Z",
        platform_short_url: "https://v.douyin.com/abc",
        delivery_mode: "browser",
        stats: { play_count: 120, like_count: 7, comment_count: "n/a" },
        stats_synced_at: "2026-09-17T04:00:00Z",
      }),
      record({ id: "f", status: "failed", error_message: "平台 5xx" }),
      record({ id: "c", status: "cancelled" }),
    ]);
    render(<PublishRecordsPanel />);
    await screen.findByText("排队中");
    expect(screen.getByText("已发布")).toBeInTheDocument();
    expect(screen.getByText("发布失败")).toBeInTheDocument();
    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(screen.getByText("平台 5xx")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "查看作品" })).toHaveAttribute(
      "href",
      "https://v.douyin.com/abc",
    );
    expect(screen.getByText(/播放 120 · 点赞 7/)).toBeInTheDocument();
    expect(screen.queryByText(/评论/)).toBeNull();
    expect(screen.getByText(/浏览器兜底/)).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "取消" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "重试" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "同步数据" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "删除记录" })).toHaveLength(3);
  });

  it("cancel / retry / delete update the list in place", async () => {
    api.listPublishRecords.mockResolvedValue([
      record({ id: "q", status: "queued" }),
      record({ id: "f", status: "failed" }),
    ]);
    api.cancelPublishRecord.mockResolvedValue(
      record({ id: "q", status: "cancelled" }),
    );
    api.retryPublishRecord.mockResolvedValue(
      record({ id: "f", status: "queued" }),
    );
    api.deletePublishRecord.mockResolvedValue(undefined);
    render(<PublishRecordsPanel />);
    await screen.findByText("排队中");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await waitFor(() =>
      expect(api.cancelPublishRecord).toHaveBeenCalledWith("q"),
    );
    await screen.findByText("已取消");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() =>
      expect(api.retryPublishRecord).toHaveBeenCalledWith("f"),
    );
    await waitFor(() => expect(screen.getAllByText("排队中")).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "删除记录" }));
    await waitFor(() =>
      expect(api.deletePublishRecord).toHaveBeenCalledWith("q"),
    );
    await waitFor(() => expect(screen.queryByText("已取消")).toBeNull());
  });

  it("polls only while a record is active and shows load errors with a retry", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    api.listPublishRecords
      .mockResolvedValueOnce([record({ id: "q", status: "publishing" })])
      .mockResolvedValueOnce([record({ id: "q", status: "published" })])
      .mockRejectedValueOnce(new Error("网络中断"))
      .mockResolvedValue([]);
    render(<PublishRecordsPanel />);
    await screen.findByText("发布中");
    expect(api.listPublishRecords).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(PUBLISH_RECORDS_POLL_MS);
    await screen.findByText("已发布");
    expect(api.listPublishRecords).toHaveBeenCalledTimes(2);
    // Terminal state: no further polling.
    await vi.advanceTimersByTimeAsync(PUBLISH_RECORDS_POLL_MS * 2);
    expect(api.listPublishRecords).toHaveBeenCalledTimes(2);
    // A pending sync makes the record active again, so polling resumes; the
    // failed poll surfaces its error without dropping the list.
    api.syncPublishRecord.mockResolvedValue(
      record({ id: "q", status: "published", sync_requested: true }),
    );
    fireEvent.click(screen.getByRole("button", { name: "同步数据" }));
    await waitFor(() =>
      expect(api.syncPublishRecord).toHaveBeenCalledWith("q"),
    );
    await vi.advanceTimersByTimeAsync(PUBLISH_RECORDS_POLL_MS);
    await screen.findByText("网络中断");
    expect(screen.getByText(/已发布 · 同步中/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await screen.findByText("暂无发布记录");
  });

  it("isActivePublishRecord covers queued, publishing and pending sync", () => {
    expect(isActivePublishRecord(record({ status: "queued" }))).toBe(true);
    expect(isActivePublishRecord(record({ status: "publishing" }))).toBe(true);
    expect(
      isActivePublishRecord(
        record({ status: "published", sync_requested: true }),
      ),
    ).toBe(true);
    expect(isActivePublishRecord(record({ status: "published" }))).toBe(false);
    expect(isActivePublishRecord(record({ status: "failed" }))).toBe(false);
  });
});

describe("publish schedule helpers", () => {
  it("round-trips a datetime-local value through ISO in the viewer's zone", () => {
    const iso = isoFromLocalDateTimeInput("2026-10-01T09:30");
    expect(iso).not.toBeNull();
    expect(new Date(iso as string).getHours()).toBe(9);
    expect(new Date(iso as string).getMinutes()).toBe(30);
    expect(localDateTimeInputFromIso(iso as string)).toBe("2026-10-01T09:30");
    expect(isoFromLocalDateTimeInput("")).toBeNull();
    expect(isoFromLocalDateTimeInput("not-a-date")).toBeNull();
    expect(localDateTimeInputFromIso(undefined)).toBe("");
  });

  it("validates lead time against the server's 2 minute / 30 day window", () => {
    const now = new Date("2026-09-17T00:00:00Z");
    expect(validatePublishSchedule(null, now)).toBeNull();
    expect(validatePublishSchedule("2026-09-17T00:01:00Z", now)).toBe(
      "定时发布至少需要提前 2 分钟。",
    );
    expect(validatePublishSchedule("2026-09-17T00:03:00Z", now)).toBeNull();
    expect(validatePublishSchedule("2026-10-20T00:00:00Z", now)).toBe(
      "定时发布最多只能提前 30 天。",
    );
    expect(validatePublishSchedule("garbage", now)).toBe(
      "请填写有效的定时时间。",
    );
  });
});
