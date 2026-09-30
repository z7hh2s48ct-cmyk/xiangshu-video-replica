import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuditLogItem } from "../api.admin";
import { AuditEventsPage } from "./AuditEventsPage";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function auditItem(partial: Partial<AuditLogItem> = {}): AuditLogItem {
  return {
    event_id: "evt-1",
    event_type: "ADMIN_ADJUSTMENT",
    actor_user_id: "admin-1",
    actor_username: "admin_op",
    target_user_id: "customer-1",
    source_document_type: "CS_TICKET",
    source_document_ref: "manual-20260901-001",
    reason: "客户电话反馈补发",
    request_id: "req-audit-1",
    created_at: "2026-09-01T10:00:00+00:00",
    change_subject: null,
    old_unit_price_fen: null,
    new_unit_price_fen: null,
    change_detail: null,
    ...partial,
  };
}

const PAGE_SIZE = 20;

function installFetch(options?: { status?: number }) {
  const fetchMock = vi.fn((url: string) => {
    if (!String(url).includes("/api/control/audit-log")) {
      throw new Error(`unexpected request: ${url}`);
    }
    if (options?.status) {
      return jsonResponse({}, options.status);
    }
    const { searchParams } = new URL(String(url));
    const actor = searchParams.get("actor_user_id");
    const offset = Number(searchParams.get("offset") ?? "0");
    if (actor) {
      return jsonResponse({
        items: [auditItem({ actor_user_id: actor, actor_username: actor })],
        total: 1,
        limit: PAGE_SIZE,
        offset,
      });
    }
    if (offset >= PAGE_SIZE) {
      return jsonResponse({
        items: [
          auditItem({
            event_id: "evt-21",
            // 真实事件名（audit_logs.action）；此前夹具用的是后端从不产生的
            // CODE_REVEAL，掩盖了标签与实际事件名不匹配的问题。
            event_type: "admin.activation_code.revealed",
            target_user_id: "customer-9",
            request_id: "req-audit-21",
          }),
        ],
        total: 21,
        limit: PAGE_SIZE,
        offset,
      });
    }
    return jsonResponse({
      items: [auditItem()],
      total: 21,
      limit: PAGE_SIZE,
      offset,
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("AuditEventsPage", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("loads the first audit page and renders key row content", async () => {
    installFetch();
    render(<AuditEventsPage />);

    expect(await screen.findByText("管理员调账")).toBeInTheDocument();
    expect(screen.getByText("admin_op")).toBeInTheDocument();
    expect(screen.getByText("customer-1")).toBeInTheDocument();
    expect(screen.getByText("客户电话反馈补发")).toBeInTheDocument();
    expect(screen.getByText("req-audit-1")).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: "审计事件列表" }),
    ).toBeInTheDocument();

    // 21 rows over a 20-row page size expose the pagination controls.
    expect(screen.getByRole("navigation", { name: "分页" })).toBeVisible();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下一页" })).toBeEnabled();
  });

  it("renders billing tariff changes from the derived detail", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          items: [
            auditItem({
              event_id: "evt-tariff",
              event_type: "billing.tariff.update",
              change_subject: "oral",
              change_detail: {
                old: {
                  unit_credits: "0.25",
                  unit_cost_fen: "0.000125",
                  enabled: true,
                },
                new: {
                  unit_credits: "0.5",
                  unit_cost_fen: "0.00025",
                  enabled: true,
                },
              },
            }),
          ],
          total: 1,
          limit: PAGE_SIZE,
          offset: 0,
        }),
      ),
    );
    render(<AuditEventsPage />);

    expect(
      await screen.findByText(
        "oral：售价 0.25 → 0.5 积分 · 成本 < ¥0.01 → < ¥0.01",
      ),
    ).toBeInTheDocument();
  });

  it("describes first-time publication and enablement toggles", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          items: [
            auditItem({
              event_id: "evt-first",
              event_type: "billing.tariff.update",
              change_subject: "quality_inspection",
              change_detail: {
                old: null,
                new: {
                  unit_credits: null,
                  unit_cost_fen: "0.5",
                  enabled: false,
                },
              },
            }),
            auditItem({
              event_id: "evt-toggle",
              event_type: "billing.tariff.update",
              change_subject: "oral",
              change_detail: {
                old: { unit_credits: "1", unit_cost_fen: "0.5", enabled: true },
                new: {
                  unit_credits: "1",
                  unit_cost_fen: "0.5",
                  enabled: false,
                },
              },
            }),
          ],
          total: 2,
          limit: PAGE_SIZE,
          offset: 0,
        }),
      ),
    );
    render(<AuditEventsPage />);

    expect(
      await screen.findByText("quality_inspection：初始配置 · 成本 < ¥0.01"),
    ).toBeInTheDocument();
    expect(screen.getByText("oral：停用用户扣费")).toBeInTheDocument();
  });

  it("requests offset=20 when moving to the next page", async () => {
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("管理员调账");
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    await screen.findByText("查看激活码明文");
    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => {
          const requestUrl = new URL(String(url));
          return (
            requestUrl.pathname.endsWith("/api/control/audit-log") &&
            requestUrl.searchParams.get("limit") === "20" &&
            requestUrl.searchParams.get("offset") === "20"
          );
        }),
      ).toBe(true);
    });
    expect(screen.getByText("req-audit-21")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
    expect(screen.getByText(/第 2 \/ 2 页（共 21 条）/)).toBeInTheDocument();
  });

  it("sends the actor filter only on submit, not while typing", async () => {
    // 整改清单 评估登记 5 的回归锁定：输入不触发请求，提交只发一次。
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("管理员调账");
    const requestsAfterLoad = fetchMock.mock.calls.length;

    fireEvent.change(screen.getByLabelText("操作人用户名"), {
      target: { value: "admin_u" },
    });
    expect(fetchMock.mock.calls.length).toBe(requestsAfterLoad);

    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(
          ([url]) =>
            String(url).includes("/api/control/audit-log?") &&
            String(url).includes("actor_username=admin_u"),
        ),
      ).toBe(true);
    });
  });

  it("offers the CSV export to writers but hides it from read-only roles", async () => {
    installFetch();
    const { unmount } = render(<AuditEventsPage />);
    expect(
      await screen.findByRole("button", { name: "导出 CSV" }),
    ).toBeInTheDocument();
    unmount();

    // auditor 没有导出权限：不渲染入口，而不是渲染出来点了才 403。
    installFetch();
    render(<AuditEventsPage readOnly />);
    await screen.findByText("管理员调账");
    expect(screen.queryByRole("button", { name: "导出 CSV" })).toBeNull();
    // 列表与筛选仍然可用。
    expect(screen.getByRole("button", { name: "重置" })).toBeInTheDocument();
  });

  it("defaults the audit scope to admin actions", async () => {
    // P0-3：服务端默认只回管理员动作；客户端显式带上同一口径，避免
    // 「默认值在哪一侧」的隐性依赖。
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("管理员调账");
    const { searchParams } = new URL(String(fetchMock.mock.calls[0]?.[0]));
    expect(searchParams.get("scope")).toBe("admin");
  });

  it("switches to the customer scope on submit (P0-3)", async () => {
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("管理员调账");
    fireEvent.change(screen.getByLabelText("审计范围"), {
      target: { value: "customer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    await waitFor(() => {
      expect(
        fetchMock.mock.calls.some(([url]) => {
          const requestUrl = new URL(String(url));
          return (
            requestUrl.pathname.endsWith("/api/control/audit-log") &&
            requestUrl.searchParams.get("scope") === "customer"
          );
        }),
      ).toBe(true);
    });
  });

  it("keeps dotted event filters exact and renders a known price change", async () => {
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("管理员调账");
    fireEvent.change(screen.getByLabelText("事件类型"), {
      target: { value: "operation_rate.update" },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes("event_type=operation_rate.update"),
        ),
      ).toBe(true),
    );
  });

  it("does not invent an old price for historical events", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          items: [
            auditItem({
              event_type: "customer_unit_price.update",
              change_subject: "customer_unit_price",
              old_unit_price_fen: null,
              new_unit_price_fen: 15,
            }),
          ],
          total: 1,
          limit: 20,
          offset: 0,
        }),
      ),
    );
    render(<AuditEventsPage />);

    expect(await screen.findByText("设置为 ¥0.15 /秒")).toBeInTheDocument();
    expect(screen.queryByText(/0 分\/秒/)).toBeNull();
  });

  it("shows a price transition and keeps full audit references in titles", async () => {
    const sourceRef = "source-document-reference-20260905-0001";
    const requestId = "request-id-audit-operation-rate-update-0001";
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          items: [
            auditItem({
              event_type: "operation_rate.update",
              change_subject: "video_generation_768p",
              old_unit_price_fen: 9,
              new_unit_price_fen: 12,
              source_document_ref: sourceRef,
              request_id: requestId,
            }),
          ],
          total: 1,
          limit: 20,
          offset: 0,
        }),
      ),
    );
    render(<AuditEventsPage />);

    expect(await screen.findByText("¥0.09 → ¥0.12 /秒")).toBeInTheDocument();
    expect(screen.getByTitle(`CS_TICKET / ${sourceRef}`)).toBeInTheDocument();
    expect(screen.getByTitle(requestId)).toBeInTheDocument();
  });

  it("shows the rate unit that matches each changed subject", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          items: [
            auditItem({
              event_id: "image-rate",
              event_type: "operation_rate.update",
              change_subject: "first_frame_image",
              old_unit_price_fen: 1,
              new_unit_price_fen: 2,
            }),
            auditItem({
              event_id: "sheet-rate",
              event_type: "operation_rate.update",
              change_subject: "character_sheet_image",
              old_unit_price_fen: 3,
              new_unit_price_fen: 4,
            }),
            auditItem({
              event_id: "view-rate",
              event_type: "operation_rate.update",
              change_subject: "character_view",
              old_unit_price_fen: 5,
              new_unit_price_fen: 6,
            }),
            auditItem({
              event_id: "context-rate",
              event_type: "operation_rate.update",
              change_subject: "context_ir",
              old_unit_price_fen: 7,
              new_unit_price_fen: 8,
            }),
            auditItem({
              event_id: "unknown-rate",
              event_type: "operation_rate.update",
              change_subject: "future_subject",
              old_unit_price_fen: 9,
              new_unit_price_fen: 10,
            }),
            auditItem({
              event_id: "customer-rate",
              event_type: "customer_unit_price.update",
              change_subject: "customer_unit_price",
              old_unit_price_fen: 11,
              new_unit_price_fen: 12,
            }),
          ],
          total: 6,
          limit: 20,
          offset: 0,
        }),
      ),
    );
    render(<AuditEventsPage />);

    expect(await screen.findByText("¥0.01 → ¥0.02 /张")).toBeInTheDocument();
    expect(screen.getByText("¥0.03 → ¥0.04 /张")).toBeInTheDocument();
    expect(screen.getByText("¥0.05 → ¥0.06 /张")).toBeInTheDocument();
    expect(screen.getByText("¥0.07 → ¥0.08 /次")).toBeInTheDocument();
    expect(screen.getByText("¥0.09 → ¥0.10")).toBeInTheDocument();
    expect(screen.getByText("¥0.11 → ¥0.12 /秒")).toBeInTheDocument();
  });

  it("resets all submitted filters", async () => {
    const fetchMock = installFetch();
    render(<AuditEventsPage />);

    await screen.findByText("管理员调账");
    fireEvent.change(screen.getByLabelText("操作人用户名"), {
      target: { value: "admin_u" },
    });
    fireEvent.click(screen.getByRole("button", { name: "筛选" }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) => String(url).includes("admin_u")),
      ).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "重置" }));

    await waitFor(() =>
      expect(screen.getByLabelText("操作人用户名")).toHaveValue(""),
    );
  });

  it("shows the load failure as an alert", async () => {
    installFetch({ status: 500 });
    render(<AuditEventsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("加载失败：读取审计日志失败（500）");
  });

  it("offers the real activation-code, batch and session event types for filtering", async () => {
    installFetch();
    render(<AuditEventsPage />);
    await screen.findByText("管理员调账");

    // 下拉项的值必须是后端真实产生的 event_type。此前"查看激活码明文"
    // 只存在于标签映射里（且键是后端从不产生的 CODE_REVEAL），运营选不到；
    // 批次创建与管理员下线也没有任何入口。
    const options = (name: string) => screen.getByRole("option", { name });
    expect(options("查看激活码明文")).toHaveValue(
      "admin.activation_code.revealed",
    );
    expect(options("归档激活码")).toHaveValue("admin.activation_code.archived");
    expect(options("创建激活码批次")).toHaveValue(
      "admin.activation_code_batch.created",
    );
    expect(options("管理员下线")).toHaveValue("ADMIN_SESSION_LOGOUT");
  });

  it("offers the sensitive admin actions the server already records (P0-4)", async () => {
    installFetch();
    render(<AuditEventsPage />);
    await screen.findByText("管理员调账");

    const options = (name: string) => screen.getByRole("option", { name });
    expect(options("查看密钥明文")).toHaveValue(
      "provider_settings.secret_reveal",
    );
    expect(options("数据导出")).toHaveValue("control.export");
    expect(options("开通套餐（线下收款）")).toHaveValue(
      "customer_package.grant",
    );
    expect(options("设置专项折扣")).toHaveValue("customer_discount.create");
    expect(options("停用专项折扣")).toHaveValue("customer_discount.deactivate");
    expect(options("修改充值套餐")).toHaveValue("recharge_package.update");
    expect(options("查单同步")).toHaveValue("payment.sync");
    expect(options("管理员密码登录")).toHaveValue(
      "admin_session.password_login",
    );
  });

  it("labels an administrator-forced session revoke as an administrator session action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        jsonResponse({
          items: [
            auditItem({
              event_id: "evt-session-1",
              event_type: "ADMIN_SESSION_LOGOUT",
              actor_username: "admin_op",
              target_user_id: "customer-7",
              source_document_type: "CUSTOMER_SESSION",
              source_document_ref: "sess-7",
              reason: "客服确认账号异常",
              request_id: "req-audit-session",
            }),
            // 未列入下拉选项的会话事件走族回退标签。
            auditItem({
              event_id: "evt-session-2",
              event_type: "ADMIN_SESSION_SWITCH",
              actor_username: "admin_op",
              target_user_id: "customer-7",
              source_document_type: "CUSTOMER_SESSION",
              source_document_ref: "sess-8",
              reason: "换设备",
              request_id: "req-audit-session-2",
            }),
          ],
          total: 2,
          limit: PAGE_SIZE,
          offset: 0,
        }),
      ),
    );

    render(<AuditEventsPage />);

    // 选项表里的具体标签优先于 ADMIN_SESSION_ 族回退。
    expect(await screen.findByText("管理员下线")).toBeInTheDocument();
    expect(screen.getByText("客服确认账号异常")).toBeInTheDocument();
    // 来源单列渲染成"类型 / 引用"的组合串（超长会截断），故用正则。
    expect(screen.getByText(/sess-7/)).toBeInTheDocument();
    expect(screen.getByText("管理员会话操作")).toBeInTheDocument();
    expect(screen.getByText(/sess-8/)).toBeInTheDocument();
  });
});
