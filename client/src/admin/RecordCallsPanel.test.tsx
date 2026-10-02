import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import {
  getAdminGenerationRecordCalls,
  getExternalCallResponse,
} from "../api.admin";
import { RecordCallsPanel } from "./RecordCallsPanel";

vi.mock("../api.admin", () => ({
  getAdminGenerationRecordCalls: vi.fn(),
  getExternalCallResponse: vi.fn(),
}));

function call(id: string) {
  return {
    call_id: id,
    created_at: "2026-10-01T12:00:00Z",
    provider: "test",
    model: "model",
    endpoint: "task/status",
    method: "GET",
    url: null,
    attempt: 1,
    http_status: 200,
    latency_ms: 20,
    outcome: "PROVIDER_ERROR",
    provider_task_id: "test-task",
    provider_request_id: null,
    provider_error_code: "busy",
    provider_message: "服务繁忙",
    error_message: null,
    request_summary: { prompt: "测试内容" },
    response_body_bytes: 30,
    has_response_body: true,
    poll_count: 1,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getAdminGenerationRecordCalls).mockResolvedValue({
    items: [call("first")],
    total: 201,
  });
  vi.mocked(getExternalCallResponse).mockResolvedValue({
    call_id: "first",
    response_headers: null,
    response_body: '{"reason":"busy"}',
    response_body_bytes: 17,
    truncated: false,
  });
});

test("权限降为审计员立即撤下已读取正文并使旧请求失效", async () => {
  const { rerender } = render(
    <RecordCallsPanel
      recordType="VIDEO"
      recordId="task"
      active
      readOnly={false}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "查看调用 first 的原始响应" }),
  );
  await screen.findByText(/"reason": "busy"/);
  rerender(
    <RecordCallsPanel recordType="VIDEO" recordId="task" active readOnly />,
  );
  expect(screen.queryByText(/"reason": "busy"/)).not.toBeInTheDocument();
  expect(screen.queryByText(/测试内容/)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: /查看调用.*原始响应/ }),
  ).not.toBeInTheDocument();
});

test("我方和第三方编号可独立复制，响应正文不随编号复制读取", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  render(
    <RecordCallsPanel
      recordType="VIDEO"
      recordId="task"
      active
      readOnly={false}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "复制任务编号 task" }),
  );
  fireEvent.click(
    screen.getByRole("button", { name: "复制第三方任务号 test-task" }),
  );
  await waitFor(() =>
    expect(writeText.mock.calls).toEqual([["task"], ["test-task"]]),
  );
  expect(getExternalCallResponse).not.toHaveBeenCalled();
});

test("翻页可访问最后一次失败，返回首页重新读取", async () => {
  vi.mocked(getAdminGenerationRecordCalls).mockImplementation(
    async (_type, _id, page) => ({
      items: [call(page?.offset === 200 ? "last-failure" : "first")],
      total: 201,
    }),
  );
  render(
    <RecordCallsPanel
      recordType="VIDEO"
      recordId="task"
      active
      readOnly={false}
    />,
  );
  await screen.findByText("共 201 次调用，当前第 1–1 次。");
  for (let page = 1; page <= 4; page += 1) {
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() =>
      expect(getAdminGenerationRecordCalls).toHaveBeenLastCalledWith(
        "VIDEO",
        "task",
        { limit: 50, offset: page * 50 },
      ),
    );
    await screen.findByText(
      `共 201 次调用，当前第 ${page * 50 + 1}–${page * 50 + 1} 次。`,
    );
  }
  expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  fireEvent.click(
    screen.getByRole("button", { name: "查看调用 last-failure 的原始响应" }),
  );
  await waitFor(() =>
    expect(getExternalCallResponse).toHaveBeenCalledWith("last-failure"),
  );
});

test("格式化展示请求和失败原因；复制和下载各自重新读取以留审计", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  const create = vi.fn().mockReturnValue("blob:test");
  const revoke = vi.fn();
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: create,
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: revoke,
  });
  const click = vi
    .spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});
  render(
    <RecordCallsPanel
      recordType="VIDEO"
      recordId="task"
      active
      readOnly={false}
    />,
  );
  expect(await screen.findByText("请求摘要")).toBeInTheDocument();
  fireEvent.click(
    screen.getByRole("button", { name: "查看调用 first 的原始响应" }),
  );
  await screen.findByText("失败原因：服务繁忙");
  fireEvent.click(await screen.findByRole("button", { name: "复制响应" }));
  await waitFor(() =>
    expect(writeText).toHaveBeenCalledWith('{\n  "reason": "busy"\n}'),
  );
  fireEvent.click(screen.getByRole("button", { name: "下载响应" }));
  await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
  expect(getExternalCallResponse).toHaveBeenCalledTimes(3);
  expect(revoke).toHaveBeenCalledWith("blob:test");
  click.mockRestore();
});

test("审计员无请求摘要和敏感正文入口", async () => {
  render(
    <RecordCallsPanel recordType="VIDEO" recordId="task" active readOnly />,
  );
  await screen.findByText("task/status");
  expect(screen.queryByText("请求摘要")).toBeNull();
  expect(screen.queryByRole("button", { name: /原始响应/ })).toBeNull();
  expect(getExternalCallResponse).not.toHaveBeenCalled();
});

test("历史正文完整性未知时明确提示", async () => {
  vi.mocked(getExternalCallResponse).mockResolvedValue({
    call_id: "first",
    response_headers: null,
    response_body: "legacy excerpt",
    response_body_bytes: 70000,
    truncated: null,
  });
  render(
    <RecordCallsPanel
      recordType="VIDEO"
      recordId="task"
      active
      readOnly={false}
    />,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "查看调用 first 的原始响应" }),
  );
  await screen.findByText(/历史记录，是否完整无法确认/);
});
