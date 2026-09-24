/**
 * 服务端在受理付费请求时以 402 + `INSUFFICIENT_CREDITS` 拒绝扣分不足的账户
 * （`usage_billing.accept_operation`，覆盖全部计费科目）。文案由服务端给出并
 * 原样透传，此处只负责判定，让各界面自行决定如何承接：复刻流内联「去充值」
 * 按钮，工作台打开钱包侧栏。
 */
export function isInsufficientCredits(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  return (error as { code?: unknown }).code === "INSUFFICIENT_CREDITS";
}
