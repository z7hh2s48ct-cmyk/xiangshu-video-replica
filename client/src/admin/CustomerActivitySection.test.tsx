import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AuditLogItem } from "../api.admin";
import { CustomerActivitySection } from "./CustomerActivitySection";

function jsonResponse(payload: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

const CUSTOMER_ID = "customer-7";

function activityItem(partial: Partial<AuditLogItem> = {}): AuditLogItem {
  return {
    event_id: "evt-1",
    event_type: "analysis.create",
    actor_user_id: CUSTOMER_ID,
    actor_username: "customer_u",
    target_user_id: CUSTOMER_ID,
    target_username: "customer_u",
    source_document_type: "VIDEO",
    source_document_ref: "material-1",
    reason: "",
    request_id: "req-activity-1",
    created_at: "2026-09-20T10:00:00+00:00",
    change_subject: null,
    old_unit_price_fen: null,
    new_unit_price_fen: null,
    change_detail: null,
    ...partial,
  };
}

describe("CustomerActivitySection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("merges customer actions and admin dispositions by stable customer id", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse({
        items: [
          activityItem(),
          activityItem({
            event_id: "evt-admin",
            event_type: "customer.suspend",
            actor_username: "operator",
            reason: "客户要求暂停",
          }),
        ],
        total: 2,
        limit: 20,
        offset: 0,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<CustomerActivitySection userId={CUSTOMER_ID} />);

    // 客户动作没有中文词典，直接以原始动作名落表（title 提供完整值）。
    expect(await screen.findByText("创建视频拆解")).toBeInTheDocument();
    expect(screen.getByText("共 2 条")).toBeInTheDocument();
    expect(screen.getByText("暂停客户")).toBeInTheDocument();
    expect(screen.getByText("operator")).toBeInTheDocument();

    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain("/api/control/audit-log?");
    expect(url).toContain("scope=all");
    expect(url).toContain(`target_user_id=${CUSTOMER_ID}`);
  });

  it("shows an empty hint when the customer has no recorded action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({ items: [], total: 0, limit: 20, offset: 0 })),
    );

    render(<CustomerActivitySection userId={CUSTOMER_ID} />);

    expect(await screen.findByText("该客户暂无操作记录。")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("surfaces a load failure as an alert", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => jsonResponse({}, 503)),
    );

    render(<CustomerActivitySection userId={CUSTOMER_ID} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "加载操作记录失败：读取审计日志失败（503）",
    );
  });
});

it("shows measured inherited and custom prices without inventing old default amounts", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      jsonResponse({
        items: [
          activityItem({
            event_id: "first-price",
            event_type: "customer_unit_price.update",
            change_detail: {
              changes: {
                customer_unit_price: {
                  before: {
                    mode: "DEFAULT",
                    custom_unit_price_fen: null,
                    effective_unit_price_fen: 1000,
                  },
                  after: {
                    mode: "CUSTOM",
                    custom_unit_price_fen: 1100,
                    effective_unit_price_fen: 1100,
                  },
                },
              },
            },
          }),
          activityItem({
            event_id: "reset-price",
            event_type: "customer_unit_price.reset",
            change_detail: {
              changes: {
                customer_unit_price: {
                  before: {
                    mode: "CUSTOM",
                    custom_unit_price_fen: 1100,
                    effective_unit_price_fen: 1100,
                  },
                  after: {
                    mode: "DEFAULT",
                    custom_unit_price_fen: null,
                    effective_unit_price_fen: 1000,
                  },
                },
              },
            },
          }),
          activityItem({
            event_id: "legacy-price",
            event_type: "customer_unit_price.update",
            change_detail: {
              changes: {
                customer_unit_price: {
                  before: {
                    mode: "DEFAULT",
                    custom_unit_price_fen: null,
                    effective_unit_price_fen: null,
                  },
                  after: {
                    mode: "CUSTOM",
                    custom_unit_price_fen: 1200,
                    effective_unit_price_fen: 1200,
                  },
                },
              },
            },
          }),
          activityItem({
            event_id: "missing-price",
            event_type: "customer_unit_price.update",
          }),
        ],
        total: 4,
        limit: 20,
        offset: 0,
      }),
    ),
  );
  render(<CustomerActivitySection userId={CUSTOMER_ID} />);
  expect(
    await screen.findByText(
      "未配置自定义价（继承默认，¥10.00） → 自定义价 ¥11.00",
    ),
  ).toBeInTheDocument();
  expect(
    screen.getByText("自定义价 ¥11.00 → 未配置自定义价（继承默认，¥10.00）"),
  ).toBeInTheDocument();
  expect(
    screen.getByText(
      "未配置自定义价（继承默认，历史金额未记录） → 自定义价 ¥12.00",
    ),
  ).toBeInTheDocument();
  expect(screen.getByText("未记录前后值")).toBeInTheDocument();
  vi.unstubAllGlobals();
});
