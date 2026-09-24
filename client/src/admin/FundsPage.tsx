import { useState } from "react";

import { AccountsPage } from "./AccountsPage";
import { OrdersPage } from "./OrdersPage";
import { TabBar } from "./ui/TabBar";

const tabs = [
  { id: "orders", label: "充值订单" },
  { id: "transactions", label: "额度流水" },
];

/**
 * v4 导航合并 — 资金流水：充值订单与额度流水合并为一个菜单项，
 * 以页签切换，收支付款与账本变动的对账视图保持各自组件不变。
 */
export function FundsPage({ readOnly = false }: { readOnly?: boolean }) {
  const [tab, setTab] = useState("orders");
  return (
    <div>
      <TabBar
        active={tab}
        ariaLabel="资金流水页签"
        items={tabs}
        onChange={setTab}
      />
      {tab === "orders" ? <OrdersPage readOnly={readOnly} /> : <AccountsPage />}
    </div>
  );
}
