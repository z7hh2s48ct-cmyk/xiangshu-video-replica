import { describe, expect, it, vi } from "vitest";
import { attachCustomerTaskReferences } from "./customerTaskReferences";

describe("客户失败任务编号", () => {
  it.each([
    ["/api/generation-batches/batch", "VIDEO", "error_message_redacted"],
    ["/api/first-frame-tasks/frame", "FIRST_FRAME_IMAGE", "error_message"],
    [
      "/api/simple-characters/task-status/person",
      "CHARACTER_SHEET_IMAGE",
      "error_message",
    ],
    ["/api/oral/tasks/oral", "ORAL_VIDEO", "error_message"],
  ])("%s 只显示服务端唯一8位编号", async (path, family, field) => {
    const read = vi.fn().mockResolvedValue({ short_ref: "12AB34CD" });
    const result = (await attachCustomerTaskReferences(
      path,
      { id: "synthetic-task-id", status: "FAILED", [field]: "合成失败提示" },
      read,
      (_message, fallback) => fallback,
    )) as Record<string, unknown>;
    expect(read).toHaveBeenCalledWith(family, "synthetic-task-id");
    expect(result[field]).toBe(
      "本次任务未完成，请核对任务状态后重试。 错误编号：12AB34CD",
    );
    expect(result[field]).not.toContain("synthetic-task-id");
  });
  it("列表仅处理失败任务，同一响应去重，不跨会话缓存", async () => {
    const read = vi.fn().mockResolvedValue({ short_ref: "12AB34CD" });
    const payload = {
      items: [
        {
          tasks: [
            {
              id: "failed",
              status: "FAILED",
              error_message_redacted: "合成失败",
            },
            { id: "success", status: "SUCCEEDED" },
            {
              id: "failed",
              status: "FAILED",
              error_message_redacted: "合成失败",
            },
          ],
        },
      ],
    };
    const result = await attachCustomerTaskReferences(
      "/api/generation-batches",
      payload,
      read,
      String,
    );
    expect(read).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(result)).toContain("错误编号：12AB34CD");
    await attachCustomerTaskReferences(
      "/api/generation-batches",
      payload,
      read,
      String,
    );
    expect(read).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(payload)).not.toContain("错误编号");
  });
  it("无效编号或读取失败时保留完整定位号，不能编造短号", async () => {
    const result = await attachCustomerTaskReferences(
      "/api/oral/tasks/task",
      { id: "full-task-reference", status: "UNKNOWN" },
      vi.fn().mockResolvedValue({ short_ref: "not-unique" }),
      String,
    );
    expect(JSON.stringify(result)).toContain("任务编号：full-task-reference");
    expect(JSON.stringify(result)).not.toContain("错误编号");
  });
});
