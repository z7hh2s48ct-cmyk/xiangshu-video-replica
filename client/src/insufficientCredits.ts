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

/**
 * 计费动作统一承接：余额不足时透传服务端文案并直接打开钱包侧栏，
 * 不让用户自己找充值入口（对齐口播/视频生成的既有行为）。
 * 返回是否已按余额不足处理，调用方据此决定是否继续各自的通用失败提示。
 */
export function openWalletIfInsufficientCredits(
  error: unknown,
  actions: { notify: (message: string) => void; openWallet: () => void },
): boolean {
  if (!isInsufficientCredits(error)) return false;
  const message =
    error instanceof Error && error.message
      ? error.message
      : "余额不足，请先充值后再试。";
  actions.notify(message);
  actions.openWallet();
  return true;
}
