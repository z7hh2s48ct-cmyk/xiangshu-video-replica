import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "./api";
import { SimpleCharacterUpload } from "./SimpleCharacterUpload";

vi.mock("./api", () => ({ uploadSimpleCharacter: vi.fn() }));
beforeEach(() => vi.clearAllMocks());

function prepare() {
  render(<SimpleCharacterUpload onCreated={vi.fn()} />);
  fireEvent.change(screen.getByLabelText("人物名称"), {
    target: { value: "测试人物" },
  });
  fireEvent.change(screen.getByLabelText("授权图片"), {
    target: {
      files: [new File(["image"], "portrait.png", { type: "image/png" })],
    },
  });
  fireEvent.click(screen.getByRole("button", { name: "一键生成五视图拼合图" }));
}

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
