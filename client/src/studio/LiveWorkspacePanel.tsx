import { useState } from "react";
// 老组件样式（F-01/P0-1 修复）：这些面板的类规则原在 styles.css（仅管理/内部壳
// 加载），客户制品必须随本挂载点自带样式。
import "../legacy-panels.css";
import type { CurrentUser, GenerationBatch, Project } from "../api";
import { CharacterLibrary } from "../CharacterLibrary";
import { CustomerProfilePanel } from "../customer/CustomerProfilePanel";
import { CustomerRechargeDialog } from "../customer/CustomerRechargeDialog";
import { CustomerWalletPanel } from "../customer/CustomerWalletPanel";
import { ProjectsPage } from "../ProjectsPage";
import { TaskRecordsPanel } from "../TaskRecordsPanel";
import { WalletPanel } from "../WalletPanel";
import type { WorkspaceShellProps } from "../workspace-shell";
import type { LivePanel } from "./types";

type CustomerAccount = WorkspaceShellProps["customerAccount"];
type CustomerWallet = WorkspaceShellProps["customerWallet"];

export function LiveWorkspacePanel({
  panel,
  currentUser,
  customerAccount,
  customerWallet,
  characterIdentityId,
  characterInitialTab = "base",
  handoffBatch = null,
  onClose,
  onHandoffConsumed,
  onProjectSelected,
  onRefresh,
}: {
  panel: LivePanel;
  currentUser: CurrentUser;
  customerAccount?: CustomerAccount;
  customerWallet?: CustomerWallet;
  characterIdentityId?: string;
  characterInitialTab?: "base" | "scenes";
  handoffBatch?: GenerationBatch | null;
  onClose: () => void;
  onHandoffConsumed: () => void;
  onProjectSelected: (project: Project) => void;
  onRefresh: () => void;
}) {
  const [isRechargeOpen, setIsRechargeOpen] = useState(false);
  // The wallet panel suggests an amount for the topping-up context it lives
  // in; a plain "充值条数" click clears it so the dialog starts empty.
  const [suggestedAmountYuan, setSuggestedAmountYuan] = useState<
    number | undefined
  >(undefined);
  // 钱包页的套餐卡片：带上套餐 id，弹窗高亮同一档位（下单时仍走套餐语义）。
  const [suggestedPackageId, setSuggestedPackageId] = useState<
    string | undefined
  >(undefined);
  const [walletRefreshKey, setWalletRefreshKey] = useState(0);
  const canWrite = currentUser.role !== "auditor";
  const customerSession = customerAccount ?? customerWallet;

  function openRecharge(amountYuan?: number, packageId?: string) {
    setSuggestedAmountYuan(amountYuan);
    setSuggestedPackageId(packageId);
    setIsRechargeOpen(true);
  }

  function finishRecharge() {
    setWalletRefreshKey((current) => current + 1);
    onRefresh();
  }

  return (
    <section className="studio-live-panel" aria-label="已有功能工作区">
      <div className="studio-live-panel__bar">
        <button type="button" onClick={onClose}>
          返回新工作台
        </button>
      </div>
      {panel === "projects" || panel === "analysis" ? (
        <ProjectsPage
          canWrite={canWrite}
          onOpenAnalysis={onProjectSelected}
          onOpenDetail={onProjectSelected}
        />
      ) : null}
      {panel === "characters" ? (
        <CharacterLibrary
          initialIdentityId={characterIdentityId}
          initialTab={characterInitialTab}
          userId={currentUser.id}
          userRole={currentUser.role}
          onChanged={onRefresh}
        />
      ) : null}
      {panel === "tasks" ? (
        <TaskRecordsPanel
          currentUserId={currentUser.id}
          handoffBatch={handoffBatch}
          onHandoffConsumed={onHandoffConsumed}
          userRole={currentUser.role}
        />
      ) : null}
      {panel === "wallet" ? (
        customerSession ? (
          <CustomerWalletPanel
            key={walletRefreshKey}
            store={customerSession.store}
            onSessionExpired={customerSession.onSessionExpired}
            onRechargeRequested={(amountYuan, packageId) =>
              openRecharge(amountYuan, packageId)
            }
          />
        ) : (
          <WalletPanel currentUserId={currentUser.id} />
        )
      ) : null}
      {panel === "profile" && customerAccount ? (
        <CustomerProfilePanel
          deviceError={customerAccount.deviceError}
          devices={customerAccount.devices}
          identity={customerAccount.identity}
          identityLoader={customerAccount.loadIdentity}
          onApprovePairing={customerAccount.onApprovePairing}
          onDismissPairing={customerAccount.onDismissPairing}
          onManualHeartbeat={customerAccount.onManualHeartbeat}
          onProfileUpdated={customerAccount.onProfileUpdated}
          onRefreshProfile={customerAccount.onRefreshProfile}
          onLogout={customerAccount.onLogout}
          onPairDevice={customerAccount.onPairDevice}
          onRecharge={(amountYuan, packageId) =>
            openRecharge(amountYuan, packageId)
          }
          onRefreshDevices={customerAccount.onRefreshDevices}
          onSessionExpired={customerAccount.onSessionExpired}
          onUnbind={customerAccount.onUnbind}
          onUpdateProfile={customerAccount.onUpdateProfile}
          profile={customerAccount.profile}
          profileLoadError={customerAccount.profileLoadError}
          sessionRuntime={customerAccount.sessionRuntime}
          store={customerAccount.store}
          walletRefreshKey={walletRefreshKey}
        />
      ) : null}
      {panel === "profile" && !customerAccount ? (
        <div className="studio-live-panel__identity">
          <h2>{currentUser.display_name}</h2>
          <p>内部工作区身份：{currentUser.role}</p>
          <WalletPanel currentUserId={currentUser.id} />
        </div>
      ) : null}
      {customerSession ? (
        <CustomerRechargeDialog
          isOpen={isRechargeOpen}
          onClose={() => {
            setIsRechargeOpen(false);
            setSuggestedAmountYuan(undefined);
            setSuggestedPackageId(undefined);
          }}
          onOrderCreated={finishRecharge}
          onPaid={finishRecharge}
          onSessionExpired={customerSession.onSessionExpired}
          store={customerSession.store}
          suggestedAmountYuan={suggestedAmountYuan}
          suggestedPackageId={suggestedPackageId}
        />
      ) : null}
    </section>
  );
}
