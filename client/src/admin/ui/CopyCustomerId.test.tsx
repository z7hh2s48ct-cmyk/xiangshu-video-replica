import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CopyCustomerId } from "./CopyCustomerId";

afterEach(() => vi.unstubAllGlobals());
it("copies by icon and double click, and reports the result", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  render(<CopyCustomerId value="customer-123" />);
  fireEvent.click(screen.getByRole("button", { name: "复制客户 ID" }));
  expect(await screen.findByRole("status")).toHaveTextContent("客户 ID 已复制");
  expect(writeText).toHaveBeenLastCalledWith("customer-123");
  fireEvent.doubleClick(screen.getByText("customer-123"));
  expect(writeText).toHaveBeenCalledTimes(2);
});
it("reports clipboard failure without claiming success", async () => {
  vi.stubGlobal("navigator", {
    clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
  });
  render(<CopyCustomerId value="customer-123" />);
  fireEvent.click(screen.getByRole("button", { name: "复制客户 ID" }));
  expect(await screen.findByRole("status")).toHaveTextContent("复制失败");
});
