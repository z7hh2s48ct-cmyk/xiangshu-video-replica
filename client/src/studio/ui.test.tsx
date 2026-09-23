import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { StudioAsset } from "./types";
import { Media } from "./ui";

/** 视频瓦片在拿不到封面时必须留下可见状态。
 *
 * `.video-preview` 的底色是 #101111，封面缺席时整块就是一个深色空框：用户分不
 * 清是坏了、在加载、还是这条素材本来就没有内容——这正是「视频预览看不到图片」
 * 反复被报上来的表象。服务端侧已经改成按需派生首帧，这里是抽帧确实失败时的
 * 最后一道兜底。
 */
describe("Media 视频瓦片的空状态", () => {
  const video: StudioAsset = {
    id: "v1",
    name: "样片",
    kind: "video",
    group: "项目素材",
    source: "素材库",
    saved: true,
  };

  it("没有封面时给出可见标记，而不是纯黑框", () => {
    render(
      <Media
        asset={{ ...video, url: "https://api.example/v1.mp4" }}
        alt="样片"
      />,
    );
    expect(screen.getByText("视频预览图")).toBeInTheDocument();
  });

  it("有时长就显示时长", () => {
    render(
      <Media
        asset={{
          ...video,
          url: "https://api.example/v1.mp4",
          duration: "00:15",
        }}
        alt="样片"
      />,
    );
    expect(screen.getByText("00:15")).toBeInTheDocument();
  });

  it("封面已就位时不叠加占位标记", () => {
    render(
      <Media
        asset={{
          ...video,
          url: "https://api.example/v1.mp4",
          poster: "https://api.example/v1.thumb.jpg",
        }}
        alt="样片"
      />,
    );
    expect(screen.queryByText("视频预览图")).not.toBeInTheDocument();
  });
});
