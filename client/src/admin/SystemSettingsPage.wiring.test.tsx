import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { expect, test, vi } from "vitest";

import { SystemSettingsPage } from "./SystemSettingsPage";

// 系统设置是「v4 导航合并」后的容器页：四个页签、服务配置下还套一层子页签，
// 并把只读态与**控制面设置后端**注入给共用组件 SettingsPanel（CW-019：客户
// 构建制品的依赖图不可达，控制面 API 只出现在管理制品里）。
// 各区块各自已有独立测试，这里只钉这层接线 —— 它此前完全无覆盖。
vi.mock("./CustomerPricingManager", () => ({
  CustomerPricingManager: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="customer-pricing">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./RechargePackageManager", () => ({
  RechargePackageManager: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="recharge-packages">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./PaymentSettingsSection", () => ({
  PaymentSettingsSection: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="payment-settings">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./BillingRatesManager", () => ({
  BillingRatesManager: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="billing-rates">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./AdminEnvironmentSwitch", () => ({
  AdminEnvironmentSwitch: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="environment-switch">{`ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./H3AccountsManager", () => ({
  H3AccountsManager: () => <div data-testid="h3-accounts">视频账号</div>,
}));
vi.mock("./QueueModeSection", () => ({
  QueueModeSection: () => <div data-testid="queue-mode">排队模式</div>,
}));
vi.mock("./ViralRuntimeSection", () => ({
  ViralRuntimeSection: () => <div data-testid="viral-runtime">爆款运行时</div>,
}));
vi.mock("./AdminAlertsSection", () => ({
  AdminAlertsSection: ({ readOnly }: { readOnly?: boolean }) => (
    <div data-testid="admin-alerts">{`失败率告警 ro=${String(readOnly)}`}</div>
  ),
}));
vi.mock("./TeamManagementSection", () => ({
  TeamManagementSection: ({ currentUserId }: { currentUserId: string }) => (
    <div data-testid="team-management">{`团队 me=${currentUserId}`}</div>
  ),
}));
vi.mock("../SettingsPanel", () => ({
  SettingsPanel: ({
    source,
    section,
    videoAccounts,
  }: {
    source?: string;
    section?: string;
    videoAccounts?: ReactNode;
  }) => (
    <div data-testid="settings-panel">
      {`source=${String(source)} section=${String(section)}`}
      {videoAccounts}
    </div>
  ),
}));

test("defaults to the pricing tab with every block writable", () => {
  render(<SystemSettingsPage />);

  expect(
    screen.getByRole("tablist", { name: "系统设置页签" }),
  ).toBeInTheDocument();
  // 价格与套餐一页化（方案 P1）：定价、套餐、费率、试算器同页挂载。
  expect(screen.getByTestId("customer-pricing")).toHaveTextContent("ro=false");
  expect(screen.getByTestId("recharge-packages")).toHaveTextContent("ro=false");
  expect(screen.getByTestId("billing-rates")).toHaveTextContent("ro=false");
  // 其它页签的内容不该提前挂载（各区块都会各自发请求）。
  expect(screen.queryByTestId("payment-settings")).toBeNull();
  expect(screen.queryByTestId("settings-panel")).toBeNull();
});

test("honours the initialTab prop", () => {
  // 旧 rates 意图归并到「价格与套餐」：定价区块随之挂载。
  render(<SystemSettingsPage initialTab="rates" />);

  expect(screen.getByTestId("billing-rates")).toBeInTheDocument();
  expect(screen.getByTestId("customer-pricing")).toBeInTheDocument();
});

test("mounts the alerts tab only when selected", () => {
  render(<SystemSettingsPage initialTab="alerts" />);

  expect(screen.getByTestId("admin-alerts")).toBeInTheDocument();
  expect(screen.queryByTestId("customer-pricing")).toBeNull();
  expect(screen.queryByTestId("settings-panel")).toBeNull();
});

test("forwards readOnly into the pricing blocks", () => {
  render(<SystemSettingsPage readOnly />);

  expect(screen.getByTestId("customer-pricing")).toHaveTextContent("ro=true");
  expect(screen.getByTestId("recharge-packages")).toHaveTextContent("ro=true");
  expect(screen.getByTestId("billing-rates")).toHaveTextContent("ro=true");
  // 收款设置在独立页签：切过去后同样收到只读态。
  fireEvent.click(screen.getByRole("tab", { name: "收款设置" }));
  expect(screen.getByTestId("payment-settings")).toHaveTextContent("ro=true");
});

test("injects the control-plane backend into the shared settings panel", () => {
  render(<SystemSettingsPage initialTab="services" isSuperAdmin />);

  const panel = screen.getByTestId("settings-panel");
  // CW-019：管理端必须走 source="control"（SettingsPanel 据此拒绝 reveal 已保存
  // 密钥等仅内部车道可用的动作），且后端由本页注入而非 SettingsPanel 静态引用。
  expect(panel).toHaveTextContent("source=control section=providers");
  // 视频账号管理作为插槽传进同一面板。
  expect(within(panel).getByTestId("h3-accounts")).toBeInTheDocument();
  expect(screen.getByTestId("environment-switch")).toHaveTextContent(
    "ro=false",
  );
});

test("switches the service sub-tab to the runtime controls", () => {
  render(<SystemSettingsPage initialTab="services" isSuperAdmin />);

  fireEvent.click(screen.getByRole("tab", { name: "运行控制" }));

  expect(screen.getByTestId("settings-panel")).toHaveTextContent(
    "section=runtime",
  );
  expect(screen.getByTestId("queue-mode")).toBeInTheDocument();
  expect(screen.queryByTestId("h3-accounts")).toBeNull();
  // 采集设置已迁到内容组（方案 P1 内容模块），技术配置里不再出现。
  expect(screen.queryByTestId("viral-runtime")).toBeNull();
});

test("keeps the service sub-tab selection inside the services tab", () => {
  render(<SystemSettingsPage initialTab="services" isSuperAdmin />);

  fireEvent.click(screen.getByRole("tab", { name: "运行控制" }));
  fireEvent.click(screen.getByRole("tab", { name: "价格与套餐" }));
  // 离开服务配置再回来，子页签保持上一次的选择（状态由本页持有）。
  fireEvent.click(screen.getByRole("tab", { name: "技术配置" }));

  expect(screen.getByTestId("settings-panel")).toHaveTextContent(
    "section=runtime",
  );
});

test("forwards readOnly into the alerts tab so auditors get the read-only settings", () => {
  render(<SystemSettingsPage initialTab="alerts" readOnly />);

  expect(screen.getByTestId("admin-alerts")).toHaveTextContent("ro=true");
});

test("hides the team tab from everyone but super admins", () => {
  render(<SystemSettingsPage />);

  expect(screen.queryByRole("tab", { name: "团队与权限" })).toBeNull();
});

test("shows the team tab to super admins and passes the signed-in user down", () => {
  render(<SystemSettingsPage currentUserId="u-root" isSuperAdmin />);

  // 未选中时不提前挂载（团队页挂载即发请求）。
  expect(screen.queryByTestId("team-management")).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "团队与权限" }));

  expect(screen.getByTestId("team-management")).toHaveTextContent("me=u-root");
});

test("never mounts the team page for a non-super admin, even via initialTab", () => {
  render(<SystemSettingsPage initialTab="team" />);

  expect(screen.queryByTestId("team-management")).toBeNull();
  // 回落到默认页签，而不是留一个空白页。
  expect(screen.getByTestId("customer-pricing")).toBeInTheDocument();
});
