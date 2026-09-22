import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { ProjectsPage } from "./ProjectsPage";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    listProjects: vi.fn(),
    startVideoAnalysis: vi.fn(),
  };
});

const failedProject: api.Project = {
  id: "project-failed",
  owner_user_id: "employee_1",
  name: "失败后恢复",
  status: "REFERENCE_READY",
  reference_asset_id: "asset-failed",
  reference_upload_status: "READY",
  analysis_status: "FAILED",
  analysis_task_id: "analysis-task-failed",
  analysis_error_message: "视频拆解失败，请重新拆解。",
  analysis_retryable: true,
};

describe("ProjectsPage analysis recovery", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.listProjects).mockResolvedValue([failedProject]);
    vi.mocked(api.startVideoAnalysis).mockResolvedValue({
      id: "analysis-task-retry",
      project_id: failedProject.id,
      asset_id: failedProject.reference_asset_id ?? "",
      status: "PENDING",
      attempt: 0,
      result_version_id: null,
      error_code: null,
      error_message: null,
      failure_phase: null,
      request_id: null,
      retryable: false,
      created_at: "2030-01-01T00:00:00Z",
      updated_at: "2030-01-01T00:00:00Z",
      started_at: null,
      completed_at: null,
    });
  });

  it("shows a failed durable analysis and lets the user enqueue a retry", async () => {
    render(
      <ProjectsPage canWrite onOpenAnalysis={vi.fn()} onOpenDetail={vi.fn()} />,
    );

    expect(
      await screen.findByText("视频拆解失败，请重新拆解。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重新拆解" }));

    await waitFor(() =>
      expect(api.startVideoAnalysis).toHaveBeenCalledWith(
        failedProject.id,
        failedProject.reference_asset_id,
      ),
    );
    expect(
      await screen.findByText("项目“失败后恢复”已重新提交拆解。"),
    ).toBeInTheDocument();
  });

  it("does not offer a blind retry for a non-retryable failure", async () => {
    vi.mocked(api.listProjects).mockResolvedValue([
      { ...failedProject, analysis_retryable: false },
    ]);

    render(
      <ProjectsPage canWrite onOpenAnalysis={vi.fn()} onOpenDetail={vi.fn()} />,
    );

    expect(await screen.findByText("拆解失败")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重新拆解" })).toBeNull();
  });
});
