import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "../api.admin";
import { AlertDeliveryStatus } from "./AlertDeliveryStatus";
import { AlertRuleEditor } from "./AlertRuleEditor";

vi.mock("../api.admin", () => ({ adminRead: vi.fn() }));
beforeEach(() => vi.clearAllMocks());

it("edits category routes and independent code thresholds without changing recipients", () => {
  const policies = vi.fn();
  const rules = vi.fn();
  render(
    <AlertRuleEditor
      candidates={[]}
      policies={[]}
      rules={[
        {
          record_type: "VIDEO",
          error_code: "X",
          threshold_percent: 20,
          min_sample_size: 10,
        },
      ]}
      onPolicies={policies}
      onRules={rules}
    />,
  );
  fireEvent.change(screen.getByLabelText("高敏操作数量阈值"), {
    target: { value: "3" },
  });
  const changed = policies.mock.calls[0][0];
  expect(
    changed.find(
      (p: api.AlertNotificationPolicy) => p.key === "sensitive_events",
    ).threshold_count,
  ).toBe(3);
  expect(
    changed.every(
      (p: api.AlertNotificationPolicy) => p.recipient_user_id === null,
    ),
  ).toBe(true);
  fireEvent.change(screen.getByLabelText("采集预算80%通知通道"), {
    target: { value: "" },
  });
  expect(
    policies.mock.calls[1][0].find(
      (p: api.AlertNotificationPolicy) => p.key === "collection_budget",
    ).channel,
  ).toBeNull();
  fireEvent.change(screen.getByLabelText("规则1失败率（%）"), {
    target: { value: "25" },
  });
  expect(rules).toHaveBeenCalledWith([
    {
      record_type: "VIDEO",
      error_code: "X",
      threshold_percent: 25,
      min_sample_size: 10,
    },
  ]);
});

it("shows audit readers static configuration without edit controls", () => {
  render(
    <AlertRuleEditor
      readOnly
      candidates={[]}
      onPolicies={vi.fn()}
      onRules={vi.fn()}
    />,
  );
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("shows missing configuration and failed delivery; refresh only reads metadata", async () => {
  vi.mocked(api.adminRead).mockResolvedValue({
    configuration: [
      {
        key: "collection_budget",
        enabled: true,
        channel_configured: false,
        recipient_configured: false,
        recipient_display_name: null,
      },
    ],
    items: [
      {
        id: "fake",
        alert_key: "sensitive_events",
        state: "FAILED",
        attempts: 2,
        last_error: "EMAIL_DELIVERY_FAILED",
        recipient_display_name: "合成管理人",
      },
    ],
  });
  render(<AlertDeliveryStatus />);
  expect(await screen.findByText(/发送失败，后台将重试/)).toBeInTheDocument();
  expect(screen.getByText(/采集预算80%.*待配置/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "刷新通知状态" }));
  await waitFor(() => expect(api.adminRead).toHaveBeenCalledTimes(2));
  expect(api.adminRead).toHaveBeenLastCalledWith(
    "/api/control/alerts/deliveries",
    "读取通知状态失败",
  );
});
