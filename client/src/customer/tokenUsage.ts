/**
 * Token 的「消费占比」（审计机会点 2 / 方案 B：主力占比可视化）。
 *
 * 口径：该 Token 的累计消费 ÷ 全部 Token 的累计消费之和。分母只算**未撤销**的
 * Token——把已撤销的历史消费也算进去，会让在用的 Token 看起来"占比很小"，
 * 与「谁是主力」这个问题的答案相反。全部为 0 时占比记 0（不显示 NaN）。
 */
export type TokenUsage = {
  id: string;
  consumed: number;
  /** 0..100 的整数百分比；分母为 0 时是 0。 */
  share: number;
};

export function tokenUsageShares(
  tokens: ReadonlyArray<{
    id: string;
    revoked_at: string | null;
    total_consumed_credits: number;
  }>,
): Map<string, TokenUsage> {
  const live = tokens.filter((token) => !token.revoked_at);
  const total = live.reduce(
    (sum, token) => sum + Math.max(0, token.total_consumed_credits),
    0,
  );
  const shares = new Map<string, TokenUsage>();
  for (const token of tokens) {
    const consumed = Math.max(0, token.total_consumed_credits);
    shares.set(token.id, {
      id: token.id,
      consumed,
      share:
        total <= 0 || token.revoked_at
          ? 0
          : Math.round((consumed / total) * 100),
    });
  }
  return shares;
}

/**
 * Token 闲置天数（审计机会点 2 的子项「闲置自动禁用」——本批只做**提示**）。
 *
 * **刻意不禁用**：自动失效客户已发出的密钥是一次静默的破坏性动作，必须先有「闲置多久」+
 * 「失效前怎么通知」的产品口径。这里只算出「多久没用了」，让客户自己决定撤销还是留着。
 *
 * 参照时刻取「最近使用」，从未使用过则取创建时间——刚建好的 Token 不算闲置。
 */
export const TOKEN_IDLE_HINT_DAYS = 30;

export function tokenIdleDays(
  token: { last_used_at: string | null; created_at: string },
  now: number = Date.now(),
): number | null {
  const reference = token.last_used_at ?? token.created_at;
  const parsed = Date.parse(reference);
  if (!Number.isFinite(parsed)) {
    return null;
  }
  const days = Math.floor((now - parsed) / 86_400_000);
  return days >= TOKEN_IDLE_HINT_DAYS ? days : null;
}
