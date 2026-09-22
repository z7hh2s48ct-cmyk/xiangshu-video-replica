import { useState } from "react";
import {
  getControlSettings,
  testControlProviderConnection,
  updateControlBillingSettings,
  updateControlProviderSettings,
  updateControlRuntimeSettings,
} from "../api";
import { type SettingsBackend, SettingsPanel } from "../SettingsPanel";
import { AdminEnvironmentSwitch } from "./AdminEnvironmentSwitch";
import { BillingRatesManager } from "./BillingRatesManager";
import { CustomerPricingManager } from "./CustomerPricingManager";
import { H3AccountsManager } from "./H3AccountsManager";
import { PaymentSettingsSection } from "./PaymentSettingsSection";
import { QueueModeSection } from "./QueueModeSection";
import { RechargePackageManager } from "./RechargePackageManager";
import { TabBar } from "./ui/TabBar";
import { ViralRuntimeSection } from "./ViralRuntimeSection";

const tabs = [
  { id: "payment", label: "支付与价格" },
  { id: "rates", label: "API 端点与价格" },
  { id: "services", label: "服务配置" },
];

/**
 * 控制面设置后端：走 `/api/control/settings`（内部通道，由反代注入
 * `X-Control-Proxy-Token`）。只由本管理端页面注入给 SettingsPanel，
 * 因此这些控制面 API 仅出现在管理构建制品（`client/dist-admin`），
 * 客户构建制品的依赖图不可达（CW-019）。
 */
const controlBackend: SettingsBackend = {
  load: getControlSettings,
  saveProvider: updateControlProviderSettings,
  saveRuntime: updateControlRuntimeSettings,
  saveBilling: updateControlBillingSettings,
  testProvider: testControlProviderConnection,
};

/**
 * v4 导航合并 — 系统设置：支付与价格、费率管理、服务配置合并为一个菜单项。
 * 费率管理承载上游成本费率（按科目/分辨率）与对外售价（按秒）配置。
 */
export function SystemSettingsPage({
  readOnly = false,
  initialTab = "payment",
}: {
  readOnly?: boolean;
  initialTab?: "payment" | "rates" | "services";
}) {
  const [tab, setTab] = useState<string>(initialTab);
  const [serviceTab, setServiceTab] = useState("providers");
  return (
    <div>
      <TabBar
        active={tab}
        ariaLabel="系统设置页签"
        items={tabs}
        onChange={setTab}
      />
      {tab === "payment" ? (
        <>
          <CustomerPricingManager readOnly={readOnly} />
          <RechargePackageManager readOnly={readOnly} />
          <PaymentSettingsSection readOnly={readOnly} />
        </>
      ) : null}
      {tab === "rates" ? <BillingRatesManager readOnly={readOnly} /> : null}
      {tab === "services" ? (
        <div className="admin-services">
          <AdminEnvironmentSwitch readOnly={readOnly} />
          <header className="admin-services__header">
            <TabBar
              active={serviceTab}
              ariaLabel="服务配置分组"
              items={[
                { id: "providers", label: "API 服务" },
                { id: "runtime", label: "运行控制" },
              ]}
              onChange={setServiceTab}
            />
          </header>
          {serviceTab === "providers" ? (
            <SettingsPanel
              controlBackend={controlBackend}
              readOnly={readOnly}
              source="control"
              section="providers"
              videoAccounts={<H3AccountsManager readOnly={readOnly} />}
            />
          ) : (
            <div className="admin-services__runtime">
              <div className="admin-services__switches">
                <QueueModeSection readOnly={readOnly} />
              </div>
              <SettingsPanel
                controlBackend={controlBackend}
                readOnly={readOnly}
                source="control"
                section="runtime"
              />
              <ViralRuntimeSection readOnly={readOnly} />
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
