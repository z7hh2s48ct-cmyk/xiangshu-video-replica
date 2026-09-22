import { expect, test } from "vitest";
import {
  ORDER_POLL_BASE_INTERVAL_MS,
  ORDER_POLL_MAX_INTERVAL_MS,
  ORDER_POLL_WINDOW_MS,
  orderPollDelay,
  orderPollWithinWindow,
} from "./orderPolling";

test("轮询节奏 2s 起、逐次翻倍、封顶 32s", () => {
  expect(orderPollDelay(0)).toBe(2_000);
  expect(orderPollDelay(1)).toBe(4_000);
  expect(orderPollDelay(2)).toBe(8_000);
  expect(orderPollDelay(3)).toBe(16_000);
  expect(orderPollDelay(4)).toBe(32_000);
  // 封顶后不再增长（否则一次等待会比整个窗口还长）
  expect(orderPollDelay(5)).toBe(ORDER_POLL_MAX_INTERVAL_MS);
  expect(orderPollDelay(20)).toBe(ORDER_POLL_MAX_INTERVAL_MS);
});

test("非法/负数次数退回首档，不产出 NaN 或负延迟", () => {
  expect(orderPollDelay(-3)).toBe(ORDER_POLL_BASE_INTERVAL_MS);
  expect(orderPollDelay(Number.NaN)).toBe(ORDER_POLL_BASE_INTERVAL_MS);
});

test("5 分钟窗口：到点就停，交回用户手动刷新", () => {
  const startedAt = 1_000_000;
  expect(orderPollWithinWindow(startedAt, startedAt)).toBe(true);
  expect(
    orderPollWithinWindow(startedAt, startedAt + ORDER_POLL_WINDOW_MS - 1),
  ).toBe(true);
  expect(
    orderPollWithinWindow(startedAt, startedAt + ORDER_POLL_WINDOW_MS),
  ).toBe(false);
});

test("退避后的总请求数明显少于原来的固定 2s×30 次", () => {
  // 按本节奏走满 5 分钟窗口的请求次数（含首帧）
  let elapsed = 0;
  let attempts = 0;
  while (elapsed < ORDER_POLL_WINDOW_MS) {
    elapsed += orderPollDelay(attempts);
    attempts += 1;
  }
  expect(attempts).toBeLessThan(30);
  expect(attempts).toBeGreaterThanOrEqual(10);
});
