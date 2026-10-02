import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import * as adminApi from "../api.admin";
import { CustomersPage } from "./CustomersPage";

// Mock the admin API module
vi.mock("../api.admin", () => ({
  adminRead: vi.fn().mockResolvedValue({ services: [] }),
  submitBillingQuote: vi.fn(),
  getAccountCreditSummary: vi.fn().mockResolvedValue({
    user_id: "user-1",
    available_credits: 0,
    reserved_credits: 0,
    total_consumed_credits: 0,
    software_consumed_credits: 0,
    other_consumed_credits: 0,
    tokens: [],
  }),
  getLegacyCreditConversion: vi
    .fn()
    .mockRejectedValue(new Error("已有新积分，不适用转换")),
  listCustomers: vi.fn(),
  suspendCustomer: vi.fn().mockResolvedValue({}),
  resumeCustomer: vi.fn().mockResolvedValue({}),
  fetchCustomerUnitPrice: vi.fn(),
  updateCustomerUnitPrice: vi.fn(),
  fetchCustomerAnnotation: vi.fn(),
  listCustomerOwnerCandidates: vi.fn(),
  updateCustomerAnnotation: vi.fn(),
  createCustomerAdjustment: vi.fn(),
  listAdminRechargeOrders: vi.fn(),
  listAdminWalletTransactions: vi.fn(),
  listDevices: vi.fn(),
  listAuditLog: vi.fn(),
  listCustomerSessions: vi.fn(),
  getAdminGenerationRecordCalls: vi.fn(),
  getExternalCallResponse: vi.fn(),
  listAdminAdjustments: vi.fn().mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    offset: 0,
  }),
  AdminCustomerError: class extends Error {
    constructor(message: string) {
      super(message);
      this.name = "AdminCustomerError";
    }
  },
  AdminAdjustmentError: class extends Error {
    constructor(message: string) {
      super(message);
      this.name = "AdminAdjustmentError";
    }
  },
  AdminActivationError: class extends Error {
    readonly status: number | undefined;
    constructor(message: string, status?: number) {
      super(message);
      this.name = "AdminActivationError";
      this.status = status;
    }
  },
}));

vi.mock("../api", () => ({
  downloadCustomersCsv: vi.fn().mockResolvedValue(undefined),
}));

// 权益区有独立测试（CustomerBenefitsSection.test.tsx）；这里只验证客户页自身。
vi.mock("./CustomerBenefitsSection", () => ({
  CustomerBenefitsSection: () => null,
}));

