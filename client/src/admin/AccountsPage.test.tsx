import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountsPage } from "./AccountsPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function transactionPage(offset: number, total: number) {
  return {
    items: [
      {
        id: `tx-${offset}`,
        user_id: "user-1",
        username: "operator-1",
        type: "CHARGE",
        available_delta: 10,
        reserved_delta: 0,
        available_balance_after: 18 as number | null,
        reserved_balance_after: 2 as number | null,
        recharge_order_id: "order-1",
        task_id: null,
        billing_round: null,
        created_at: "2026-09-01T10:01:00Z",
      },
    ],
    total,
    limit: 20,
    offset,
  };
}

function installFetch() {
  const fetchMock = vi.fn((url: string) => {
    const { searchParams, pathname } = new URL(String(url));
    const offset = Number(searchParams.get("offset") ?? "0");
    if (pathname.endsWith("/api/control/wallet-transactions.csv")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: new Headers({
          "X-Export-Total": "3",
          "X-Export-Returned": "3",
          "X-Export-Truncated": "false",
        }),
        blob: async () => new Blob(["id\n1"]),
      });
    }
    if (pathname.endsWith("/api/control/wallet-transactions")) {
      return jsonResponse(transactionPage(offset, 41));
    }
    throw new Error(`unexpected request: ${url}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("AccountsPage", () => {
  it("shows point units and traces general business charges to their source", async () => {
    const page = transactionPage(0, 1);
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          ...page,
          items: [
            {
              ...page.items[0],
              recharge_order_id: null,
              billing_operation_id: "operation-1",
              source_id: "image-task-1",
              service_name: "人物形象及任务图片",
            },
          ],
        }),
      ),
    );
    render(<AccountsPage />);
    expect(await screen.findByText("image-task-1")).toBeVisible();
    expect(screen.getByText("人物形象及任务图片")).toBeVisible();
    expect(screen.getByText("18 积分")).toBeVisible();
    expect(screen.queryByText("18 秒")).toBeNull();
  });
  it("exports wallet filters from the wallet page", async () => {
    const fetchMock = installFetch();
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
      () => undefined,
    );
    render(<AccountsPage />);
    await screen.findByText("operator-1");
    fireEvent.change(screen.getByLabelText("流水客户"), {
      target: { value: "operator" },
    });
    fireEvent.change(screen.getByLabelText("流水业务类型"), {
      target: { value: "CHARGE" },
    });
    fireEvent.change(screen.getByLabelText("流水起始时间"), {
      target: { value: "2026-09-12" },
    });
    fireEvent.change(screen.getByLabelText("流水截止时间"), {
      target: { value: "2026-09-12" },
    });
    const button = screen.getByRole("button", { name: "导出账务流水 CSV" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(
      await screen.findByText("当前筛选共 3 条，已全部导出。"),
    ).toBeInTheDocument();
    const call = fetchMock.mock.calls.find(([url]) =>
      new URL(url).pathname.endsWith("wallet-transactions.csv"),
    );
    expect(call).toBeDefined();
    expect(Object.fromEntries(new URL(String(call?.[0])).searchParams)).toEqual(
      {
        username: "operator",
        type: "CHARGE",
        created_from: "2026-09-12",
        created_to: "2026-09-12",
      },
    );
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders wallet transactions with deterministic balances", async () => {
    installFetch();
    render(<AccountsPage />);

    expect(await screen.findByText("operator-1")).toBeInTheDocument();
    // P2-1：业务类型下拉从词典生成后，选项文本与表格徽章重名，
    // 断言限定到表格内。
    const table = screen.getByRole("table", { name: "账务流水列表" });
    expect(within(table).getByText("充值到账")).toBeInTheDocument();
    expect(screen.getByText("+10 积分")).toBeInTheDocument();
    expect(screen.getByText(/18 积分/)).toBeInTheDocument();
    expect(screen.getByText(/暂扣中 2 积分/)).toBeInTheDocument();
    expect(
      screen.getByRole("columnheader", { name: "暂扣中变动" }),
    ).toBeInTheDocument();
  });

  it("builds the type filter from the vocabulary with the unified ledger terms", async () => {
    installFetch();
    render(<AccountsPage />);
    await screen.findByText("operator-1");

    const select = screen.getByLabelText("流水业务类型");
    const options = within(select).getAllByRole("option");
    const byValue = Object.fromEntries(
      options.map((option) => [
        (option as HTMLOptionElement).value,
        option.textContent,
      ]),
    );
    // 下拉与词典逐项对应；客服对客户说的「暂扣 / 实扣 / 退回」就是筛选项文字。
    expect(byValue).toMatchObject({
      RESERVE: "暂扣",
      SETTLE: "实扣",
      RELEASE: "退回",
      CHARGE: "充值到账",
      REFUND: "退款扣减",
      CONVERSION: "历史转换",
    });
    expect(options.map((option) => option.textContent)).not.toContain(
      "生成冻结",
    );
  });

  it("does not invent balances for unsequenced history", async () => {
    const fetchMock = vi.fn((url: string) => {
      const page = transactionPage(0, 1);
      page.items[0].available_balance_after = null;
      page.items[0].reserved_balance_after = null;
      if (
        new URL(String(url)).pathname.endsWith(
          "/api/control/wallet-transactions",
        )
      ) {
        return jsonResponse(page);
      }
      throw new Error(`unexpected request: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AccountsPage />);

    expect(await screen.findByText("历史未记录")).toBeInTheDocument();
  });

  it("pages wallet transactions with the server total", async () => {
    const fetchMock = installFetch();
    render(<AccountsPage />);

    expect(
      await screen.findByText("第 1 / 3 页（共 41 条）"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes(
            "/api/control/wallet-transactions?limit=20&offset=20",
          ),
        ),
      ).toBe(true),
    );
  });
});
