import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "./api";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

vi.mock("./api", () => ({ uploadSimpleCharacter: vi.fn() }));
beforeEach(() => vi.clearAllMocks());

function prepare(
  file = new File(["image"], "portrait.png", { type: "image/png" }),
) {
  render(<SimpleCharacterUpload onCreated={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("人物名称"), {
    target: { value: "测试人物" },
  });
  fireEvent.change(screen.getByLabelText("授权图片"), {
    target: { files: [file] },
  });
  fireEvent.click(screen.getByRole("button", { name: "一键生成五视图拼合图" }));
}

function confirmAuthorization() {
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我已阅读并确认以上图像授权声明" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "确认授权并生成" }));
}

it.each([
  ["scan.tif", "", "image/tiff"],
  ["photo.AVIF", "", "image/avif"],
  ["legacy.bmp", "image/x-ms-bmp", "image/bmp"],
  ["meme.gif", "image/gif", "image/gif"],
])("uploads %s (browser type %j) as %s", (name, browserType, expectedType) => {
  vi.mocked(api.uploadSimpleCharacter).mockReturnValue(new Promise(() => {}));
  prepare(new File(["image"], name, { type: browserType }));
  confirmAuthorization();
  const uploaded = vi.mocked(api.uploadSimpleCharacter).mock.calls[0][1];
  expect(uploaded.name).toBe(name);
  expect(uploaded.type).toBe(expectedType);
});

it("rejects formats outside the supported list before authorization", () => {
  prepare(new File(["image"], "IMG_0001.heic", { type: "image/heic" }));
  expect(screen.queryByRole("dialog", { name: "人物图像使用授权" })).toBeNull();
  expect(
    screen.getByText(
      "请选择不超过 10MB 的 PNG、JPEG、WebP、GIF、BMP、TIFF 或 AVIF 图片。",
    ),
  ).toBeTruthy();
  expect(api.uploadSimpleCharacter).not.toHaveBeenCalled();
});

it("does not upload until the user explicitly confirms image authorization", () => {
  vi.mocked(api.uploadSimpleCharacter).mockReturnValue(new Promise(() => {}));
  prepare();
  expect(screen.getByRole("dialog", { name: "人物图像使用授权" })).toBeTruthy();
  expect(api.uploadSimpleCharacter).not.toHaveBeenCalled();
  const confirm = screen.getByRole("button", { name: "确认授权并生成" });
  expect((confirm as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我已阅读并确认以上图像授权声明" }),
  );
  fireEvent.click(confirm);
  expect(api.uploadSimpleCharacter).toHaveBeenCalledWith(
    null,
    expect.any(File),
    "测试人物",
    "",
    "2026-09-14-v1",
  );
});

it("cancel and a new submit require a fresh unchecked confirmation", () => {
  prepare();
  fireEvent.click(
    screen.getByRole("checkbox", { name: "我已阅读并确认以上图像授权声明" }),
  );
  fireEvent.click(screen.getByRole("button", { name: "取消上传" }));
  expect(api.uploadSimpleCharacter).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "一键生成五视图拼合图" }));
  expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(
    false,
  );
});
