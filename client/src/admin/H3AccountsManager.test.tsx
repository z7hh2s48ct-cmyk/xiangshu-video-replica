import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { adminRead, adminWrite } from "../api.admin";
import { H3AccountsManager } from "./H3AccountsManager";

vi.mock("../api.admin", () => ({ adminRead: vi.fn(), adminWrite: vi.fn() }));
const snapshot = {
  managed: true,
  total_concurrency: 18,
  accounts: [
    {
      id: "a",
      name: "账号 A",
      concurrency_limit: 6,
      enabled: true,
      configured: true,
      active_tasks: 2,
      version: 1,
    },
    {
      id: "b",
      name: "账号 B",
      concurrency_limit: 12,
      enabled: true,
      configured: true,
      active_tasks: 3,
      version: 1,
    },
  ],
};
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(adminRead).mockResolvedValue(snapshot);
});
describe("H3 account configuration", () => {
  it("shows independent limits and leaves new account entitlement blank", async () => {
    render(<H3AccountsManager />);
    expect(await screen.findByText("总并发 18")).toBeInTheDocument();
    expect(
      screen
        .getAllByLabelText("并发上限")
        .map((input) => (input as HTMLInputElement).value),
    ).toEqual(["6", "12"]);
    fireEvent.click(screen.getByRole("button", { name: "添加账号" }));
    expect(
      within(screen.getByRole("form", { name: "新增视频账号" })).getByLabelText(
        "并发上限",
      ),
    ).toHaveValue(null);
    expect(
      screen
        .getAllByLabelText("API Key")
        .every((input) => (input as HTMLInputElement).value === ""),
    ).toBe(true);
  });
  it("saves a custom limit with blank secret preserved and stable retry", async () => {
    vi.mocked(adminWrite)
      .mockRejectedValueOnce(new Error("暂时不可用"))
      .mockResolvedValueOnce(snapshot);
    render(<H3AccountsManager />);
    const row = within(await screen.findByRole("form", { name: "账号 A配置" }));
    fireEvent.change(row.getByLabelText("并发上限"), {
      target: { value: "23" },
    });
    fireEvent.click(row.getByRole("button", { name: "保存账号" }));
    await screen.findByText("暂时不可用");
    fireEvent.click(row.getByRole("button", { name: "保存账号" }));
    await waitFor(() => expect(adminWrite).toHaveBeenCalledTimes(2));
    const calls = vi.mocked(adminWrite).mock.calls;
    expect(calls[0][1]).toMatchObject({
      concurrency_limit: 23,
      api_key: "",
      expected_version: 1,
    });
    expect(calls[0][4]).toEqual(calls[1][4]);
  });
  it("makes observer configuration read-only", async () => {
    render(<H3AccountsManager readOnly />);
    await screen.findByText("总并发 18");
    expect(
      screen.queryByRole("button", { name: "添加账号" }),
    ).not.toBeInTheDocument();
    for (const input of screen.getAllByLabelText("并发上限"))
      expect(input).toBeDisabled();
  });
});
