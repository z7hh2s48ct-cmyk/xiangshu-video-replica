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

  it("reads the customer's own actions via scope=customer + target_user_id", async () => {
    const fetchMock = vi.fn((_url: string, _init?: RequestInit) =>
      jsonResponse({ items: [activityItem()], total: 1, limit: 20, offset: 0 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<CustomerActivitySection userId={CUSTOMER_ID} />);

    // 客户动作没有中文词典，直接以原始动作名落表（title 提供完整值）。
    expect(await screen.findByText("analysis.create")).toBeInTheDocument();
    expect(screen.getByText("共 1 条")).toBeInTheDocument();

    const url = String(fetchMock.mock.calls[0]?.[0]);
    expect(url).toContain("/api/control/audit-log?");
    expect(url).toContain("scope=customer");
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
