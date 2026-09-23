import { describe, expect, it, vi } from "vitest";
import { createCloudDraftQueue } from "./cloudDraftQueue";

describe("cloud draft latest-intent queue", () => {
  it("历史成功A在B进行中再次到达时仍排队成为最终写入", async () => {
    let resolveB: (() => void) | undefined;
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockResolvedValueOnce(undefined)
      .mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveB = resolve;
        }),
      )
      .mockResolvedValueOnce(undefined);
    const queue = createCloudDraftQueue(write);

    await queue.persist("A", "A", () => true);
    const pendingB = queue.persist("B", "B", () => true);
    const finalA = queue.persist("A", "A", () => true);
    expect(write.mock.calls.map(([value]) => value)).toEqual(["A", "B"]);

    resolveB?.();
    await pendingB;
    await finalA;
    expect(write.mock.calls.map(([value]) => value)).toEqual(["A", "B", "A"]);
  });

  it("前序失败后继续发送仍有效的最新意图", async () => {
    let rejectA: ((cause: Error) => void) | undefined;
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockReturnValueOnce(
        new Promise<void>((_resolve, reject) => {
          rejectA = reject;
        }),
      )
      .mockResolvedValueOnce(undefined);
    const queue = createCloudDraftQueue(write);

    const failedA = queue.persist("A", "A", () => true);
    const currentB = queue.persist("B", "B", () => true);
    rejectA?.(new Error("A failed"));

    await expect(failedA).rejects.toThrow("A failed");
    await currentB;
    expect(write.mock.calls.map(([value]) => value)).toEqual(["A", "B"]);
  });

  it("同key待写意图的旧谓词失效时改用最新谓词实际发送", async () => {
    let resolveX: (() => void) | undefined;
    let oldCurrent = true;
    const write = vi
      .fn<(value: string) => Promise<void>>()
      .mockReturnValueOnce(
        new Promise<void>((resolve) => {
          resolveX = resolve;
        }),
      )
      .mockResolvedValueOnce(undefined);
    const queue = createCloudDraftQueue(write);

    const activeX = queue.persist("X", "X", () => true);
    const oldA = queue.persist("A", "old A", () => oldCurrent);
    oldCurrent = false;
    const latestA = queue.persist("A", "latest A", () => true);
    resolveX?.();

    await activeX;
    await oldA;
    await latestA;
    expect(write.mock.calls.map(([value]) => value)).toEqual(["X", "latest A"]);
  });
});
