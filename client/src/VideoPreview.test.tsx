import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Media } from "./studio/ui";
import { VideoPreview } from "./VideoPreview";

afterEach(() => vi.restoreAllMocks());

describe("VideoPreview", () => {
  it("lets fixed thumbnail cards control the preview dimensions", () => {
    const { container } = render(
      <Media alt="素材预览" fitContainer aspectRatio="adaptive" />,
    );

    expect(container.firstElementChild).not.toHaveAttribute("style");
  });

  it("does not label old decoded dimensions as a newly selected source", () => {
    vi.spyOn(HTMLVideoElement.prototype, "readyState", "get").mockReturnValue(
      4,
    );
    vi.spyOn(HTMLVideoElement.prototype, "videoWidth", "get").mockReturnValue(
      1920,
    );
    const height = vi
      .spyOn(HTMLVideoElement.prototype, "videoHeight", "get")
      .mockReturnValue(1080);
    const currentSrc = vi
      .spyOn(HTMLVideoElement.prototype, "currentSrc", "get")
      .mockReturnValue(new URL("/previous.mp4", document.baseURI).href);
    const onAspectRatioChange = vi.fn();
    const { container, rerender } = render(
      <VideoPreview
        src="/previous.mp4"
        frameRatio="adaptive"
        onAspectRatioChange={onAspectRatioChange}
      />,
    );
    onAspectRatioChange.mockClear();
    rerender(
      <VideoPreview
        src="/next.mp4"
        frameRatio="adaptive"
        onAspectRatioChange={onAspectRatioChange}
      />,
    );
    expect(onAspectRatioChange).not.toHaveBeenCalled();
    expect(container.firstElementChild).toHaveStyle({
      aspectRatio: String(9 / 16),
    });
    currentSrc.mockReturnValue(new URL("/next.mp4", document.baseURI).href);
    height.mockReturnValue(1920);
    const video = container.querySelector("video");
    if (!video) throw new Error("video preview missing");
    fireEvent.loadedMetadata(video);
    expect(onAspectRatioChange).toHaveBeenLastCalledWith(1);
    expect(container.firstElementChild).toHaveStyle({ aspectRatio: "1" });
  });

  it("recovers video dimensions already loaded before the metadata handler", () => {
    vi.spyOn(HTMLVideoElement.prototype, "currentSrc", "get").mockReturnValue(
      new URL("/cached.mp4", document.baseURI).href,
    );
    vi.spyOn(HTMLVideoElement.prototype, "readyState", "get").mockReturnValue(
      4,
    );
    vi.spyOn(HTMLVideoElement.prototype, "videoWidth", "get").mockReturnValue(
      1920,
    );
    vi.spyOn(HTMLVideoElement.prototype, "videoHeight", "get").mockReturnValue(
      1080,
    );
    const onAspectRatioChange = vi.fn();
    const { container } = render(
      <VideoPreview
        src="/cached.mp4"
        frameRatio="adaptive"
        onAspectRatioChange={onAspectRatioChange}
      />,
    );
    expect(container.firstElementChild).toHaveStyle({
      aspectRatio: String(1920 / 1080),
    });
    expect(onAspectRatioChange).toHaveBeenCalledWith(1920 / 1080);
  });

  it("reports native video and poster ratios for the surrounding layout", () => {
    const onAspectRatioChange = vi.fn();
    const { container, rerender } = render(
      <Media
        alt="参考视频"
        asset={{
          id: "video",
          name: "原片",
          kind: "video",
          url: "/portrait.mp4",
          group: "项目",
          source: "上传",
          saved: true,
        }}
        aspectRatio="adaptive"
        onAspectRatioChange={onAspectRatioChange}
      />,
    );
    const video = container.querySelector("video");
    if (!video) throw new Error("video preview missing");
    Object.defineProperties(video, {
      videoWidth: { value: 1080 },
      videoHeight: { value: 1920 },
    });
    fireEvent.loadedMetadata(video);
    expect(onAspectRatioChange).toHaveBeenLastCalledWith(1080 / 1920);
    rerender(
      <Media
        alt="参考视频"
        asset={{
          id: "poster",
          name: "原片",
          kind: "image",
          url: "/landscape.jpg",
          group: "项目",
          source: "上传",
          saved: true,
        }}
        aspectRatio="adaptive"
        onAspectRatioChange={onAspectRatioChange}
      />,
    );
    const poster = screen.getByRole("img", { name: "参考视频" });
    Object.defineProperties(poster, {
      naturalWidth: { value: 1920 },
      naturalHeight: { value: 1080 },
    });
    fireEvent.load(poster);
    expect(onAspectRatioChange).toHaveBeenLastCalledWith(1920 / 1080);
  });

  it.each([
    [1600, 900],
    [900, 1600],
    [800, 800],
  ])(
    "fits image placeholders to uploaded dimensions %s × %s",
    (width, height) => {
      const { container, rerender } = render(<Media alt="上传图片" />);
      expect(container.firstElementChild).toHaveStyle({
        aspectRatio: "0.5625",
      });
      rerender(
        <Media
          alt="上传图片"
          asset={{
            id: "upload",
            name: "照片",
            kind: "image",
            url: "/upload.jpg",
            group: "上传",
            source: "上传",
            saved: true,
          }}
        />,
      );
      const image = screen.getByRole("img", { name: "上传图片" });
      Object.defineProperties(image, {
        naturalWidth: { value: width },
        naturalHeight: { value: height },
      });
      fireEvent.load(image);
      expect(container.firstElementChild).toHaveStyle({
        aspectRatio: String(width / height),
      });
      rerender(<Media alt="上传图片" />);
      expect(container.firstElementChild).toHaveStyle({
        aspectRatio: "0.5625",
      });
    },
  );

  it("uses the selected frame ratio for loaded and empty images", () => {
    const { container, rerender } = render(
      <VideoPreview frameRatio="9:16" alt="首帧" />,
    );
    expect(container.firstElementChild).toHaveStyle({ aspectRatio: "0.5625" });
    rerender(
      <VideoPreview frameRatio="16:9" poster="/square.jpg" alt="首帧" />,
    );
    const image = screen.getByRole("img", { name: "首帧" });
    Object.defineProperties(image, {
      naturalWidth: { value: 800 },
      naturalHeight: { value: 800 },
    });
    fireEvent.load(image);
    expect(container.firstElementChild).toHaveStyle({
      aspectRatio: String(16 / 9),
    });
    rerender(<VideoPreview frameRatio="1:1" poster="/square.jpg" alt="首帧" />);
    expect(container.firstElementChild).toHaveStyle({ aspectRatio: "1" });
    fireEvent.error(image);
    expect(container.firstElementChild).toHaveStyle({ aspectRatio: "1" });
  });

  it("uses native image dimensions by default and resets them for a new source", () => {
    const { container, rerender } = render(
      <VideoPreview poster="/wide.jpg" alt="自动预览" />,
    );
    const image = screen.getByRole("img", { name: "自动预览" });
    Object.defineProperties(image, {
      naturalWidth: { value: 1600 },
      naturalHeight: { value: 900 },
    });
    fireEvent.load(image);
    expect(container.firstElementChild).toHaveStyle({
      aspectRatio: String(16 / 9),
    });
    rerender(<VideoPreview poster="/new.jpg" alt="自动预览" />);
    expect(container.firstElementChild).toHaveStyle({ aspectRatio: "0.5625" });
  });

  it("keeps the player and external ref stable when a signed URL refreshes", () => {
    const ref = createRef<HTMLVideoElement>();
    const { rerender } = render(
      <VideoPreview
        src="/video?signature=old"
        ref={ref}
        aria-label="可刷新成片"
      />,
    );
    const player = ref.current;
    rerender(
      <VideoPreview
        src="/video?signature=new"
        ref={ref}
        aria-label="可刷新成片"
      />,
    );
    expect(ref.current).toBe(player);
    expect(player).toHaveAttribute("src", "/video?signature=new");
  });

  it("cancels background frame callbacks on pause and unmount", () => {
    const ref = createRef<HTMLVideoElement>();
    const { unmount } = render(<VideoPreview src="/wide.mp4" ref={ref} />);
    const video = ref.current as HTMLVideoElement;
    const request = vi.fn().mockReturnValue(7);
    const cancel = vi.fn();
    Object.defineProperties(video, {
      paused: { value: false },
      requestVideoFrameCallback: { value: request },
      cancelVideoFrameCallback: { value: cancel },
    });
    fireEvent.playing(video);
    expect(request).toHaveBeenCalledOnce();
    fireEvent.pause(video);
    expect(cancel).toHaveBeenCalledWith(7);
    fireEvent.playing(video);
    unmount();
    expect(cancel).toHaveBeenCalledTimes(2);
  });

  it("does not render an invisible blurred copy for exact portrait media", () => {
    const draw = vi.spyOn(HTMLCanvasElement.prototype, "getContext");
    const ref = createRef<HTMLVideoElement>();
    const { container } = render(
      <VideoPreview src="/portrait.mp4" ref={ref} />,
    );
    Object.defineProperties(ref.current, {
      videoWidth: { value: 1080 },
      videoHeight: { value: 1920 },
      readyState: { value: 2 },
    });
    fireEvent.loadedData(ref.current as HTMLVideoElement);
    expect(draw).not.toHaveBeenCalled();
    expect(container.querySelector("canvas")).toHaveAttribute("hidden");
  });
  it("uses one controllable video and decorates it without another audio stream", () => {
    const onPlay = vi.fn();
    const ref = createRef<HTMLVideoElement>();
    const { container } = render(
      <VideoPreview
        src="/landscape.mp4"
        poster="/poster.jpg"
        aria-label="成片"
        controls
        ref={ref}
        onPlay={onPlay}
      />,
    );
    const video = screen.getByLabelText("成片");
    expect(container.querySelectorAll("video")).toHaveLength(1);
    expect(ref.current).toBe(video);
    expect(video).toHaveAttribute("controls");
    expect(video).toHaveAttribute("playsinline");
    expect(video).not.toHaveAttribute("autoplay");
    fireEvent.play(video);
    expect(onPlay).toHaveBeenCalledOnce();
    expect(container.querySelector("canvas")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  it("shows one accessible poster, reports a broken source once, and recovers when replaced", () => {
    const onError = vi.fn();
    const { rerender } = render(
      <VideoPreview
        poster="/wide.jpg"
        alt="横屏海报"
        onPosterError={onError}
      />,
    );
    expect(screen.getAllByRole("img")).toHaveLength(1);
    fireEvent.error(screen.getByRole("img", { name: "横屏海报" }));
    expect(onError).toHaveBeenCalledOnce();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    rerender(
      <VideoPreview
        poster="/tall.jpg"
        alt="竖屏海报"
        onPosterError={onError}
      />,
    );
    expect(screen.getByRole("img", { name: "竖屏海报" })).toHaveAttribute(
      "src",
      "/tall.jpg",
    );
  });

  it("draws the current landscape frame for the blurred background and stops after unmount", () => {
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage,
      clearRect: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    const ref = createRef<HTMLVideoElement>();
    const onError = vi.fn();
    const { container, unmount } = render(
      <VideoPreview
        src="/wide.mp4"
        aria-label="横屏播放"
        ref={ref}
        onError={onError}
      />,
    );
    const video = ref.current as HTMLVideoElement;
    Object.defineProperties(video, {
      videoWidth: { value: 1920 },
      videoHeight: { value: 1080 },
      readyState: { value: 2 },
    });
    fireEvent.loadedData(video);
    expect(drawImage).toHaveBeenCalledWith(video, 0, 0, 160, 90);
    expect(container.querySelector("canvas")).not.toHaveAttribute("hidden");
    fireEvent.seeked(video);
    expect(drawImage).toHaveBeenCalledTimes(2);
    fireEvent.error(video);
    expect(onError).toHaveBeenCalledOnce();
    unmount();
    fireEvent.seeked(video);
    expect(drawImage).toHaveBeenCalledTimes(2);
  });

  it("marks container-fitted previews so host-sized tiles keep the whole frame", () => {
    // 素材瓦片的盒子由宿主网格决定（显式行高），页面级 cover 规则会在视窗里
    // 裁掉竖屏素材的中段。fit 类让宿主场景的前景回到 contain（完整显示）。
    const { container } = render(
      <VideoPreview alt="素材预览" fitContainer poster="/thumb.jpg" />,
    );
    const root = container.firstElementChild;
    expect(root).toHaveClass("video-preview", "video-preview--fit");
    expect(root).not.toHaveAttribute("style");
  });

  it("leaves fixed-ratio previews outside the fit mode", () => {
    const { container } = render(
      <VideoPreview alt="固定比例" poster="/thumb.jpg" />,
    );
    expect(container.firstElementChild).not.toHaveClass("video-preview--fit");
  });

  it("keeps the duration badge on video tiles that already carry a poster", () => {
    const { container } = render(
      <Media
        alt="带封面的视频"
        fitContainer
        asset={{
          id: "video-with-poster",
          name: "同款视频",
          kind: "video",
          url: "/clip.mp4",
          poster: "/thumb.jpg",
          duration: "00:12",
          group: "项目",
          source: "上传",
          saved: true,
        }}
      />,
    );
    expect(
      container.querySelector(".studio-media-duration")?.textContent,
    ).toContain("00:12");
  });

  it("shows the duration badge on playable audio tiles", () => {
    const { container } = render(
      <Media
        alt="口播音频"
        fitContainer
        asset={{
          id: "audio-1",
          name: "口播",
          kind: "audio",
          url: "/oral.mp3",
          duration: "00:24",
          group: "口播成片",
          source: "素材库",
          saved: true,
        }}
      />,
    );
    expect(
      container.querySelector(".studio-media-duration")?.textContent,
    ).toContain("00:24");
  });
});
