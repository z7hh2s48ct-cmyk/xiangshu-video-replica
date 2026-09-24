import { expect, test } from "vitest";
import { tokenIdleDays, tokenUsageShares } from "./tokenUsage";

const token = (
  id: string,
  consumed: number,
  revoked_at: string | null = null,
) => ({ id, revoked_at, total_consumed_credits: consumed });

test("按在用 Token 的累计消费算占比", () => {
  const shares = tokenUsageShares([
    token("a", 75),
    token("b", 25),
    token("c", 100),
  ]);
  expect(shares.get("a")?.share).toBe(38); // 75/200
  expect(shares.get("b")?.share).toBe(13); // 25/200
  expect(shares.get("c")?.share).toBe(50);
});

test("已撤销的 Token 不计入分母，自身占比记 0", () => {
  const shares = tokenUsageShares([
    token("live", 100),
    token("dead", 900, "2026-09-01"),
  ]);
  expect(shares.get("live")?.share).toBe(100);
  expect(shares.get("dead")?.share).toBe(0);
});

test("全都没消费过时占比是 0，不产生 NaN", () => {
  const shares = tokenUsageShares([token("a", 0), token("b", 0)]);
  expect(shares.get("a")?.share).toBe(0);
  expect(Number.isNaN(shares.get("a")?.share)).toBe(false);
});

test("负数（历史脏数据）按 0 计，不互相抵消", () => {
  const shares = tokenUsageShares([token("a", -50), token("b", 100)]);
  expect(shares.get("a")?.consumed).toBe(0);
  expect(shares.get("b")?.share).toBe(100);
});

test("闲置天数：超过阈值才返回，从未使用过以创建时间为准", () => {
  const now = Date.parse("2026-09-22T00:00:00Z");
  expect(
    tokenIdleDays(
      {
        last_used_at: "2026-09-20T00:00:00Z",
        created_at: "2026-01-01T00:00:00Z",
      },
      now,
    ),
  ).toBeNull();
  expect(
    tokenIdleDays(
      {
        last_used_at: "2026-08-01T00:00:00Z",
        created_at: "2026-01-01T00:00:00Z",
      },
      now,
    ),
  ).toBe(52);
  // 从未使用过：看创建时间（新 Token 不提示）
  expect(
    tokenIdleDays(
      { last_used_at: null, created_at: "2026-09-21T00:00:00Z" },
      now,
    ),
  ).toBeNull();
  expect(
    tokenIdleDays(
      { last_used_at: null, created_at: "2026-07-01T00:00:00Z" },
      now,
    ),
  ).toBe(83);
  // 脏数据不抛出
  expect(
    tokenIdleDays({ last_used_at: "not-a-date", created_at: "junk" }, now),
  ).toBeNull();
});
