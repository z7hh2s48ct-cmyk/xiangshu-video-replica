import { describe, expect, it } from "vitest";
import { cancelledTaskCreditsNote, failedTaskCreditsNote } from "./taskCredits";

describe("failedTaskCreditsNote", () => {
  it("已退回时说出金额并标为已确认", () => {
    expect(
      failedTaskCreditsNote({
        status: "failed",
        credits: { charged: 0, refunded: 8 },
      }),
    ).toEqual({
      tone: "refunded",
      text: "已自动退回 8 积分，可用积分已恢复。",
    });
  });

  it("部分成功的批次同时交代实扣与退回，不说成全额退回", () => {
    const note = failedTaskCreditsNote({
      status: "failed",
      credits: { charged: 14, refunded: 10 },
    });
    expect(note.tone).toBe("refunded");
    expect(note.text).toContain("已自动退回 10 积分");
    expect(note.text).toContain("实扣 14 积分");
    expect(note.text).not.toContain("全额");
  });

  it("只有实扣没有退回时不声称退回", () => {
    const note = failedTaskCreditsNote({
      status: "failed",
      credits: { charged: 6, refunded: 0 },
    });
    expect(note.tone).toBe("pending");
    expect(note.text).toContain("实扣 6 积分");
    expect(note.text).not.toContain("退回 ");
  });

  it("两个数都是 0 只表示还没落账：承诺自动退回，但不编造金额", () => {
    for (const credits of [{ charged: 0, refunded: 0 }, undefined]) {
      const note = failedTaskCreditsNote({ status: "failed", credits });
      expect(note.tone).toBe("pending");
      expect(note.text).toContain("失败处理完成后自动退回");
      expect(note.text).not.toMatch(/\d/);
    }
  });

  it("状态待确认时不承诺具体金额，即使已有一部分退回", () => {
    const note = failedTaskCreditsNote({
      status: "uncertain",
      credits: { charged: 0, refunded: 8 },
    });
    expect(note.tone).toBe("pending");
    expect(note.text).toContain("状态待确认");
    expect(note.text).not.toMatch(/\d/);
  });
});

describe("cancelledTaskCreditsNote", () => {
  it("有退回时给出金额，没有则交还通用文案", () => {
    expect(
      cancelledTaskCreditsNote({
        status: "cancelled",
        credits: { charged: 0, refunded: 16 },
      }),
    ).toEqual({
      tone: "refunded",
      text: "暂扣的 16 积分已退回，明细可在消费记录里查看。",
    });
    expect(
      cancelledTaskCreditsNote({ status: "cancelled", credits: undefined }),
    ).toBeNull();
    expect(
      cancelledTaskCreditsNote({
        status: "cancelled",
        credits: { charged: 0, refunded: 0 },
      }),
    ).toBeNull();
  });
});
