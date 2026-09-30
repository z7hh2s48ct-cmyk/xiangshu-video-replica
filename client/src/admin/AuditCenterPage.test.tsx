import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { AuditCenterPage } from "./AuditCenterPage";

// 审计中心在方案 P1 后只剩「审计日志」单页：调账记录迁入资金中心·人工调整
// （一件事只有一个入口），容器页不再承担页签职责。调整记录的接线由
// FundsPage.test.tsx 钉住。
vi.mock("./AuditEventsPage", () => ({
  AuditEventsPage: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-readonly={String(Boolean(readOnly))} data-testid="audit-events">
      审计事件
    </div>
  ),
}));

test("renders the audit log page directly without an adjustments tab", () => {
  render(<AuditCenterPage />);

  expect(screen.getByTestId("audit-events")).toBeInTheDocument();
  expect(screen.getByTestId("audit-events")).toHaveAttribute(
    "data-readonly",
    "false",
  );
});

test("passes the read-only state down to the audit log page", () => {
  render(<AuditCenterPage readOnly />);

  expect(screen.getByTestId("audit-events")).toHaveAttribute(
    "data-readonly",
    "true",
  );
});
