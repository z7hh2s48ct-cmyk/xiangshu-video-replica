import type { ReactNode } from "react";

export type TabBarItem = { id: string; label: string };

/**
 * v4 导航合并 — 合并式页面顶部的页签行。
 * 左侧为页签（金色下划线高亮当前页），右侧为可选动作插槽
 * （例如激活码页签的「生成激活码」主按钮）。
 */
export function TabBar({
  items,
  active,
  onChange,
  actions,
  ariaLabel,
}: {
  items: TabBarItem[];
  active: string;
  onChange: (id: string) => void;
  actions?: ReactNode;
  ariaLabel: string;
}) {
  return (
    <div className="admin-page-tabs">
      <div
        aria-label={ariaLabel}
        className="admin-page-tabs__items"
        role="tablist"
      >
        {items.map((item) => (
          <button
            aria-selected={active === item.id}
            className={
              active === item.id ? "admin-page-tab is-active" : "admin-page-tab"
            }
            key={item.id}
            role="tab"
            type="button"
            onClick={() => onChange(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>
      {actions ? (
        <div className="admin-page-tabs__actions">{actions}</div>
      ) : null}
    </div>
  );
}
