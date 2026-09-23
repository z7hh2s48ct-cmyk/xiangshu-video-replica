/** 创作草稿本地残留清理（LEFTOVER-ON-OPEN）。
 *
 * 清理边界是这组用例的重点：幂等键与待支付订单必须活下来。清掉幂等键，
 * 「放弃草稿」就成了重复建批（重复扣费）的入口；清掉待支付订单号，用户
 * 就再也找不回那笔充值。两者都与创作内容无关，不在清理范围内。
 */
import { beforeEach, describe, expect, it } from "vitest";
import { clearAccountResidue, clearCreationDraftResidue } from "./draftResidue";
import { readPageCache, writePageCache } from "./pageCache";

const ACCOUNT = "user-1";
const OTHER = "user-2";

function seed() {
  window.localStorage.clear();
  window.localStorage.setItem(
    `generation.localDraft/script/${ACCOUNT}/proj-a`,
    JSON.stringify({ text: "上次的口播稿" }),
  );
  window.localStorage.setItem(
    `generation.localDraft/prompt/${ACCOUNT}/proj-a`,
    JSON.stringify({ text: "上次的提示词" }),
  );
  window.localStorage.setItem(
    `generation.localDraft/script/${OTHER}/proj-b`,
    JSON.stringify({ text: "别人的稿子" }),
  );
  window.localStorage.setItem(
    `generation.idempotency/${ACCOUNT}/proj-a`,
    JSON.stringify({ fingerprint: "f", key: "k", request: {} }),
  );
  window.localStorage.setItem(`wallet.pendingOrderNo:${ACCOUNT}`, "order-9");
  window.localStorage.setItem(`generation.batchId:${ACCOUNT}`, "batch-9");
}

describe("clearCreationDraftResidue", () => {
  beforeEach(seed);

  it("清掉本账号未保存的口播稿与提示词草稿", () => {
    clearCreationDraftResidue(ACCOUNT);
    expect(
      window.localStorage.getItem(
        `generation.localDraft/script/${ACCOUNT}/proj-a`,
      ),
    ).toBeNull();
    expect(
      window.localStorage.getItem(
        `generation.localDraft/prompt/${ACCOUNT}/proj-a`,
      ),
    ).toBeNull();
  });

  it("不碰其他账号的草稿", () => {
    clearCreationDraftResidue(ACCOUNT);
    expect(
      window.localStorage.getItem(
        `generation.localDraft/script/${OTHER}/proj-b`,
      ),
    ).not.toBeNull();
  });

  it("保留幂等键与待支付订单号", () => {
    clearCreationDraftResidue(ACCOUNT);
    expect(
      window.localStorage.getItem(`generation.idempotency/${ACCOUNT}/proj-a`),
    ).not.toBeNull();
    expect(
      window.localStorage.getItem(`wallet.pendingOrderNo:${ACCOUNT}`),
    ).toBe("order-9");
  });

  it("留下上次查看的批次 id：它只是任务中心的定位，放弃草稿不该影响", () => {
    clearCreationDraftResidue(ACCOUNT);
    expect(window.localStorage.getItem(`generation.batchId:${ACCOUNT}`)).toBe(
      "batch-9",
    );
  });

  it("账号 id 含 / 或 % 时按编码后的前缀匹配，不误伤相邻账号", () => {
    window.localStorage.setItem(
      `generation.localDraft/script/${encodeURIComponent("a/b")}/proj`,
      JSON.stringify({ text: "x" }),
    );
    window.localStorage.setItem(
      `generation.localDraft/script/${encodeURIComponent("a/bc")}/proj`,
      JSON.stringify({ text: "y" }),
    );
    clearCreationDraftResidue("a/b");
    expect(
      window.localStorage.getItem(
        `generation.localDraft/script/${encodeURIComponent("a/b")}/proj`,
      ),
    ).toBeNull();
    expect(
      window.localStorage.getItem(
        `generation.localDraft/script/${encodeURIComponent("a/bc")}/proj`,
      ),
    ).not.toBeNull();
  });

  it("存储不可用时静默降级，不抛给调用方", () => {
    const original = window.localStorage.removeItem;
    Object.defineProperty(window.localStorage, "removeItem", {
      configurable: true,
      value: () => {
        throw new Error("storage blocked");
      },
    });
    try {
      expect(() => clearCreationDraftResidue(ACCOUNT)).not.toThrow();
    } finally {
      Object.defineProperty(window.localStorage, "removeItem", {
        configurable: true,
        value: original,
      });
    }
  });
});

describe("clearAccountResidue", () => {
  beforeEach(seed);

  it("登出时连同上次查看的批次 id 与页间缓存一起清", () => {
    writePageCache("materials:user-1", { rows: 3 });
    clearAccountResidue(ACCOUNT);
    expect(window.localStorage.getItem(`generation.batchId:${ACCOUNT}`)).toBe(
      null,
    );
    expect(readPageCache("materials:user-1")).toBeUndefined();
  });

  it("登出也不清幂等键与待支付订单号", () => {
    clearAccountResidue(ACCOUNT);
    expect(
      window.localStorage.getItem(`generation.idempotency/${ACCOUNT}/proj-a`),
    ).not.toBeNull();
    expect(
      window.localStorage.getItem(`wallet.pendingOrderNo:${ACCOUNT}`),
    ).toBe("order-9");
  });
});
