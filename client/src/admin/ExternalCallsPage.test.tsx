import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import {
  type GlobalExternalCallPage,
  getExternalCallResponse,
  listGlobalExternalCalls,
} from "../api.admin";
import { ExternalCallsPage } from "./ExternalCallsPage";

vi.mock("../api.admin", () => ({
  listGlobalExternalCalls: vi.fn(),
  getExternalCallResponse: vi.fn(),
}));
const page: GlobalExternalCallPage = {
  total: 51,
  limit: 50,
  offset: 0,
  providers: ["fake-provider"],
  endpoints: ["fake/status"],
  metrics_since: "2026-10-01T00:00:00Z",
  metrics: [
    {
      provider: "fake-provider",
      total: 3,
      failed: 1,
      failure_rate_pct: 100 / 3,
      avg_latency_ms: 200,
      latency_samples: 3,
    },
  ],
  items: [
    {
      task_type: "VIDEO",
      task_id: "task-one",
      request_id: "request-one",
      call: {
        call_id: "call-one",
        created_at: "2026-10-01T10:00:00Z",
        provider: "fake-provider",
        model: null,
        endpoint: "fake/status",
        method: "GET",
        url: null,
        attempt: 1,
        http_status: 200,
        latency_ms: 300,
        outcome: "PROVIDER_ERROR",
        provider_task_id: "third-task",
        provider_request_id: "third-request",
        provider_error_code: null,
        provider_message: "原始失败",
        error_message: null,
        request_summary: { prompt: "客户原文" },
        response_body_bytes: 20,
        has_response_body: true,
        poll_count: 1,
      },
    },
  ],
};
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listGlobalExternalCalls).mockResolvedValue(page);
  vi.mocked(getExternalCallResponse).mockResolvedValue({
    call_id: "call-one",
    response_body: '{"message":"原始失败"}',
    response_headers: null,
    response_body_bytes: 20,
    truncated: false,
  });
});
test("查询按提交筛选，服务指标和分页来自后端，查看及复制重新读取", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: { writeText },
  });
  render(<ExternalCallsPage />);
  await screen.findByText("共 51 条调用记录");
  expect(screen.getByText(/失败率 33.3%/)).toHaveTextContent(
    "平均耗时 200 毫秒",
  );
  expect(getExternalCallResponse).not.toHaveBeenCalled();
  fireEvent.change(
    screen.getByLabelText(/任务编号、8 位错误编号或第三方任务号/),
    {
      target: { value: "third-request" },
    },
  );
  expect(listGlobalExternalCalls).toHaveBeenCalledTimes(1);
  fireEvent.change(screen.getByLabelText("服务"), {
    target: { value: "fake-provider" },
  });
  fireEvent.change(screen.getByLabelText("接口"), {
    target: { value: "fake/status" },
  });
  fireEvent.change(screen.getByLabelText("结果"), {
    target: { value: "PROVIDER_ERROR" },
  });
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await waitFor(() =>
    expect(listGlobalExternalCalls).toHaveBeenLastCalledWith(
      expect.objectContaining({
        task_ref: "third-request",
        provider: "fake-provider",
        endpoint: "fake/status",
        outcome: "PROVIDER_ERROR",
        offset: 0,
      }),
    ),
  );
  await screen.findByText("共 51 条调用记录");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() =>
    expect(listGlobalExternalCalls).toHaveBeenLastCalledWith(
      expect.objectContaining({ task_ref: "third-request", offset: 50 }),
    ),
  );
  fireEvent.click(await screen.findByText("技术详情"));
  fireEvent.click(screen.getByRole("button", { name: "查看原始响应" }));
  await screen.findByRole("button", { name: "复制响应" });
  fireEvent.click(screen.getByRole("button", { name: "复制响应" }));
  await waitFor(() => expect(writeText).toHaveBeenCalled());
  expect(getExternalCallResponse).toHaveBeenCalledTimes(2);
});
test("查询失败明确显示，可重新查询恢复", async () => {
  vi.mocked(listGlobalExternalCalls).mockRejectedValueOnce(
    new Error("拒绝访问"),
  );
  render(<ExternalCallsPage />);
  await screen.findByText("拒绝访问");
  fireEvent.click(screen.getByRole("button", { name: "查询" }));
  await screen.findByText("共 51 条调用记录");
});
