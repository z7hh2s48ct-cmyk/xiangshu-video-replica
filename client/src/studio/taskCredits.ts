import type { StudioTask } from "./types";

export type TaskCreditsNote = {
  /** refunded：已经退回，绿色确认；pending：还在处理或需要去核对，中性提示。 */
  tone: "refunded" | "pending";
  text: string;
};

type CreditsView = Pick<StudioTask, "status" | "credits">;

/**
 * 失败任务卡片上的积分去向说明。
 *
 * 为什么要在任务页上说：提交时暂扣的额度已经让可用积分变少了，任务失败后用户第一眼
 * 看的是任务页而不是消费记录；此前这里只写「不会扣费」，与用户刚看到的余额矛盾，
 * 又没有金额，用户只能自己去流水里对账。
 *
 * 措辞刻意不写「全额」：任务状态是批次级的，批次里可能还有别的任务在跑，
 * 此刻退回的只是已经失败的那部分。
 */
export function failedTaskCreditsNote(task: CreditsView): TaskCreditsNote {
  // 状态待确认时结果还没定，先不承诺具体金额。
  if (task.status === "uncertain") {
    return {
      tone: "pending",
      text: "任务状态待确认；确认未成功后，暂扣的积分会自动退回，可在任务中心查看进展。",
    };
  }
  const refunded = task.credits?.refunded ?? 0;
  const charged = task.credits?.charged ?? 0;
  if (refunded > 0 && charged > 0) {
    return {
      tone: "refunded",
      text: `已自动退回 ${refunded} 积分；已成功的部分按实际用量实扣 ${charged} 积分。`,
    };
  }
  if (refunded > 0) {
    return {
      tone: "refunded",
      text: `已自动退回 ${refunded} 积分，可用积分已恢复。`,
    };
  }
  if (charged > 0) {
    return {
      tone: "pending",
      text: `已按实际用量实扣 ${charged} 积分，明细可在消费记录里核对。`,
    };
  }
  // 两个数都是 0 只表示「还没有落账」（或旧服务端没返回），不能说成「没扣过」。
  return {
    tone: "pending",
    text: "暂扣的积分会在失败处理完成后自动退回，稍后可在消费记录里核对。",
  };
}

/** 已取消任务的说明；没有可说的金额时返回 null，由调用方沿用通用文案。 */
export function cancelledTaskCreditsNote(
  task: CreditsView,
): TaskCreditsNote | null {
  const refunded = task.credits?.refunded ?? 0;
  if (refunded <= 0) return null;
  return {
    tone: "refunded",
    text: `暂扣的 ${refunded} 积分已退回，明细可在消费记录里查看。`,
  };
}