describe("CustomersPage (ADM-02 / T33)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.sessionStorage.clear();
    vi.mocked(adminApi.fetchCustomerUnitPrice).mockResolvedValue({
      user_id: "user-1",
      unit_price_fen: 1000,
      custom_unit_price_fen: null,
      default_unit_price_fen: 1000,
      min_recharge_fen: 1000,
      recharge_step_fen: 1000,
      updated_at: null,
      request_id: "request-price-read",
    });
    // P2-3：详情展开即读标注 + 负责人候选；未显式叠加的用例走零值默认。
    vi.mocked(adminApi.fetchCustomerAnnotation).mockResolvedValue({
      user_id: "user-1",
      tags: [],
      note: "",
      owner_user_id: "",
      owner_username: "",
      updated_by_user_id: "",
      updated_at: "",
      request_id: "request-annotation-read",
    });
    vi.mocked(adminApi.listCustomerOwnerCandidates).mockResolvedValue({
      items: [],
    });
    vi.mocked(adminApi.listAdminRechargeOrders).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
    });
    vi.mocked(adminApi.listAdminWalletTransactions).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
    });
    vi.mocked(adminApi.listDevices).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
    });
    vi.mocked(adminApi.listCustomerSessions).mockResolvedValue({
      items: [],
      total: 0,
      limit: 3,
      offset: 0,
    });
    vi.mocked(adminApi.listAuditLog).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });
    // 充值单查单日志面板懒加载：默认给空列表，不点「查单日志」不会打到它。
    vi.mocked(adminApi.getAdminGenerationRecordCalls).mockResolvedValue({
      items: [],
      total: 0,
    });
  });

  it("restores server scope, shows global attention counts, and sorts both directions", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "stable-id",
          username: "operator-user",
          display_name: "合成客户公司",
          created_at: "2026-09-01T00:00:00Z",
          activation_code: "",
          status: "active",
          low_balance: true,
        },
      ],
      total: 25,
      limit: 20,
      offset: 20,
      attention_counts: { low_balance: 7, recent_failure: 6, inactive: 5 },
    });
    const navigate = vi.fn();
    render(
      <CustomersPage
        readOnly
        onCustomer={navigate}
        initialListQuery="username=合成&threshold=50&offset=20&sort=recharge&direction=asc"
      />,
    );
    await screen.findByText("合成客户公司");
    expect(adminApi.listCustomers).toHaveBeenCalledWith(
      expect.objectContaining({
        username_filter: "合成",
        lowBalanceThreshold: 50,
        offset: 20,
        sort: "recharge",
        direction: "asc",
      }),
    );
    expect(screen.queryByRole("button", { name: "导出列表 CSV" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /余额不足.*7/ }));
    await waitFor(() =>
      expect(adminApi.listCustomers).toHaveBeenLastCalledWith(
        expect.objectContaining({
          attention: "low_balance",
          lowBalanceThreshold: 50,
          offset: 0,
        }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /累计充值/ }));
    await waitFor(() =>
      expect(adminApi.listCustomers).toHaveBeenLastCalledWith(
        expect.objectContaining({
          sort: "recharge",
          direction: "desc",
          attention: "low_balance",
          offset: 0,
        }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /累计充值/ }));
    await waitFor(() =>
      expect(adminApi.listCustomers).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: "recharge", direction: "asc" }),
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: /合成客户公司/ }));
    expect(navigate).toHaveBeenCalledWith("stable-id");
  });

  it("does not invent a low balance threshold and only applies its draft on submit", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
      attention_counts: { low_balance: null, recent_failure: 0, inactive: 0 },
    });
    render(<CustomersPage />);
    await screen.findByText(/暂无客户/);
    expect(screen.getByRole("button", { name: /余额不足/ })).toBeDisabled();
    const before = vi.mocked(adminApi.listCustomers).mock.calls.length;
    fireEvent.change(screen.getByLabelText("低余额阈值"), {
      target: { value: "50" },
    });
    expect(adminApi.listCustomers).toHaveBeenCalledTimes(before);
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    await waitFor(() =>
      expect(adminApi.listCustomers).toHaveBeenLastCalledWith(
        expect.objectContaining({ lowBalanceThreshold: 50 }),
      ),
    );
  });

  it.each([false, true])(
    "账号暂停状态优先于激活码并在写入后刷新，初始暂停=%s",
    async (paused) => {
      const customer = {
        user_id: "user-1",
        username: "pause-customer",
        display_name: "暂停测试公司",
        created_at: "2026-10-01T00:00:00Z",
        activation_code: "XS04-****",
        status: "ACTIVE",
        account_active: !paused,
        activation_status: "ACTIVE",
        available_credits: 50,
        reserved_credits: 3,
      };
      vi.mocked(adminApi.listCustomers)
        .mockResolvedValueOnce({
          items: [customer],
          total: 1,
          limit: 20,
          offset: 0,
        })
        .mockResolvedValue({
          items: [
            {
              ...customer,
              account_active: paused,
              status: paused ? "ACTIVE" : "SUSPENDED",
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        });
      render(<CustomersPage />);
      fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
      fireEvent.click(
        await screen.findByRole("button", {
          name: paused ? "恢复账号" : "暂停账号",
        }),
      );
      const dialog = screen.getByRole("dialog");
      expect(
        within(dialog).getByText(/钱包余额|余额保持不变/),
      ).toBeInTheDocument();
      fireEvent.change(within(dialog).getByLabelText("操作原因"), {
        target: { value: "客户确认账户状态" },
      });
      fireEvent.click(
        within(dialog).getByRole("button", {
          name: paused ? "确认恢复账号" : "确认暂停账号",
        }),
      );
      await waitFor(() =>
        expect(
          paused ? adminApi.resumeCustomer : adminApi.suspendCustomer,
        ).toHaveBeenCalledWith(
          "user-1",
          "客户确认账户状态",
          expect.any(String),
        ),
      );
      expect(
        await screen.findByRole("button", {
          name: paused ? "暂停账号" : "恢复账号",
        }),
      ).toBeInTheDocument();
      expect(adminApi.listCustomers).toHaveBeenCalledTimes(2);
    },
  );

  it("客户详情可分页查看25条人工调整，稳定客户编号随翻页保留", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "分页调整客户",
          display_name: "调整公司",
          created_at: "2026-10-01T00:00:00Z",
          activation_code: "synthetic",
          status: "ACTIVE",
          available_credits: 50,
          reserved_credits: 0,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.listAdminAdjustments).mockResolvedValue({
      items: [
        {
          adjustment_id: "refund100",
          source_document_type: "REFUND_APPROVAL",
          source_document_ref: "refund100",
          reason: "已审批扣减100积分",
          credits: -100,
          amount_fen: 0,
          admin_username: "operator",
          balance_before: 150,
          balance_after: 50,
          created_at: "2026-10-01T00:00:00Z",
        },
      ],
      total: 25,
      limit: 20,
      offset: 0,
    } as never);
    render(<CustomersPage readOnly />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "查看全部人工调整" }),
    );
    expect(await screen.findByText("150 → 50 积分")).toBeInTheDocument();
    expect(screen.getByText("operator")).toBeInTheDocument();
    const history = screen.getByRole("table", { name: "调账历史列表" })
      .parentElement?.parentElement;
    expect(history).not.toBeNull();
    fireEvent.click(
      within(history as HTMLElement).getByRole("button", { name: "下一页" }),
    );
    await waitFor(() =>
      expect(adminApi.listAdminAdjustments).toHaveBeenLastCalledWith("user-1", {
        limit: 20,
        offset: 20,
      }),
    );
    expect(
      screen.queryByRole("button", { name: "导出 CSV" }),
    ).not.toBeInTheDocument();
  });

  it("renders customer list with pagination", async () => {
    const mockCustomers = [
      {
        user_id: "user-1",
        username: "customer-1",
        display_name: "乡墅装饰有限公司",
        created_at: "2026-08-24T10:00:00Z",
        activation_code: "ABC-123",
        status: "active",
        generation_total: 8,
        generation_succeeded: 5,
        generation_total_30d: 8,
        generation_succeeded_30d: 5,
        generation_failed_30d: 1,
        success_rate_30d: 62.5,
        generation_failed: 1,
        generation_in_progress: 1,
        generation_attention: 1,
        credits_spent: 5,
      },
      {
        user_id: "user-2",
        username: "customer-2",
        display_name: "合家美宅建材商行",
        created_at: "2026-08-24T11:00:00Z",
        activation_code: "DEF-456",
        status: "active",
      },
    ];

    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: mockCustomers,
      total: 2,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("customer-1")).toBeInTheDocument();
      expect(screen.getByText("customer-2")).toBeInTheDocument();
    });

    expect(screen.getByText("第 1 / 1 页（共 2 位）")).toBeInTheDocument();
    expect(screen.getByText("5 / 8 次")).toBeInTheDocument();
    const headers = within(screen.getByRole("table", { name: "客户列表" }))
      .getAllByRole("columnheader")
      .map((cell) => cell.textContent);
    expect(headers).toEqual([
      "客户",
      "负责人",
      "状态与权益",
      "可用积分",
      "累计充值 ↕",
      "本月消耗 ↕",
      "近30天生成",
      "最近活跃 ↕",
      "操作",
    ]);
    expect(screen.getByLabelText("customer-1 累计充值")).toHaveTextContent(
      "¥0.00",
    );
    const firstDataRow = screen
      .getAllByRole("row")
      .find((row) => row.textContent?.includes("customer-1"));
    expect(firstDataRow).toBeDefined();
    const cells = within(firstDataRow as HTMLElement).getAllByRole("cell");
    expect(cells).toHaveLength(9);
    expect(within(cells[0]).getByText("乡墅装饰有限公司")).toBeInTheDocument();
    expect(within(cells[0]).getByText("customer-1")).toBeInTheDocument();
    expect(
      within(cells[0]).getByRole("button", { name: "复制客户 ID" }),
    ).toBeInTheDocument();
    expect(cells[2]).toHaveTextContent("活跃原价");
    expect(cells[6]).toHaveTextContent("62.5%5 / 8 次失败 1");
    expect(cells[7]).toHaveTextContent("暂无活动");
  });

  it("公司名称未填写时显式占位，不拿用户名顶替", async () => {
    // display_name 在契约里可选（激活/注册默认写用户名，客户可在个人中心改）。
    // 缺失时若回退成用户名，既与相邻的「用户名」列重复，又掩盖了"这个账号还没填
    // 公司名"这一运营信号 —— 而该信号的用处正是提醒运营去催客户补填。
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    const row = await screen.findByRole("row", { name: /customer-1/ });
    const cells = within(row)
      .getAllByRole("cell")
      .map((cell) => cell.textContent?.replace(/\s+/g, " ").trim());
    // 「用户名」列有值、与「公司名称」列内容不同，才说明没有静默顶替。
    expect(cells[0]).toContain("未填写customer-1");
    expect(within(row).getByText("未填写").tagName).toBe("STRONG");

    // 详情页与列表同口径（原先详情页在这里回退成用户名，两处显示不同值）。
    fireEvent.click(screen.getByRole("button", { name: "展开详情" }));
    await waitFor(() => {
      expect(screen.getAllByText("未填写").length).toBeGreaterThan(0);
    });
  });

  it("列表展示客户标签与负责人，标签超出 3 个折叠为 +N（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          display_name: "乡墅装饰有限公司",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
          tags: ["VIP", "重点客户", "已回访", "待续费"],
          owner_user_id: "admin-9",
          owner_username: "ops-chen",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    const row = await screen.findByRole("row", { name: /customer-1/ });
    // 列窄：只摆前 3 个 pill，余量收成 +N；完整清单在 title 里悬浮可见。
    const tagsCell = within(row).getByText("VIP").closest("td");
    expect(tagsCell).toHaveTextContent("重点客户");
    expect(tagsCell).toHaveTextContent("已回访");
    expect(tagsCell).not.toHaveTextContent("待续费");
    expect(tagsCell).toHaveTextContent("+1");
    expect(tagsCell?.querySelector(".customer-cell-tags")).toHaveAttribute(
      "title",
      "VIP、重点客户、已回访、待续费",
    );
    expect(within(row).getByText("ops-chen")).toBeInTheDocument();
  });

  it("详情标注区：审计员只读展示，无编辑表单（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.fetchCustomerAnnotation).mockResolvedValue({
      user_id: "user-1",
      tags: ["VIP"],
      note: "老客户，续费前先电话回访。",
      owner_user_id: "admin-9",
      owner_username: "ops-chen",
      updated_by_user_id: "admin-1",
      updated_at: "2026-09-28T10:00:00Z",
      request_id: "request-annotation-read",
    });

    render(<CustomersPage readOnly />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    const section = await screen.findByRole("region", { name: "客户标注" });
    expect(await within(section).findByText("VIP")).toBeInTheDocument();
    expect(within(section).getByText("ops-chen")).toBeInTheDocument();
    expect(
      within(section).getByText("老客户，续费前先电话回访。"),
    ).toBeInTheDocument();
    expect(within(section).getByText(/审计员仅可查看/)).toBeInTheDocument();
    expect(
      within(section).queryByRole("button", { name: "保存标注" }),
    ).not.toBeInTheDocument();
  });

  it("详情标注区：整体替换保存后提示并刷新列表（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.fetchCustomerAnnotation).mockResolvedValue({
      user_id: "user-1",
      tags: ["VIP"],
      note: "老客户",
      owner_user_id: "",
      owner_username: "",
      updated_by_user_id: "admin-1",
      updated_at: "2026-09-28T10:00:00Z",
      request_id: "request-annotation-read",
    });
    vi.mocked(adminApi.listCustomerOwnerCandidates).mockResolvedValue({
      items: [
        { user_id: "admin-9", username: "ops-chen", display_name: "陈运营" },
      ],
    });
    vi.mocked(adminApi.updateCustomerAnnotation).mockResolvedValue({
      user_id: "user-1",
      tags: ["VIP", "重点"],
      note: "已回访",
      owner_user_id: "admin-9",
      owner_username: "ops-chen",
      updated_by_user_id: "admin-1",
      updated_at: "2026-09-29T02:00:00Z",
      request_id: "request-annotation-write",
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    const section = await screen.findByRole("region", { name: "客户标注" });
    const tagsInput = within(section).getByLabelText(
      "标签（逗号分隔，最多 10 个）",
    );
    expect(tagsInput).toHaveValue("VIP");
    // 中文逗号分隔、去空白保序——提交前前端与服务端同口径拆分。
    fireEvent.change(tagsInput, { target: { value: "VIP，重点" } });
    fireEvent.change(
      within(section).getByLabelText("备注（最多 2000 字，仅运营可见）"),
      { target: { value: "已回访" } },
    );
    fireEvent.change(within(section).getByLabelText("负责人"), {
      target: { value: "admin-9" },
    });
    fireEvent.click(within(section).getByRole("button", { name: "保存标注" }));

    await screen.findByRole("dialog", { name: "保存客户标注" });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => {
      expect(adminApi.updateCustomerAnnotation).toHaveBeenCalledWith(
        "user-1",
        { tags: ["VIP", "重点"], note: "已回访", owner_user_id: "admin-9" },
        "更新客户标注",
      );
    });
    expect(await screen.findByText("客户标注已保存")).toBeInTheDocument();
    // 列表带标注列，保存后必须重新拉列表（onChanged）：初次 1 次 + 刷新 ≥1 次。
    expect(vi.mocked(adminApi.listCustomers).mock.calls.length).toBeGreaterThan(
      1,
    );
  });

  it("详情标注区：标签超过 10 个本地拦截，不发请求（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    const section = await screen.findByRole("region", { name: "客户标注" });
    fireEvent.change(
      within(section).getByLabelText("标签（逗号分隔，最多 10 个）"),
      {
        target: {
          value: "t1，t2，t3，t4，t5，t6，t7，t8，t9，t10，t11",
        },
      },
    );
    fireEvent.click(within(section).getByRole("button", { name: "保存标注" }));

    expect(
      await within(section).findByText("标签最多 10 个"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "保存客户标注" }),
    ).not.toBeInTheDocument();
    expect(adminApi.updateCustomerAnnotation).not.toHaveBeenCalled();
  });

  it("列表展示客户标签与负责人，标签超出 3 个折叠为 +N（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          display_name: "乡墅装饰有限公司",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
          tags: ["VIP", "重点客户", "已回访", "待续费"],
          owner_user_id: "admin-9",
          owner_username: "ops-chen",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    const row = await screen.findByRole("row", { name: /customer-1/ });
    // 列窄：只摆前 3 个 pill，余量收成 +N；完整清单在 title 里悬浮可见。
    const tagsCell = within(row).getByText("VIP").closest("td");
    expect(tagsCell).toHaveTextContent("重点客户");
    expect(tagsCell).toHaveTextContent("已回访");
    expect(tagsCell).not.toHaveTextContent("待续费");
    expect(tagsCell).toHaveTextContent("+1");
    expect(tagsCell?.querySelector(".customer-cell-tags")).toHaveAttribute(
      "title",
      "VIP、重点客户、已回访、待续费",
    );
    expect(within(row).getByText("ops-chen")).toBeInTheDocument();
  });

  it("详情标注区：审计员只读展示，无编辑表单（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.fetchCustomerAnnotation).mockResolvedValue({
      user_id: "user-1",
      tags: ["VIP"],
      note: "老客户，续费前先电话回访。",
      owner_user_id: "admin-9",
      owner_username: "ops-chen",
      updated_by_user_id: "admin-1",
      updated_at: "2026-09-28T10:00:00Z",
      request_id: "request-annotation-read",
    });

    render(<CustomersPage readOnly />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    const section = await screen.findByRole("region", { name: "客户标注" });
    expect(await within(section).findByText("VIP")).toBeInTheDocument();
    expect(within(section).getByText("ops-chen")).toBeInTheDocument();
    expect(
      within(section).getByText("老客户，续费前先电话回访。"),
    ).toBeInTheDocument();
    expect(within(section).getByText(/审计员仅可查看/)).toBeInTheDocument();
    expect(
      within(section).queryByRole("button", { name: "保存标注" }),
    ).not.toBeInTheDocument();
  });

  it("详情标注区：整体替换保存后提示并刷新列表（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.fetchCustomerAnnotation).mockResolvedValue({
      user_id: "user-1",
      tags: ["VIP"],
      note: "老客户",
      owner_user_id: "",
      owner_username: "",
      updated_by_user_id: "admin-1",
      updated_at: "2026-09-28T10:00:00Z",
      request_id: "request-annotation-read",
    });
    vi.mocked(adminApi.listCustomerOwnerCandidates).mockResolvedValue({
      items: [
        { user_id: "admin-9", username: "ops-chen", display_name: "陈运营" },
      ],
    });
    vi.mocked(adminApi.updateCustomerAnnotation).mockResolvedValue({
      user_id: "user-1",
      tags: ["VIP", "重点"],
      note: "已回访",
      owner_user_id: "admin-9",
      owner_username: "ops-chen",
      updated_by_user_id: "admin-1",
      updated_at: "2026-09-29T02:00:00Z",
      request_id: "request-annotation-write",
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    const section = await screen.findByRole("region", { name: "客户标注" });
    const tagsInput = within(section).getByLabelText(
      "标签（逗号分隔，最多 10 个）",
    );
    expect(tagsInput).toHaveValue("VIP");
    // 中文逗号分隔、去空白保序——提交前前端与服务端同口径拆分。
    fireEvent.change(tagsInput, { target: { value: "VIP，重点" } });
    fireEvent.change(
      within(section).getByLabelText("备注（最多 2000 字，仅运营可见）"),
      { target: { value: "已回访" } },
    );
    fireEvent.change(within(section).getByLabelText("负责人"), {
      target: { value: "admin-9" },
    });
    fireEvent.click(within(section).getByRole("button", { name: "保存标注" }));

    await screen.findByRole("dialog", { name: "保存客户标注" });
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => {
      expect(adminApi.updateCustomerAnnotation).toHaveBeenCalledWith(
        "user-1",
        { tags: ["VIP", "重点"], note: "已回访", owner_user_id: "admin-9" },
        "更新客户标注",
      );
    });
    expect(await screen.findByText("客户标注已保存")).toBeInTheDocument();
    // 列表带标注列，保存后必须重新拉列表（onChanged）：初次 1 次 + 刷新 ≥1 次。
    expect(vi.mocked(adminApi.listCustomers).mock.calls.length).toBeGreaterThan(
      1,
    );
  });

  it("详情标注区：标签超过 10 个本地拦截，不发请求（P2-3）", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));

    const section = await screen.findByRole("region", { name: "客户标注" });
    fireEvent.change(
      within(section).getByLabelText("标签（逗号分隔，最多 10 个）"),
      {
        target: {
          value: "t1，t2，t3，t4，t5，t6，t7，t8，t9，t10，t11",
        },
      },
    );
    fireEvent.click(within(section).getByRole("button", { name: "保存标注" }));

    expect(
      await within(section).findByText("标签最多 10 个"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("dialog", { name: "保存客户标注" }),
    ).not.toBeInTheDocument();
    expect(adminApi.updateCustomerAnnotation).not.toHaveBeenCalled();
  });

  it("筛选框说明关键字同时覆盖用户名与公司名称", async () => {
    // 列有了公司名、筛选却只认用户名，运营照样找不到目标公司 —— 文案与
    // placeholder 一起锁定这条识别路径。
    render(<CustomersPage />);

    expect(await screen.findByText("用户名 / 公司名称")).toBeVisible();
    expect(screen.getByPlaceholderText("按用户名或公司名称筛选")).toBeVisible();
  });

  it("表格列数与该表 fixed 布局的列宽规则数一致", async () => {
    // 回归守卫：.customers-table 是 table-layout: fixed，宽度按 nth-child 位置
    // 生效。新增「公司名称」列时漏改样式，宽度就从插入点起整体错位、末列拿不到
    // 宽度 —— 渲染层测不出这类问题，所以在这里把列数与规则数钉在一起。
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          display_name: "乡墅装饰有限公司",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    const columns = within(
      await screen.findByRole("table", { name: "客户列表" }),
    ).getAllByRole("columnheader").length;

    // Vitest 对 CSS 导入返回空串（css: false），所以读源码文件本身。路径以
    // client/ 为根（`npm run test --workspace client` 的 cwd）；读不到就直接
    // 失败，避免守卫静默失效。
    const css = readFileSync(
      resolve(process.cwd(), "src/admin/admin-customer-detail.css"),
      "utf8",
    );
    const desktop = /@media \(min-width: 761px\) \{([\s\S]*?)\n\}/.exec(
      css,
    )?.[1];
    expect(desktop).toBeDefined();
    const rules = [
      ...(desktop as string).matchAll(
        /\.customers-table th:nth-child\((\d+)\)\s*\{\s*width:\s*(\d+)%/g,
      ),
    ];

    // 编号必须 1..N 无缺口无重复，且宽度合计 100%（fixed 布局下多出的列分不到宽度）。
    expect(rules.map((rule) => Number(rule[1]))).toEqual(
      Array.from({ length: columns }, (_, index) => index + 1),
    );
    expect(rules.reduce((sum, rule) => sum + Number(rule[2]), 0)).toBe(100);
  });

  it("shows loading state while fetching customers", () => {
    vi.mocked(adminApi.listCustomers).mockImplementation(
      () => new Promise(() => {}), // Never resolves
    );

    render(<CustomersPage />);

    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("handles API error gracefully", async () => {
    vi.mocked(adminApi.listCustomers).mockRejectedValue(
      new adminApi.AdminCustomerError("网络错误"),
    );

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("加载失败：网络错误")).toBeInTheDocument();
    });
  });

  it("supports pagination navigation", async () => {
    const mockCustomers = Array.from({ length: 20 }, (_, i) => ({
      user_id: `user-${i}`,
      username: `user-${i}`,
      created_at: "2026-08-24T10:00:00Z",
      activation_code: `CODE-${i}`,
      status: "active",
    }));

    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: mockCustomers,
      total: 50,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("第 1 / 3 页（共 50 位）")).toBeInTheDocument();
    });

    const nextPageButton = screen.getByRole("button", { name: "下一页" });
    fireEvent.click(nextPageButton);

    expect(adminApi.listCustomers).toHaveBeenCalledWith(
      expect.objectContaining({
        limit: 20,
        offset: 20,
      }),
    );
  });

  it("supports filtering by username", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    const filterInput = screen.getByPlaceholderText("按用户名或公司名称筛选");
    fireEvent.change(filterInput, { target: { value: "customer-1" } });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    expect(adminApi.listCustomers).toHaveBeenCalledWith(
      expect.objectContaining({
        limit: 20,
        offset: 0,
        username_filter: "customer-1",
      }),
    );
  });

  it("applies filter drafts together and ignores an older response arriving last", async () => {
    let resolveOld!: (
      value: Awaited<ReturnType<typeof adminApi.listCustomers>>,
    ) => void;
    vi.mocked(adminApi.listCustomers).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "new-id",
          username: "new-user",
          status: "active",
          created_at: "2026-09-13T10:00:00Z",
          activation_code: "",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    render(<CustomersPage />);
    fireEvent.change(screen.getByLabelText("最低余额"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("注册起始"), {
      target: { value: "2026-09-01" },
    });
    expect(adminApi.listCustomers).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    await screen.findByText("new-user");
    resolveOld({ items: [], total: 0, limit: 20, offset: 0 });
    await waitFor(() =>
      expect(screen.getByText("new-user")).toBeInTheDocument(),
    );
    expect(adminApi.listCustomers).toHaveBeenCalledTimes(2);
  });

  it("exports the same date, status and balance filters as the list", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });
    render(<CustomersPage />);

    fireEvent.change(screen.getByLabelText("注册起始"), {
      target: { value: "2026-09-01" },
    });
    fireEvent.change(screen.getByLabelText("注册截止"), {
      target: { value: "2026-09-05" },
    });
    fireEvent.change(screen.getByLabelText("最低余额"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("最高余额"), {
      target: { value: "500" },
    });
    fireEvent.change(screen.getByLabelText("客户状态"), {
      target: { value: "active" },
    });
    fireEvent.change(screen.getByPlaceholderText("按用户名或公司名称筛选"), {
      target: { value: "customer-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    fireEvent.click(screen.getByRole("button", { name: "导出列表 CSV" }));

    expect(api.downloadCustomersCsv).toHaveBeenCalledWith({
      status: "active",
      username: "customer-1",
      createdFrom: "2026-09-01",
      createdTo: "2026-09-05",
      balanceMin: 10,
      balanceMax: 500,
      attention: "",
      sort: "activated",
      direction: "desc",
      lowBalanceThreshold: undefined,
    });
  });

  it("opens the adjustment history for the selected customer", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("customer-1")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "展开详情" }));
    expect(
      screen.queryByRole("button", { name: "调账历史" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "复制客户 ID" }),
    ).toBeInTheDocument();
  });

  it("opens a focused customer detail and returns to the filtered list", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          display_name: "乡墅装饰有限公司",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
          generation_total: 8,
          generation_succeeded: 5,
          generation_failed: 1,
          generation_in_progress: 1,
          generation_attention: 1,
          credits_spent: 5,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    fireEvent.change(
      await screen.findByPlaceholderText("按用户名或公司名称筛选"),
      {
        target: { value: "customer-1" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    await waitFor(() => {
      expect(adminApi.listCustomers).toHaveBeenLastCalledWith(
        expect.objectContaining({ username_filter: "customer-1" }),
      );
    });
    const toggle = await screen.findByRole("button", { name: "展开详情" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(toggle);
    // 六页签（方案 P1）：资金数据在「充值与积分」、设备在「登录与设备」。
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    fireEvent.click(screen.getByRole("tab", { name: "登录与设备" }));

    expect(screen.queryByRole("table", { name: "客户列表" })).toBeNull();
    const detailPanel = screen
      .getByRole("heading", { name: "乡墅装饰有限公司" })
      .closest("div");
    expect(detailPanel).not.toBeNull();
    // 详情页带出公司名（display_name），让管理员确认账号与公司的一一对应
    expect(screen.getByText(/用户名 customer-1/)).toBeInTheDocument();
    expect(screen.getByText("乡墅装饰有限公司")).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "客户核心指标" }),
    ).toBeInTheDocument();
    expect(screen.getByText("累计消耗")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "调账历史",
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "← 返回客户列表" }),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(adminApi.listAdminRechargeOrders).toHaveBeenCalledWith({
        userId: "user-1",
        limit: 3,
        offset: 0,
      });
      expect(adminApi.listAdminWalletTransactions).toHaveBeenCalledWith({
        userId: "user-1",
        limit: 3,
        offset: 0,
      });
      // 任务书 C（2026-09-18）第 2 项：客户详情恢复设备视图，
      // 进入详情即按 user_id 拉取该客户的 BOUND 设备。
      // 本条此前断言 listDevices 不被调用，是 PR #102 删除设备页后的固化状态，
      // 随任务书 C 反转；会话与调账仍未在详情内直连，两条否定断言保持。
      expect(adminApi.listDevices).toHaveBeenCalledWith({
        userId: "user-1",
        offset: 0,
        limit: 50,
      });
      // 方案 P1「登录与设备」页签：设备 + 该客户的在线会话一并挂载；
      // 调账历史仍不在详情内直连（资金中心入口）。
      expect(adminApi.listCustomerSessions).not.toHaveBeenCalled();
      expect(adminApi.listDevices).toHaveBeenCalledTimes(1);
      expect(adminApi.listAdminAdjustments).not.toHaveBeenCalled();
    });
    expect(
      screen.getByRole("region", { name: "客户设备" }),
    ).toBeInTheDocument();
    expect(screen.getByText("该范围内没有设备记录。")).toBeInTheDocument();
    expect(screen.queryByText("绑定设备")).not.toBeInTheDocument();
    expect(screen.queryByText("登录设备")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "查看设备" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "查看会话" }),
    ).not.toBeInTheDocument();
    // 详情不单列生成冻结额度：旧叫法「冻结额度」与统一后的「生成冻结额度」都不应出现。
    expect(screen.queryByText(/冻结额度|生成冻结额度/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "← 返回客户列表" }));

    expect(
      await screen.findByRole("table", { name: "客户列表" }),
    ).toBeInTheDocument();
    expect(screen.getByPlaceholderText("按用户名或公司名称筛选")).toHaveValue(
      "customer-1",
    );
  });

  it("用订单号反查充值单的查单日志（RECHARGE_ORDER，P0-9 #9）", async () => {
    // 充值单此前只写不读：运营在客户详情看得到订单，却无法知道这张单
    // 出网调了什么、支付回调报文长什么样。订单行现在带「查单日志」，
    // 以订单号为 record_id 打到 calls 端点。
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          display_name: "乡墅装饰有限公司",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
          generation_total: 8,
          generation_succeeded: 5,
          generation_failed: 1,
          generation_in_progress: 1,
          generation_attention: 1,
          credits_spent: 5,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.listAdminRechargeOrders).mockResolvedValue({
      items: [
        {
          id: "order-row-1",
          user_id: "user-1",
          username: "customer-1",
          order_no: "CZ20260928001",
          status: "PAID",
          amount_fen: 9900,
          credits: 100,
          channel: "wechat_native",
          provider: "zpay",
          provider_trade_no: "trade-1",
          transaction_id: null,
          created_at: "2026-09-28T09:59:00Z",
          paid_at: "2026-09-28T10:00:00Z",
        },
      ],
      total: 1,
      limit: 3,
      offset: 0,
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));

    const toggle = await screen.findByRole("button", { name: "查单日志" });
    // 懒加载：没点之前不发请求。
    expect(adminApi.getAdminGenerationRecordCalls).not.toHaveBeenCalled();

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(adminApi.getAdminGenerationRecordCalls).toHaveBeenCalledWith(
        "RECHARGE_ORDER",
        "CZ20260928001",
        { limit: 50, offset: 0 },
      ),
    );
    expect(
      await screen.findByText("该记录暂无第三方调用留痕。"),
    ).toBeInTheDocument();
  });

  it("loads and updates the customer's effective recharge price", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.updateCustomerUnitPrice).mockResolvedValue({
      user_id: "user-1",
      unit_price_fen: 880,
      custom_unit_price_fen: 880,
      default_unit_price_fen: 1000,
      min_recharge_fen: 1000,
      recharge_step_fen: 880,
      updated_at: "2026-08-29T10:00:00Z",
      request_id: "request-price-write",
    });

    render(<CustomersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "价格与权益" }));

    expect(await screen.findByText(/当前 ¥10.00 \/ 秒/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("售价（元/秒）"), {
      target: { value: "8.8" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存客户售价" }));

    await screen.findByRole("dialog", { name: "修改客户售价" });
    expect(screen.queryByLabelText("操作原因")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认保存" }));

    await waitFor(() => {
      expect(adminApi.updateCustomerUnitPrice).toHaveBeenCalledWith(
        "user-1",
        880,
        "更新客户售价",
      );
    });
    expect(await screen.findByText("客户售价已保存")).toBeInTheDocument();
  });

  it("grants free credits through the audited FREE_GRANT adjustment", async () => {
    // 首步填写事由，自动单号在确认及重试时保持一致。
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValue({
      adjustment_id: "adj-free-1",
      order_id: "order-free-1",
      credits: "10",
      amount_fen: "0",
      pricing_scope: "CUSTOMER_STANDARD",
      wallet_balance_after: 60,
      source_document_type: "FREE_GRANT",
      source_document_ref: "PROMO-2026-09-001",
      request_id: "request-free-grant",
    });

    let view = render(<CustomersPage operatorId="admin-retry" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));

    fireEvent.change(await screen.findByLabelText("发放积分"), {
      target: { value: "10" },
    });
    expect(screen.queryByLabelText("来源单号")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("事由"), {
      target: { value: "新客活动发放" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));

    const dialog = await screen.findByRole("dialog", { name: "发放赠送积分" });
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent("新客活动发放");
    expect(dialog).toHaveTextContent("GRANT-");
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
          available_credits: 60,
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.createCustomerAdjustment).mockRejectedValueOnce(
      new Error("结果未知，请重试"),
    );
    fireEvent.click(screen.getByRole("button", { name: "确认发放" }));
    await screen.findByText("结果未知，请重试");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByLabelText("积分来源")).toBeDisabled();
    expect(screen.getByLabelText("发放积分")).toBeDisabled();
    expect(screen.getByLabelText("事由")).toBeDisabled();
    expect(screen.getByText(/上次发放结果尚未确认/)).toHaveTextContent(
      "新客活动发放",
    );

    fireEvent.click(screen.getByRole("button", { name: "← 返回客户列表" }));
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    expect(screen.getByLabelText("发放积分")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "重试确认上次发放" }),
    ).toBeEnabled();

    view.unmount();
    const otherOperatorView = render(
      <CustomersPage operatorId="other-admin" />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    expect(screen.getByLabelText("发放积分")).toBeEnabled();
    expect(screen.getByRole("button", { name: "发放赠送积分" })).toBeEnabled();
    otherOperatorView.unmount();

    view = render(<CustomersPage operatorId="admin-retry" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    expect(screen.getByLabelText("发放积分")).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "重试确认上次发放" }));
    vi.mocked(adminApi.createCustomerAdjustment).mockRejectedValueOnce(
      new adminApi.AdminActivationError("会话已失效", 403),
    );
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );
    await screen.findByText("会话已失效");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.getByLabelText("发放积分")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试确认上次发放" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );

    await waitFor(() => {
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledWith(
        "user-1",
        {
          sourceDocumentType: "FREE_GRANT",
          sourceDocumentRef: expect.stringMatching(/^GRANT-[a-f0-9-]+$/),
          credits: 10,
        },
        "新客活动发放",
        expect.any(String),
      );
    });
    expect(await screen.findByText(/已发放 10 赠送积分/)).toBeInTheDocument();
    expect(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls,
    ).toHaveLength(3);
    expect(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[1]).toEqual(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0],
    );
    expect(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[2]).toEqual(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0],
    );
    await waitFor(() =>
      expect(
        within(screen.getByRole("region", { name: "客户核心指标" })).getByText(
          "60",
        ),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getAllByRole("region", { name: "账号积分查账" }),
    ).toHaveLength(1);
  });

  it("carries a real ticket reference for a CS_TICKET compensation", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.createCustomerAdjustment).mockResolvedValue({
      adjustment_id: "adj-ticket-1",
      order_id: "order-ticket-1",
      credits: "8",
      amount_fen: "0",
      pricing_scope: "CUSTOMER_STANDARD",
      wallet_balance_after: 68,
      source_document_type: "CS_TICKET",
      source_document_ref: "TICKET-20260928-001",
      request_id: "request-ticket",
    });

    render(<CustomersPage operatorId="admin-ticket" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));

    fireEvent.change(await screen.findByLabelText("积分来源"), {
      target: { value: "CS_TICKET" },
    });
    fireEvent.change(screen.getByLabelText("发放积分"), {
      target: { value: "8" },
    });
    fireEvent.change(screen.getByLabelText("事由"), {
      target: { value: "客服工单补偿" },
    });
    // 工单号没填不许进入确认：补偿必须能回溯到客服工单。
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    expect(await screen.findByText(/请填写来源单号/)).toBeInTheDocument();
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();

    fireEvent.change(await screen.findByLabelText("来源单号"), {
      target: { value: "TICKET-20260928-001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );

    await waitFor(() => {
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledWith(
        "user-1",
        {
          sourceDocumentType: "CS_TICKET",
          sourceDocumentRef: "TICKET-20260928-001",
          credits: 8,
        },
        "客服工单补偿",
        expect.any(String),
      );
    });
  });

  it("restores an in-flight free-credit intent before its response arrives", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    const success = {
      adjustment_id: "adj-in-flight",
      order_id: "order-in-flight",
      credits: "12",
      amount_fen: "0",
      pricing_scope: "CUSTOMER_STANDARD",
      wallet_balance_after: 62,
      source_document_type: "FREE_GRANT",
      source_document_ref: "GRANT-IN-FLIGHT",
      request_id: "request-in-flight",
    };
    let resolveFirst: ((value: typeof success) => void) | undefined;
    let resolveSecond: ((value: typeof success) => void) | undefined;
    const firstRequest = new Promise<typeof success>((resolve) => {
      resolveFirst = resolve;
    });
    const secondRequest = new Promise<typeof success>((resolve) => {
      resolveSecond = resolve;
    });
    vi.mocked(adminApi.createCustomerAdjustment)
      .mockImplementationOnce(() => firstRequest)
      .mockImplementationOnce(() => secondRequest)
      .mockResolvedValue(success);

    let view = render(<CustomersPage operatorId="admin-in-flight" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    fireEvent.change(await screen.findByLabelText("发放积分"), {
      target: { value: "12" },
    });
    fireEvent.change(screen.getByLabelText("事由"), {
      target: { value: "在途请求恢复" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );
    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(1),
    );

    view.unmount();
    view = render(<CustomersPage operatorId="admin-in-flight" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    expect(screen.getByLabelText("发放积分")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试确认上次发放" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );
    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(2),
    );

    await act(async () => {
      resolveFirst?.(success);
      await firstRequest;
    });
    view.unmount();
    view = render(<CustomersPage operatorId="admin-in-flight" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    expect(screen.getByLabelText("发放积分")).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重试确认上次发放" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );

    expect(await screen.findByText(/已发放 12 赠送积分/)).toBeInTheDocument();
    expect(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls,
    ).toHaveLength(3);
    expect(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[1]).toEqual(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0],
    );
    expect(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[2]).toEqual(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0],
    );
    await act(async () => {
      resolveSecond?.(success);
      await secondRequest;
    });
  });

  it("ignores a successful response after its free-credit section unmounts", async () => {
    const customer = {
      user_id: "user-1",
      username: "customer-1",
      created_at: "2026-08-24T10:00:00Z",
      activation_code: "ABC-123",
      status: "active",
    };
    vi.mocked(adminApi.listCustomers)
      .mockResolvedValueOnce({
        items: [customer],
        total: 1,
        limit: 20,
        offset: 0,
      })
      .mockRejectedValue(new Error("刷新失败"));
    const firstGrant = {
      adjustment_id: "adj-first",
      order_id: "order-first",
      credits: "10",
      amount_fen: "0",
      pricing_scope: "CUSTOMER_STANDARD",
      wallet_balance_after: 60,
      source_document_type: "FREE_GRANT",
      source_document_ref: "GRANT-FIRST",
      request_id: "request-first",
    };
    const secondGrant = {
      ...firstGrant,
      adjustment_id: "adj-second",
      order_id: "order-second",
      credits: "20",
      wallet_balance_after: 80,
      source_document_ref: "GRANT-SECOND",
      request_id: "request-second",
    };
    let resolveOriginal: ((value: typeof firstGrant) => void) | undefined;
    const originalRequest = new Promise<typeof firstGrant>((resolve) => {
      resolveOriginal = resolve;
    });
    vi.mocked(adminApi.createCustomerAdjustment)
      .mockImplementationOnce(() => originalRequest)
      .mockResolvedValueOnce(firstGrant)
      .mockResolvedValue(secondGrant);

    render(<CustomersPage operatorId="admin-late-success" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    fireEvent.change(await screen.findByLabelText("发放积分"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("事由"), {
      target: { value: "第一笔" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );
    await waitFor(() =>
      expect(adminApi.createCustomerAdjustment).toHaveBeenCalledTimes(1),
    );

    fireEvent.click(screen.getByRole("button", { name: "← 返回客户列表" }));
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    fireEvent.click(screen.getByRole("button", { name: "重试确认上次发放" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );
    expect(await screen.findByText(/已发放 10 赠送积分/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("发放积分"), {
      target: { value: "20" },
    });
    fireEvent.change(screen.getByLabelText("事由"), {
      target: { value: "第二笔" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );
    expect(await screen.findByText(/已发放 20 赠送积分/)).toBeInTheDocument();

    await act(async () => {
      resolveOriginal?.(firstGrant);
      await originalRequest;
    });
    expect(
      within(screen.getByRole("region", { name: "客户核心指标" })).getByText(
        "80",
      ),
    ).toBeInTheDocument();
    expect(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[1]).toEqual(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0],
    );
    expect(
      vi.mocked(adminApi.createCustomerAdjustment).mock.calls[2]?.[3],
    ).not.toBe(vi.mocked(adminApi.createCustomerAdjustment).mock.calls[0]?.[3]);
  });

  it("does not send a free-credit request when durable intent storage fails", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    const storageSpy = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new Error("storage unavailable");
      });

    render(<CustomersPage operatorId="admin-storage-failure" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    fireEvent.change(await screen.findByLabelText("发放积分"), {
      target: { value: "8" },
    });
    fireEvent.change(screen.getByLabelText("事由"), {
      target: { value: "存储失败不得发送" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );

    expect(
      await screen.findByText(/无法安全保存待确认发放/),
    ).toBeInTheDocument();
    expect(adminApi.createCustomerAdjustment).not.toHaveBeenCalled();
    storageSpy.mockRestore();
  });

  it("releases a rejected free-credit intent so the operator can correct it", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });
    vi.mocked(adminApi.createCustomerAdjustment).mockRejectedValueOnce(
      new adminApi.AdminActivationError("事由不符合要求", 422),
    );

    render(<CustomersPage operatorId="admin-rejected" />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "充值与积分" }));
    fireEvent.change(await screen.findByLabelText("发放积分"), {
      target: { value: "10" },
    });
    fireEvent.change(screen.getByLabelText("事由"), {
      target: { value: "待修正事由" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发放赠送积分" }));
    fireEvent.click(
      within(
        await screen.findByRole("dialog", { name: "发放赠送积分" }),
      ).getByRole("button", { name: "确认发放" }),
    );
    await screen.findByText("事由不符合要求");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(screen.getByLabelText("积分来源")).toBeEnabled();
    expect(screen.getByLabelText("发放积分")).toBeEnabled();
    expect(screen.getByLabelText("事由")).toBeEnabled();
    expect(screen.getByRole("button", { name: "发放赠送积分" })).toBeEnabled();
  });

  it("keeps customer pricing read-only for auditors", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage readOnly />);
    fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
    fireEvent.click(screen.getByRole("tab", { name: "价格与权益" }));

    expect(
      await screen.findByText("审计员仅可查看定价，不能修改。"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存客户售价" })).toBeNull();
  });

  it("shows empty state when no customers exist", async () => {
    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: [],
      total: 0,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("暂无客户数据")).toBeInTheDocument();
    });
  });

  it("displays customer status badges correctly", async () => {
    const mockCustomers = [
      {
        user_id: "user-1",
        username: "active-user",
        created_at: "2026-08-24T10:00:00Z",
        activation_code: "ABC-123",
        status: "active",
      },
      {
        user_id: "user-2",
        username: "suspended-user",
        created_at: "2026-08-24T11:00:00Z",
        activation_code: "DEF-456",
        status: "suspended",
      },
    ];

    vi.mocked(adminApi.listCustomers).mockResolvedValue({
      items: mockCustomers,
      total: 2,
      limit: 20,
      offset: 0,
    });

    render(<CustomersPage />);

    await waitFor(() => {
      expect(screen.getByText("活跃")).toBeInTheDocument();
      expect(screen.getByText("已暂停")).toBeInTheDocument();
    });
  });

  // 总览快捷入口（customerAdjustments）落到本页的接入验证：
  // intent 必须产生"下文"，否则入口只是换了个页签。
  describe("导航意图落地", () => {
    const oneCustomer = {
      items: [
        {
          user_id: "user-1",
          username: "customer-1",
          created_at: "2026-08-24T10:00:00Z",
          activation_code: "ABC-123",
          status: "active",
        },
      ],
      total: 1,
      limit: 20,
      offset: 0,
    };

    it("guides the adjustment intent to the filter and the grant form", async () => {
      vi.mocked(adminApi.listCustomers).mockResolvedValue(oneCustomer);
      const scrollIntoView = vi.fn();
      Element.prototype.scrollIntoView = scrollIntoView;

      render(<CustomersPage initialIntent="customerAdjustments" />);

      expect(
        await screen.findByText(/赠送积分在客户详情内完成.*「赠送积分」区块/),
      ).toBeInTheDocument();
      expect(
        screen.getByPlaceholderText("按用户名或公司名称筛选"),
      ).toHaveFocus();

      fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
      // 意图自动切到「充值与积分」（赠送表单所在页签）并滚动定位。
      expect(
        await screen.findByRole("button", { name: "发放赠送积分" }),
      ).toBeInTheDocument();
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    });

    it("routes package and refund intents to their own sections (P0-1/P0-2)", async () => {
      vi.mocked(adminApi.listCustomers).mockResolvedValue(oneCustomer);
      const scrollIntoView = vi.fn();
      Element.prototype.scrollIntoView = scrollIntoView;

      const { unmount } = render(
        <CustomersPage initialIntent="customerPackage" />,
      );
      expect(
        await screen.findByText(/开通套餐（已收款）在客户详情内完成/),
      ).toBeInTheDocument();
      unmount();

      render(<CustomersPage initialIntent="customerRefund" />);
      expect(
        await screen.findByText(/退款扣减在客户详情内完成/),
      ).toBeInTheDocument();
      fireEvent.click(await screen.findByRole("button", { name: "展开详情" }));
      // 退款意图自动落「充值与积分」；顶部「开通套餐」按钮跨页签可达（P0-1）。
      expect(screen.queryByRole("button", { name: "后台加款" })).toBeNull();
      expect(
        await screen.findByRole("button", { name: "提交退款扣减" }),
      ).toBeInTheDocument();
      await waitFor(() => expect(scrollIntoView).toHaveBeenCalled());
    });

    it("keeps the adjustment guidance out of the auditor view", async () => {
      vi.mocked(adminApi.listCustomers).mockResolvedValue(oneCustomer);

      render(<CustomersPage initialIntent="customerAdjustments" readOnly />);

      await screen.findByText("customer-1");
      expect(
        screen.queryByText(/赠送积分在客户详情内完成/),
      ).not.toBeInTheDocument();
    });

    it("adds no guidance banner without an intent", async () => {
      vi.mocked(adminApi.listCustomers).mockResolvedValue(oneCustomer);

      render(<CustomersPage />);

      await screen.findByText("customer-1");
      expect(
        screen.queryByText(/赠送积分在客户详情内完成/),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByText(/激活码发放与查询不在客户管理页/),
      ).not.toBeInTheDocument();
      expect(
        screen.getByPlaceholderText("按用户名或公司名称筛选"),
      ).not.toHaveFocus();
    });
  });
});
