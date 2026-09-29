import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../api.admin";
import { RecordThumbnail } from "./RecordThumbnail";

vi.mock("../api.admin", () => ({
  getAdminGenerationRecordThumbnail: vi.fn(),
}));

function mockThumbnail(url: string | null) {
  vi.mocked(adminApi.getAdminGenerationRecordThumbnail).mockResolvedValue({
    record_type: "VIDEO",
    record_id: "rec-1",
    url,
    expires_in_seconds: 604800,
  });
}

describe("RecordThumbnail", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("does not sign anything while the detail is closed", () => {
    // 未展开就不该签发：签发一次写一条审计，「看了哪张图」必须来自真实的展开动作。
    render(
      <RecordThumbnail active={false} recordId="rec-1" recordType="VIDEO" />,
    );
    expect(adminApi.getAdminGenerationRecordThumbnail).not.toHaveBeenCalled();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows the signed thumbnail once the detail is opened", async () => {
    mockThumbnail("https://cos.example/thumb.jpg");
    render(<RecordThumbnail active recordId="rec-1" recordType="VIDEO" />);

    const image = await screen.findByRole("img", {
      name: "生成结果缩略图 rec-1",
    });
    expect(image).toHaveAttribute("src", "https://cos.example/thumb.jpg");
    expect(adminApi.getAdminGenerationRecordThumbnail).toHaveBeenCalledWith(
      "VIDEO",
      "rec-1",
    );
  });

  it("explains the absence instead of leaving a blank or a broken image", async () => {
    // 历史记录、本地盘存储、没有媒体的记录类型都会给空 url：那是"没有派生小图"，
    // 不是故障，页面上要说清。
    mockThumbnail(null);
    render(<RecordThumbnail active recordId="rec-1" recordType="VIDEO" />);

    expect(await screen.findByText(/无缩略图/)).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("keeps the failure visible without breaking the detail panel", async () => {
    vi.mocked(adminApi.getAdminGenerationRecordThumbnail).mockRejectedValue(
      new Error("boom"),
    );
    render(<RecordThumbnail active recordId="rec-1" recordType="VIDEO" />);

    expect(await screen.findByText(/缩略图读取失败/)).toBeInTheDocument();
  });
});
