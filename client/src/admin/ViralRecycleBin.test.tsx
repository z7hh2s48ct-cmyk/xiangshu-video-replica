import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  listViralRecycleBin,
  restoreViralVideo,
  type ViralRecycleVideo,
} from "../api.admin";
import { ViralRecycleBin } from "./ViralRecycleBin";

vi.mock("../api.admin", () => ({
  listViralRecycleBin: vi.fn(),
  restoreViralVideo: vi.fn(),
  adminActivationErrorMessage: (_: unknown, fallback: string) => fallback,
}));
const video = {
  platform: "douyin",
  video_id: "opaque/id",
  title: "回收样例",
  author: "作者",
  deleted_at: "2026-10-01T12:00:00Z",
  restore_before: "2026-10-31T12:00:00Z",
  restorable: true,
} as ViralRecycleVideo;
describe("视频回收站", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listViralRecycleBin).mockResolvedValue({
      items: [video],
      total: 1,
      rule: "30天",
    });
  });
  it("恢复必须真实原因，一次确认恢复后不自动上首页", async () => {
    vi.mocked(restoreViralVideo).mockResolvedValue({});
    render(<ViralRecycleBin readOnly={false} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "恢复到视频库" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认恢复" }));
    expect(restoreViralVideo).not.toHaveBeenCalled();
    fireEvent.change(screen.getByRole("textbox", { name: "操作原因" }), {
      target: { value: "误删，恢复供运营重新选择" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认恢复" }));
    await waitFor(() => expect(restoreViralVideo).toHaveBeenCalledTimes(1));
    expect(restoreViralVideo).toHaveBeenCalledWith(
      video,
      "误删，恢复供运营重新选择",
      expect.any(String),
    );
    expect(
      await screen.findByText("视频已恢复到库中，不会自动上首页。"),
    ).toBeInTheDocument();
  });
  it("到期项不可恢复，审计员没有写入口", async () => {
    vi.mocked(listViralRecycleBin).mockResolvedValue({
      items: [{ ...video, restorable: false }],
      total: 1,
      rule: "30天",
    });
    const result = render(<ViralRecycleBin readOnly={false} />);
    expect(
      await screen.findByRole("button", { name: "恢复到视频库" }),
    ).toBeDisabled();
    result.unmount();
    render(<ViralRecycleBin readOnly />);
    await screen.findByText("回收样例");
    expect(screen.queryByRole("button", { name: "恢复到视频库" })).toBeNull();
    expect(restoreViralVideo).not.toHaveBeenCalled();
  });
});
