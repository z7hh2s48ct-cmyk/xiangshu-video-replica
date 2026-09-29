import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterVersionDialog } from "./CharacterVersionDialog";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    listCharacterVersions: vi.fn(),
    createCharacterVersion: vi.fn(),
    listCharacterGenerationTasks: vi.fn(),
    listCharacterAssets: vi.fn(),
    generateCharacterAssets: vi.fn(),
    regenerateCharacterAsset: vi.fn(),
    reviewCharacterAsset: vi.fn(),
    publishCharacterVersion: vi.fn(),
    getCachedCharacterAssetUrl: vi.fn(),
  };
});

const REQUIRED_VIEWS: api.RequiredCharacterViewType[] = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "LEFT_SIDE",
];

function makeVersion(
  overrides: Partial<api.CharacterVersion> = {},
): api.CharacterVersion {
  return {
    id: "version-1",
    persona_id: "persona-1",
    version_number: 1,
    status: "REVIEWING",
    source_asset_id: "source-1",
    source_sha256: "a".repeat(64),
    persona_snapshot_json: {},
    provider: "apilio",
    model: "gpt-image-2",
    generation_params_json: {},
    template_version: "character-prompt-v1",
    template_hash: "hash",
    required_view_types_json: [...REQUIRED_VIEWS],
    published_by: null,
    published_at: null,
    publication_snapshot_json: null,
    publication_hash: null,
    created_by: "admin_1",
    created_at: "2026-09-01T00:00:00Z",
    ...overrides,
  };
}

function makeTask(
  view: api.RequiredCharacterViewType,
  status: api.CharacterGenerationTask["status"],
  overrides: Partial<api.CharacterGenerationTask> = {},
): api.CharacterGenerationTask {
  return {
    id: `task-${view}`,
    character_version_id: "version-1",
    view_type: view,
    provider: "apilio",
    model: "gpt-image-2",
    idempotency_key: `key-${view}`,
    request_hash: "hash",
    candidate_number: 1,
    request_snapshot_json: {},
    status,
    provider_task_id: null,
    attempt: 1,
    max_attempts: 3,
    error_code: null,
    error_message_redacted: null,
    cost_amount: null,
    next_poll_at: null,
    created_by: "admin_1",
    created_at: "2026-09-01T00:00:00Z",
    updated_at: "2026-09-01T00:00:10Z",
    started_at: null,
    completed_at: null,
    ...overrides,
  };
}

function makeAsset(
  view: api.RequiredCharacterViewType,
  overrides: Partial<api.CharacterAsset> = {},
): api.CharacterAsset {
  return {
    id: `asset-${view}`,
    character_version_id: "version-1",
    asset_id: `core-${view}`,
    view_type: view,
    candidate_number: 1,
    generation_task_id: `task-${view}`,
    auto_quality_json: {},
    review_status: "NOT_REVIEWED",
    is_published_selection: false,
    created_at: "2026-09-01T00:00:20Z",
    ...overrides,
  };
}

function renderDialog(onPublished?: () => void) {
  return render(
    <CharacterVersionDialog
      displayName="林夏"
      onClose={vi.fn()}
      onPublished={onPublished}
      personaId="persona-1"
    />,
  );
}

