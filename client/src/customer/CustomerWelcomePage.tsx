import { useMemo } from "react";
import { BrandIdentity } from "../BrandIdentity";
import { StudioContext } from "../studio/context";
import { WorkbenchPage } from "../studio/MainPages";
import { navGroups } from "../studio/StudioWorkspace";
import { createState } from "../studio/state";
import type { StudioContextValue } from "../studio/types";
import { Button, Icon } from "../studio/ui";
import "../studio/studio.css";

/** Public home reuses the production workbench. It never loads account data. */
export function CustomerWelcomePage({ onLogin }: { onLogin(): void }) {
  const context = useMemo<StudioContextValue>(
    () => ({
      state: createState("workbench"),
      review: false,
      user: { id: "", username: "", display_name: "访客", role: "customer" },
      data: {
        people: [],
        assets: [],
        materials: [],
        videos: [],
        tasks: [],
        projects: [],
        errors: [],
        loading: false,
        stats: null,
        analytics7: null,
        analytics30: null,
      },
      requireLogin: onLogin,
      navigate: onLogin,
      patchDraft: onLogin,
      patchState: onLogin,
      updateData: onLogin,
      notify: onLogin,
      openPicker: onLogin,
      openLive: onLogin,
      requestGeneration: onLogin,
      saveDraft: onLogin,
      discardSavedDraft: onLogin,
      confirmFinalDraft: onLogin,
      extractScriptFromUpload: onLogin,
      extractViralCopy: onLogin,
      refresh: onLogin,
    }),
    [onLogin],
  );
  return (
    <StudioContext.Provider value={context}>
      <div className="studio-shell">
        <aside className="studio-sidebar">
          <div className="studio-brand">
            <BrandIdentity />
            <small>乡墅爆款视频创作平台</small>
          </div>
          <Button
            variant="primary"
            className="studio-new-button"
            onClick={onLogin}
          >
            <Icon name="plus" />
            新建创作
          </Button>
          <nav aria-label="主要导航">
            {navGroups.map((group, index) => (
              <div className="studio-nav-group" key={group.label ?? index}>
                {group.label && (
                  <span className="studio-nav-label">{group.label}</span>
                )}
                {group.pages.map((item) => (
                  <button
                    type="button"
                    key={item.id}
                    className={item.id === "workbench" ? "is-active" : ""}
                    aria-current={item.id === "workbench" ? "page" : undefined}
                    onClick={item.id === "workbench" ? undefined : onLogin}
                  >
                    <Icon name={item.icon} />
                    <span>{item.title}</span>
                  </button>
                ))}
              </div>
            ))}
          </nav>
          <button
            type="button"
            className="studio-account-entry"
            onClick={onLogin}
          >
            <span className="studio-user-initial">
              <Icon name="person" />
            </span>
            <span>登录 / 注册</span>
          </button>
        </aside>
        <main className="studio-main studio-route-workbench">
          <div className="studio-topbar">
            <span style={{ flex: 1, color: "#a4a4ad" }}>
              欢迎使用众墅之家 · AI 即创
            </span>
            <button
              type="button"
              className="studio-top-avatar"
              aria-label="用户档案"
              onClick={onLogin}
            >
              <span className="studio-user-initial">
                <Icon name="person" />
              </span>
            </button>
          </div>
          <div className="studio-stage">
            <WorkbenchPage />
          </div>
          <footer className="studio-version">
            登录账号后即可开始创作并查看个人资料
          </footer>
        </main>
      </div>
    </StudioContext.Provider>
  );
}
