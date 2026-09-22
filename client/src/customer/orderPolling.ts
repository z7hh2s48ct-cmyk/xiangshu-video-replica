/**
 * 充值订单状态轮询的节奏（审计 P2 清单 #20）。
 *
 * 原来的实现是「固定 2 秒 × 30 次 = 约 1 分钟」，两处（钱包面板、充值弹窗）各写一遍。
 * 现在是：2s 起、每次翻倍、封顶 32s，总窗口 5 分钟——用户去扫码支付的那几分钟里
 * 请求数从 30 次降到 11 次左右，而付款后仍能在几秒内被发现（前几次仍是 2s/4s）。
 *
 * 抽成独立模块的原因：同一个订单会被钱包面板与充值弹窗**同时**轮询，两处各写一套
 * 节奏迟早会走偏；这里同时给两处用，也便于单测。
 */
export const ORDER_POLL_BASE_INTERVAL_MS = 2_000;
export const ORDER_POLL_MAX_INTERVAL_MS = 32_000;
export const ORDER_POLL_WINDOW_MS = 5 * 60_000;

/** 第 `attempt` 次轮询（从 0 数）距离上一次该等多久。 */
export function orderPollDelay(attempt: number): number {
  const safeAttempt = Number.isFinite(attempt) && attempt > 0 ? attempt : 0;
  return Math.min(
    ORDER_POLL_BASE_INTERVAL_MS * 2 ** safeAttempt,
    ORDER_POLL_MAX_INTERVAL_MS,
  );
}

/** 从轮询开始到现在是否仍在窗口内——超出窗口就停，交给用户手动刷新。 */
export function orderPollWithinWindow(
  startedAt: number,
  now: number = Date.now(),
): boolean {
  return now - startedAt < ORDER_POLL_WINDOW_MS;
}
