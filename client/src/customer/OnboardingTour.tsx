import { useEffect, useRef, useState } from "react";

/**
 * 首次进入个人中心的三步引导（审计方案 G / P2 清单 #14）。
 *
 * 只讲三件事：Token 是什么、消费记录能干什么、账号设置里有什么。**三步就停**——
 * 引导越长越容易变成每次都要点掉的障碍。
 *
 * 「看过」按账号记在 localStorage：换账号会重新引导一次（新账号确实需要）。
 * 读写都包在 try 里——隐私模式下读不到就**不显示**引导（少打扰比多打扰好）。
 */
export const ONBOARDING_STORAGE_PREFIX = "uc:onboarding-seen:";

export function shouldShowOnboarding(scope: string): boolean {
  try {
    return (
      window.localStorage.getItem(ONBOARDING_STORAGE_PREFIX + scope) !== "1"
    );
  } catch {
    return false;
  }
}

export function markOnboardingSeen(scope: string): void {
  try {
    window.localStorage.setItem(ONBOARDING_STORAGE_PREFIX + scope, "1");
  } catch {
    // 记不住就下次再说，不影响其它功能。
  }
}

export const ONBOARDING_STEPS: ReadonlyArray<{ title: string; body: string }> =
  [
    {
      title: "Token 管理",
      body: "Token 是给你的程序调用平台接口用的凭据。完整值只在创建时显示一次，请立即保存。",
    },
    {
      title: "消费记录",
      body: "每一笔暂扣、实扣与退回都在这里，可按业务、子账号和时间筛选，也能导出 CSV 对账。",
    },
    {
      title: "账号设置",
      body: "改密码、退出所有设备、撤销全部 Token、通知偏好，以及最近登录记录都在这里。",
    },
  ];

export function OnboardingTour({
  step,
  onNext,
  onSkip,
}: {
  step: number;
  onNext: () => void;
  onSkip: () => void;
}) {
  const dialog = useRef<HTMLDialogElement | null>(null);
  const current = ONBOARDING_STEPS[Math.min(step, ONBOARDING_STEPS.length - 1)];
  const isLast = step >= ONBOARDING_STEPS.length - 1;

  useEffect(() => {
    const element = dialog.current;
    if (!element || element.open) {
      return;
    }
    if (typeof element.showModal === "function") {
      element.showModal();
    } else {
      element.setAttribute("open", "");
    }
  }, []);

  return (
    <dialog
      aria-label="新手引导"
      className="uc-dialog uc-onboarding"
      onCancel={(event) => {
        // Esc 等同「跳过」，不允许把引导留在「打开但看不见」的状态。
        event.preventDefault();
        onSkip();
      }}
      ref={dialog}
    >
      <p className="uc-onboarding__progress" role="status">
        第 {step + 1} / {ONBOARDING_STEPS.length} 步
      </p>
      <h2>{current.title}</h2>
      <p>{current.body}</p>
      <div className="uc-dialog-actions">
        <button className="uc-primary" type="button" onClick={onNext}>
          {isLast ? "开始使用" : "下一步"}
        </button>
        <button type="button" onClick={onSkip}>
          跳过
        </button>
      </div>
    </dialog>
  );
}

/** 引导要显示时的最小状态机：显示 → 下一步 → 记「看过」并关闭。 */
export function useOnboarding(scope: string) {
  const [step, setStep] = useState<number | null>(() =>
    shouldShowOnboarding(scope) ? 0 : null,
  );
  function finish() {
    markOnboardingSeen(scope);
    setStep(null);
  }
  function next() {
    if (step === null) {
      return;
    }
    if (step >= ONBOARDING_STEPS.length - 1) {
      finish();
      return;
    }
    setStep(step + 1);
  }
  return {
    tour:
      step === null ? null : (
        <OnboardingTour onNext={next} onSkip={finish} step={step} />
      ),
  };
}
