import type { WalletTransaction } from "../api";

/**
 * 客户界面里积分流转的用词，全站只用这一套。
 *
 * 为什么要收成一处：同一件事此前在用户中心叫「预扣 / 任务消费 / 积分退回」，在钱包页叫
 * 「冻结 / 成功结算 / 失败返还」，用户分不清这是不是两回事，更认不出「待结算」是什么。
 * 三个词按资金实际的走向排：任务提交先「暂扣」，结束后按用量「实扣」，多暂扣的「退回」。
 * 管理端与客服话术另有自己的词典，那边要改也应该以这里为准。
 */
export const LEDGER_TERMS = {
  hold: "暂扣",
  charge: "实扣",
  refund: "退回",
} as const;

/** 账户里正在暂扣、尚未结算的那部分额度的叫法。 */
export const HELD_CREDITS_LABEL = "暂扣中";

/** 单笔流水（原始记录）的类型名；折叠成任务后的行用 {@link LEDGER_OUTCOME_LABEL}。 */
export const LEDGER_TYPE_LABEL: Record<WalletTransaction["type"], string> = {
  CHARGE: "积分入账",
  CONVERSION: "历史积分转换",
  RESERVE: LEDGER_TERMS.hold,
  SETTLE: LEDGER_TERMS.charge,
  RELEASE: LEDGER_TERMS.refund,
  // 管理端审计调账的反向记账（20260923T1200_admin_refund_adjustment）。
  REFUND: "退款调账",
};

/** 「暂扣中」的一句话解释：悬浮提示、帮助中心、价格页都用它，不各写各的。 */
export const HOLD_EXPLANATION =
  "提交任务时先按预估用量暂扣一笔额度，结束后按实际用量实扣，多暂扣的部分自动退回；失败的任务会全额退回。";
