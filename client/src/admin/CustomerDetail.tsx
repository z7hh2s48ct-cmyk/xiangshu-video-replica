import { useEffect, useState } from "react";
import type { AdjustmentWriteResult, CustomerListItem } from "../api.admin";
import { AccountCreditPanel } from "./AccountCreditPanel";
import { BillingQuoteCalculator } from "./BillingQuoteCalculator";
import { CustomerActivitySection } from "./CustomerActivitySection";
import { CustomerBenefitsSection } from "./CustomerBenefitsSection";
import { CustomerDeviceSection } from "./CustomerDeviceSection";
import { CustomerRefundSection } from "./CustomerRefundSection";
import { GenerationRecordsPage } from "./GenerationRecordsPage";
import { CopyCustomerId } from "./ui/CopyCustomerId";
import { PageBanner } from "./ui/PageBanner";
import { CustomerStatusBadge } from "./ui/StatusBadge";
import { TabBar } from "./ui/TabBar";
import { formatDateTime } from "./ui/vocabulary";
import "./admin-customer-detail.css";

import { CustomerAnnotationSection } from "./CustomerAnnotationSection";
import { Customer360Data, CustomerSuspendButton } from "./CustomerFundsSection";
import { companyNameOf, scrollToSection } from "./CustomerIdentity";
import { CustomerOverview } from "./CustomerOverview";
import { CustomerPriceEditor } from "./CustomerPriceEditor";
import { FreeCreditsSection } from "./FreeCreditsSection";
import { formatFen } from "./ui/vocabulary";

const CUSTOMER_DETAIL_TABS = [
  { id: "overview", label: "概览" },
  { id: "funds", label: "充值与积分" },
  { id: "records", label: "生成记录" },
  { id: "devices", label: "登录与设备" },
  { id: "benefits", label: "价格与权益" },
  { id: "activity", label: "操作记录" },
] as const;

type CustomerDetailTab = (typeof CUSTOMER_DETAIL_TABS)[number]["id"];

/** 资金操作目标区块 → 所属页签：顶部主操作按钮先切页签再滚动定位。 */
const SECTION_TAB: Record<string, CustomerDetailTab> = {
  "customer-benefits": "benefits",
  "customer-free-grant": "funds",
  "customer-refund": "funds",
  "customer-annotation": "overview",
  "customer-activity": "activity",
  "customer-devices": "devices",
};