describe("CharacterVersionDialog", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.getCachedCharacterAssetUrl).mockImplementation(
      async (assetId) => ({
        url: `http://127.0.0.1:8000/mock/${assetId}`,
      }),
    );
    vi.mocked(api.listCharacterVersions).mockResolvedValue([]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([]);
    vi.mocked(api.listCharacterAssets).mockResolvedValue([]);
  });

  it("尚无版本时创建版本并自动提交五视图生成", async () => {
    renderDialog();
    const createButton = await screen.findByRole("button", {
      name: "创建版本并生成五视图",
    });

    vi.mocked(api.createCharacterVersion).mockResolvedValue(
      makeVersion({ status: "DRAFT" }),
    );
    vi.mocked(api.generateCharacterAssets).mockResolvedValue(
      REQUIRED_VIEWS.map((view) => makeTask(view, "PENDING")),
    );
    vi.mocked(api.listCharacterVersions).mockResolvedValue([
      makeVersion({ status: "GENERATING" }),
    ]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue(
      REQUIRED_VIEWS.map((view) => makeTask(view, "PENDING")),
    );

    fireEvent.click(createButton);

    await waitFor(() =>
      expect(api.createCharacterVersion).toHaveBeenCalledWith("persona-1", {
        provider: "apilio",
        model: "gpt-image-2",
        generation_params_json: {},
      }),
    );
    await waitFor(() =>
      expect(api.generateCharacterAssets).toHaveBeenCalledWith(
        "version-1",
        expect.objectContaining({
          idempotency_key: expect.any(String),
          candidates_per_view: 1,
        }),
      ),
    );
    expect(
      await screen.findByRole("status", { name: "角色五视图生成进度" }),
    ).toBeInTheDocument();
  });

  it("有活跃任务时轮询，任务完成后自动展示候选图", async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(api.listCharacterVersions).mockResolvedValue([
        makeVersion({ status: "GENERATING" }),
      ]);
      vi.mocked(api.listCharacterGenerationTasks)
        .mockResolvedValueOnce(
          REQUIRED_VIEWS.map((view) => makeTask(view, "RUNNING")),
        )
        .mockResolvedValue(
          REQUIRED_VIEWS.map((view) => makeTask(view, "SUCCEEDED")),
        );
      vi.mocked(api.listCharacterAssets)
        .mockResolvedValueOnce([])
        .mockResolvedValue(REQUIRED_VIEWS.map((view) => makeAsset(view)));

      renderDialog();
      // 先刷新挂载读取的微任务链，再断言已进入轮询状态。
      await act(async () => {});
      expect(
        screen.getByRole("status", { name: "角色五视图生成进度" }),
      ).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
      });

      expect(api.listCharacterGenerationTasks).toHaveBeenCalledTimes(2);
      expect(
        screen.queryByRole("status", { name: "角色五视图生成进度" }),
      ).toBeNull();
      expect(screen.getAllByText("候选 1")).toHaveLength(5);
    } finally {
      vi.useRealTimers();
    }
  });

  it("批准候选后刷新展示已批准状态", async () => {
    vi.mocked(api.listCharacterVersions).mockResolvedValue([makeVersion()]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([
      makeTask("FRONT_FACE", "SUCCEEDED"),
    ]);
    const asset = makeAsset("FRONT_FACE");
    vi.mocked(api.listCharacterAssets)
      .mockResolvedValueOnce([asset])
      .mockResolvedValue([{ ...asset, review_status: "APPROVED" }]);
    vi.mocked(api.reviewCharacterAsset).mockResolvedValue({
      id: "review-1",
      character_asset_id: asset.id,
      reviewer_user_id: "admin_1",
      decision: "APPROVED",
      issue_codes_json: [],
      comment: null,
      created_at: "2026-09-01T00:05:00Z",
    });

    renderDialog();
    fireEvent.click(await screen.findByRole("button", { name: "批准" }));

    await waitFor(() =>
      expect(api.reviewCharacterAsset).toHaveBeenCalledWith(
        "asset-FRONT_FACE",
        "APPROVED",
        "",
      ),
    );
    expect(await screen.findByText("已批准")).toBeInTheDocument();
  });

  it("驳回候选携带驳回裁决", async () => {
    vi.mocked(api.listCharacterVersions).mockResolvedValue([makeVersion()]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([
      makeTask("FRONT_FACE", "SUCCEEDED"),
    ]);
    const asset = makeAsset("FRONT_FACE");
    vi.mocked(api.listCharacterAssets)
      .mockResolvedValueOnce([asset])
      .mockResolvedValue([{ ...asset, review_status: "REJECTED" }]);
    vi.mocked(api.reviewCharacterAsset).mockResolvedValue({
      id: "review-2",
      character_asset_id: asset.id,
      reviewer_user_id: "admin_1",
      decision: "REJECTED",
      issue_codes_json: ["MANUAL_REJECT"],
      comment: null,
      created_at: "2026-09-01T00:05:00Z",
    });

    renderDialog();
    fireEvent.click(await screen.findByRole("button", { name: "驳回" }));

    await waitFor(() =>
      expect(api.reviewCharacterAsset).toHaveBeenCalledWith(
        "asset-FRONT_FACE",
        "REJECTED",
        "",
      ),
    );
    expect(await screen.findByText("已驳回")).toBeInTheDocument();
  });

  it("重新生成该视角提交新任务并携带幂等键", async () => {
    vi.mocked(api.listCharacterVersions).mockResolvedValue([makeVersion()]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([
      makeTask("FRONT_FACE", "SUCCEEDED"),
    ]);
    vi.mocked(api.listCharacterAssets).mockResolvedValue([
      makeAsset("FRONT_FACE", { review_status: "REJECTED" }),
    ]);
    vi.mocked(api.regenerateCharacterAsset).mockResolvedValue([
      makeTask("FRONT_FACE", "PENDING"),
    ]);

    renderDialog();
    fireEvent.click(
      await screen.findByRole("button", { name: "重新生成该视角" }),
    );

    await waitFor(() =>
      expect(api.regenerateCharacterAsset).toHaveBeenCalledWith(
        "asset-FRONT_FACE",
        expect.any(String),
      ),
    );
  });

  it("缺视角时补生成只提交缺失视角", async () => {
    vi.mocked(api.listCharacterVersions).mockResolvedValue([makeVersion()]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue([
      makeTask("FRONT_FACE", "SUCCEEDED"),
    ]);
    vi.mocked(api.listCharacterAssets).mockResolvedValue([
      makeAsset("FRONT_FACE", { review_status: "APPROVED" }),
    ]);
    vi.mocked(api.generateCharacterAssets).mockResolvedValue([
      makeTask("FRONT_HALF", "PENDING"),
    ]);

    renderDialog();
    fireEvent.click(
      await screen.findByRole("button", { name: "补生成 4 个视角" }),
    );

    await waitFor(() =>
      expect(api.generateCharacterAssets).toHaveBeenCalledWith(
        "version-1",
        expect.objectContaining({
          view_types: ["FRONT_HALF", "FRONT_FULL", "LEFT_45", "LEFT_SIDE"],
        }),
      ),
    );
  });

  it("五个视角全部批准后发布所选候选", async () => {
    vi.mocked(api.listCharacterVersions)
      .mockResolvedValueOnce([makeVersion()])
      .mockResolvedValue([
        makeVersion({
          status: "PUBLISHED",
          published_at: "2026-09-28T09:00:00Z",
        }),
      ]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue(
      REQUIRED_VIEWS.map((view) => makeTask(view, "SUCCEEDED")),
    );
    vi.mocked(api.listCharacterAssets).mockResolvedValue(
      REQUIRED_VIEWS.map((view) =>
        makeAsset(view, { review_status: "APPROVED" }),
      ),
    );
    vi.mocked(api.publishCharacterVersion).mockResolvedValue(
      makeVersion({
        status: "PUBLISHED",
        published_at: "2026-09-28T09:00:00Z",
      }),
    );
    const onPublished = vi.fn();

    renderDialog(onPublished);
    const publishButton = await screen.findByRole("button", {
      name: "发布角色版本",
    });
    await waitFor(() => expect(publishButton).toBeEnabled());

    fireEvent.click(publishButton);

    await waitFor(() =>
      expect(api.publishCharacterVersion).toHaveBeenCalledWith("version-1", {
        FRONT_FACE: "asset-FRONT_FACE",
        FRONT_HALF: "asset-FRONT_HALF",
        FRONT_FULL: "asset-FRONT_FULL",
        LEFT_45: "asset-LEFT_45",
        LEFT_SIDE: "asset-LEFT_SIDE",
      }),
    );
    await waitFor(() => expect(onPublished).toHaveBeenCalled());
    expect(
      await screen.findByText("角色版本 v1 已发布，五个视角已冻结。"),
    ).toBeInTheDocument();
  });

  it("未全部批准时发布按钮禁用并提示缺口", async () => {
    vi.mocked(api.listCharacterVersions).mockResolvedValue([makeVersion()]);
    vi.mocked(api.listCharacterGenerationTasks).mockResolvedValue(
      REQUIRED_VIEWS.map((view) => makeTask(view, "SUCCEEDED")),
    );
    vi.mocked(api.listCharacterAssets).mockResolvedValue(
      REQUIRED_VIEWS.map((view, index) =>
        makeAsset(view, {
          review_status: index === 0 ? "NOT_REVIEWED" : "APPROVED",
        }),
      ),
    );

    renderDialog();

    const publishButton = await screen.findByRole("button", {
      name: "发布角色版本",
    });
    expect(publishButton).toBeDisabled();
    expect(
      screen.getByText(
        "发布前需为五个视角各批准并选择一张候选（已批准 4/5）。",
      ),
    ).toBeInTheDocument();
  });

  it("已发布版本展示冻结视角并提供创建新版本", async () => {
    vi.mocked(api.listCharacterVersions).mockResolvedValue([
      makeVersion({
        status: "PUBLISHED",
        published_at: "2026-09-28T09:00:00Z",
      }),
    ]);
    vi.mocked(api.listCharacterAssets).mockResolvedValue(
      REQUIRED_VIEWS.map((view) =>
        makeAsset(view, {
          review_status: "APPROVED",
          is_published_selection: true,
        }),
      ),
    );

    renderDialog();

    expect(
      await screen.findByText("五个视角已冻结，如需调整请创建新版本。"),
    ).toBeInTheDocument();
    const figures = screen.getAllByRole("figure");
    expect(figures).toHaveLength(5);
    expect(
      figures.filter((figure) => within(figure).queryByText("已发布")),
    ).toHaveLength(5);
    const newVersionButton = screen.getByRole("button", {
      name: "创建新版本",
    });
    vi.mocked(api.createCharacterVersion).mockResolvedValue(
      makeVersion({ id: "version-2", version_number: 2, status: "DRAFT" }),
    );
    fireEvent.click(newVersionButton);
    await waitFor(() =>
      expect(api.createCharacterVersion).toHaveBeenCalledWith("persona-1", {
        provider: "apilio",
        model: "gpt-image-2",
        generation_params_json: {},
      }),
    );
  });

  it("创建版本失败时展示可重试的错误", async () => {
    renderDialog();
    vi.mocked(api.createCharacterVersion).mockRejectedValue(
      new Error("创建角色版本失败"),
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "创建版本并生成五视图" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "创建角色版本失败",
    );
  });
});
