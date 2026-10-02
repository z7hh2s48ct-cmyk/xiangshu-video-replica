import { act, fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { adminRead } from "../api.admin";
import { CustomerOverview } from "./CustomerOverview";

vi.mock("../api.admin", () => ({ adminRead: vi.fn() }));
const empty = {
  daily_consumption: Array.from({ length: 30 }, (_, i) => ({
    day: `2026-09-${String(i + 1).padStart(2, "0")}`,
    credits: 0,
  })),
  timeline: [],
  basis: "原账本生成扣费积分",
};

test("shows all thirty days and five mixed factual events without exposing identifiers", async () => {
  vi.mocked(adminRead).mockResolvedValue({
    ...empty,
    daily_consumption: empty.daily_consumption.map((day, i) => ({
      ...day,
      credits: i === 29 ? 7 : 0,
    })),
    timeline: [
      "recharge",
      "generation",
      "adjustment",
      "login",
      "generation",
    ].map((kind, i) => ({
      kind,
      event_id: `opaque-${i}`,
      label: `动态 ${kind} ${i}`,
      created_at: "2026-09-30T10:00:00Z",
      credits: i === 0 ? 10 : null,
    })),
  });
  render(<CustomerOverview userId="a/opaque" />);
  const chart = await screen.findByRole("img", {
    name: "近30天积分消耗，共 7 积分",
  });
  expect(chart.querySelectorAll("rect")).toHaveLength(30);
  expect(screen.getAllByRole("listitem")).toHaveLength(5);
  expect(screen.queryByText(/opaque-/)).toBeNull();
  expect(adminRead).toHaveBeenCalledWith(
    "/api/control/customers/a%2Fopaque/overview",
    "概览加载失败",
  );
});

test("failed read can retry and the empty state keeps zero consumption visible", async () => {
  vi.mocked(adminRead)
    .mockRejectedValueOnce(new Error("隔离读取失败"))
    .mockResolvedValue(empty);
  render(<CustomerOverview userId="customer" />);
  fireEvent.click(await screen.findByRole("button", { name: "重试概览" }));
  expect(await screen.findByText("近30天暂无消耗。")).toBeInTheDocument();
  expect(
    screen.getByText("暂无充值、生成、调整或登录记录。"),
  ).toBeInTheDocument();
  expect(screen.queryByText("隔离读取失败")).toBeNull();
});

test("changing customers discards an older request that finishes last", async () => {
  let finish!: (data: typeof empty) => void;
  vi.mocked(adminRead)
    .mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    )
    .mockResolvedValue({ ...empty, basis: "客户B账本" });
  const view = render(<CustomerOverview userId="A" />);
  await act(async () => {});
  view.rerender(<CustomerOverview userId="B" />);
  expect(await screen.findByText("客户B账本")).toBeInTheDocument();
  await act(async () => finish({ ...empty, basis: "客户A账本" }));
  expect(screen.queryByText("客户A账本")).toBeNull();
  expect(screen.getByText("客户B账本")).toBeInTheDocument();
});