export function CustomerDetail({
  customer,
  focusSectionId,
  operatorId,
  onChanged,
  onGranted,
  readOnly,
  refreshError,
  onBack,
}: {
  customer: CustomerListItem;
  focusSectionId: string | null;
  operatorId: string;
  onChanged: () => void;
  onGranted: (result: AdjustmentWriteResult) => void;
  readOnly: boolean;
  refreshError: string;
  onBack: () => void;
}) {
  // 带资金意图进入时，先切到对应页签，展开客户即直达表单。
  const [tab, setTab] = useState<CustomerDetailTab>(
    (focusSectionId && SECTION_TAB[focusSectionId]) || "overview",
  );
  const [pendingSection, setPendingSection] = useState<string | null>(
    focusSectionId,
  );
  // 切页签后再滚动：目标区块随页签挂载，同帧滚动会落空。
  // setTab 与 setPendingSection 批量提交，effect 在重渲染后执行一次，
  // 因此这里只依赖 pendingSection 即可，无需把 tab 列进来。
  useEffect(() => {
    if (!pendingSection) return;
    scrollToSection(pendingSection);
    setPendingSection(null);
  }, [pendingSection]);
  useEffect(() => {
    if (!focusSectionId) return;
    setTab(SECTION_TAB[focusSectionId] ?? "overview");
    setPendingSection(focusSectionId);
  }, [focusSectionId]);

  function focusSection(sectionId: string) {
    const target = SECTION_TAB[sectionId];
    if (target && target !== tab) {
      setTab(target);
      setPendingSection(sectionId);
    } else {
      scrollToSection(sectionId);
    }
  }

  return (
    <div
      className="customers-page customer-focused-detail"
      id={`customer-detail-${customer.user_id}`}
    >
      {refreshError ? (
        <PageBanner tone="error">{refreshError}</PageBanner>
      ) : null}
      <button
        className="customer-detail-back btn-secondary"
        type="button"
        onClick={onBack}
      >
        ← 返回客户列表
      </button>
      <section className="customer-detail-hero">
        <div className="customer-detail-identity">
          <div aria-hidden="true" className="customer-detail-avatar">
            {customer.username.slice(0, 1)}
          </div>
          <div>
            <div className="customer-detail-title">
              <h1>{companyNameOf(customer)}</h1>
              <CustomerStatusBadge status={customer.status} />
            </div>
            <p>
              用户名 {customer.username}{" "}
              <CopyCustomerId compact value={customer.user_id} />
            </p>
            <p>
              当前权益 <strong>{customer.current_benefit || "原价"}</strong>
            </p>
            <p>注册时间 {formatDateTime(customer.created_at)}</p>
          </div>
        </div>
        <div className="customer-detail-operations">
          {/* 收款开通计入收入、赠送不计收入：两个入口分开，避免把客户付过的
              钱误记成赠送（方案 P0-1）；退款扣减从会话页迁到这里（P0-2）。 */}
          {!readOnly ? (
            <>
              <button
                type="button"
                onClick={() => focusSection("customer-benefits")}
              >
                开通套餐（已收款）
              </button>
              <button
                type="button"
                onClick={() => focusSection("customer-free-grant")}
              >
                赠送积分
              </button>
              <button
                type="button"
                onClick={() => focusSection("customer-refund")}
              >
                退款扣减
              </button>
              <CustomerSuspendButton
                customer={customer}
                onChanged={onChanged}
              />
            </>
          ) : null}
        </div>
      </section>

      <section aria-label="客户核心指标" className="customer-detail-kpis">
        <article>
          <span>可用积分</span>
          <strong>{customer.available_credits ?? 0}</strong>
          <small>积分</small>
        </article>
        <article>
          <span>累计消耗</span>
          <strong>{customer.credits_spent ?? 0}</strong>
          <small>积分</small>
        </article>
        <article>
          <span>累计生成</span>
          <strong>{customer.generation_total ?? 0}</strong>
          <small>条</small>
        </article>
        <article>
          <span>累计充值</span>
          <strong>{formatFen(customer.total_recharge_fen ?? 0)}</strong>
          <small>实收金额</small>
        </article>
        <article>
          <span>本月消耗</span>
          <strong>{customer.month_consumed_credits ?? 0}</strong>
          <small>积分</small>
        </article>
        <article>
          <span>当前权益</span>
          <strong>{customer.current_benefit || "原价"}</strong>
        </article>
        <article>
          <span>近30天成功率</span>
          <strong>
            {customer.success_rate_30d == null
              ? "无任务"
              : `${customer.success_rate_30d.toFixed(1)}%`}
          </strong>
          <small>失败 {customer.generation_failed_30d ?? 0} 次</small>
        </article>
      </section>

      <TabBar
        active={tab}
        ariaLabel="客户详情页签"
        items={CUSTOMER_DETAIL_TABS.map(({ id, label }) => ({ id, label }))}
        onChange={(id) => setTab(id as CustomerDetailTab)}
      />

      {tab === "overview" ? (
        <>
          <CustomerOverview
            key={`overview:${customer.user_id}:${customer.available_credits}`}
            userId={customer.user_id}
          />
          <CustomerAnnotationSection
            key={`annotation:${customer.user_id}`}
            onChanged={onChanged}
            readOnly={readOnly}
            userId={customer.user_id}
          />
          <details className="customer-technical-details">
            <summary>技术详情</summary>
            <p>
              客户编号 <code>{customer.user_id}</code>
            </p>
            <p>激活码 {customer.activation_code}</p>
          </details>
        </>
      ) : null}

      {tab === "funds" ? (
        <>
          <AccountCreditPanel
            key={`account:${customer.user_id}:${customer.available_credits}`}
            onChanged={onChanged}
            userId={customer.user_id}
            readOnly={readOnly}
          />
          <Customer360Data
            key={`ledger:${customer.user_id}:${customer.available_credits}`}
            readOnly={readOnly}
            userId={customer.user_id}
          />
          <div className="customer-detail-settings-grid">
            <FreeCreditsSection
              key={`free-grant:${operatorId}:${customer.user_id}`}
              onGranted={onGranted}
              operatorId={operatorId}
              readOnly={readOnly}
              userId={customer.user_id}
            />
            <CustomerRefundSection
              key={`refund:${customer.user_id}`}
              availableCredits={customer.available_credits ?? 0}
              onRefunded={onGranted}
              readOnly={readOnly}
              userId={customer.user_id}
            />
          </div>
        </>
      ) : null}

      {tab === "records" ? (
        <GenerationRecordsPage
          key={`records:${customer.user_id}`}
          initialUserId={customer.user_id}
          readOnly={readOnly}
        />
      ) : null}

      {tab === "devices" ? (
        <CustomerDeviceSection
          key={`devices:${customer.user_id}`}
          readOnly={readOnly}
          userId={customer.user_id}
        />
      ) : null}

      {tab === "benefits" ? (
        <>
          <CustomerBenefitsSection
            key={`benefits:${customer.user_id}`}
            onChanged={onChanged}
            readOnly={readOnly}
            userId={customer.user_id}
          />
          {customer.activation_code !== "账号注册" && (
            <CustomerPriceEditor
              readOnly={readOnly}
              userId={customer.user_id}
            />
          )}
          <BillingQuoteCalculator
            key={`quote:${customer.user_id}`}
            userId={customer.user_id}
            customerLabel={companyNameOf(customer)}
            readOnly={readOnly}
          />
        </>
      ) : null}

      {tab === "activity" ? (
        /* 方案 P0-3：客户自己的动作（建项目、读素材等）——与管理员处置共用
           同一张审计表，这里按 scope=customer + 客户 ID 取出属于自己的部分。 */
        <CustomerActivitySection
          key={`activity:${customer.user_id}`}
          userId={customer.user_id}
        />
      ) : null}
    </div>
  );
}
