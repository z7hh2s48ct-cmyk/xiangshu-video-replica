import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { CharacterSelection } from "./CharacterSelection";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    chooseProjectMainCharacterVersion: vi.fn(),
    getAssetDownloadUrl: vi.fn(),
    getProjectMainCharacter: vi.fn(),
    listProjectCharacterVersions: vi.fn(),
  };
});

const viewTypes = [
  "FRONT_FACE",
  "FRONT_HALF",
  "FRONT_FULL",
  "LEFT_45",
  "LEFT_SIDE",
] as const;

const option: api.ProjectCharacterVersionOption = {
  character_version_id: "character-version-3",
  version_number: 3,
  identity_id: "identity-1",
  identity_name: "林夏",
  authorization_expires_at: "2035-01-01T00:00:00Z",
  persona_id: "persona-1",
  persona_snapshot_json: {
    name: "乡墅项目管理专家",
    occupation: "项目管理",
    costume_description: "工程马甲",
  },
  provider: "fake_character",
  model: "fake-character-v1",
  template_version: "character-prompt-v1",
  template_hash: "template-hash",
  published_at: "2030-01-01T00:00:00Z",
  publication_hash: "publication-hash",
  assets: viewTypes.map((viewType) => ({
    character_asset_id: `character-asset-${viewType}`,
    asset_id: `asset-${viewType}`,
    view_type: viewType,
  })),
};

const sceneOption: api.ProjectCharacterVersionOption = {
  ...option,
  character_version_id: "character-version-scene-1",
  version_number: 1,
  persona_id: "persona-scene-1",
  persona_snapshot_json: {
    ...option.persona_snapshot_json,
    name: "工地巡检",
    scene_description: "在施工现场检查工程进度",
    costume_description: "白色安全帽和反光工程马甲",
    appearance_constraints_json: {
      appearance_type: "scene",
    },
  },
  published_at: "2031-01-01T00:00:00Z",
};

const selected: api.ProjectMainCharacter = {
  project_id: "project-1",
  character_id: null,
  character_version_id: option.character_version_id,
  version_id: "project-character-selection-1",
  version_number: 1,
  character_snapshot: {
    schema_version: "project-character-selection.v1",
    character_version_id: option.character_version_id,
    character_version_number: option.version_number,
    identity: {
      id: option.identity_id,
      display_name: option.identity_name,
      authorization_expires_at: option.authorization_expires_at,
    },
    persona_id: option.persona_id,
    persona_snapshot_json: option.persona_snapshot_json,
    provider: option.provider,
    model: option.model,
    published_assets: option.assets,
  },
};

describe("CharacterSelection", () => {
  it("requires explicit scene selection and excludes the restored base appearance", async () => {
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(selected);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      option,
      sceneOption,
    ]);
    const onVersionChange = vi.fn();
    render(
      <CharacterSelection
        projectId="scene-only"
        variant="inline"
        sceneOnly
        onVersionChange={onVersionChange}
      />,
    );
    const dropdown = await screen.findByRole("combobox", {
      name: "人物场景形象",
    });
    expect(screen.getByText("人物场景")).toBeInTheDocument();
    await screen.findByRole("option", { name: /工地巡检/ });
    expect(
      within(dropdown).queryByRole("option", { name: /基础形象/ }),
    ).not.toBeInTheDocument();
    expect(dropdown).toHaveValue("");
    expect(onVersionChange).toHaveBeenCalledWith(null);
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
  });

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(null);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([option]);
    vi.mocked(api.chooseProjectMainCharacterVersion).mockResolvedValue(
      selected,
    );
    vi.mocked(api.getAssetDownloadUrl).mockResolvedValue({
      url: "/scene.jpg",
    });
  });

  it("sceneOnly 只展示一张已选场景图", async () => {
    const sceneSelected: api.ProjectMainCharacter = {
      ...selected,
      character_version_id: sceneOption.character_version_id,
      character_snapshot: {
        ...selected.character_snapshot,
        character_version_id: sceneOption.character_version_id,
        character_version_number: sceneOption.version_number,
        persona_id: sceneOption.persona_id,
        persona_snapshot_json: sceneOption.persona_snapshot_json,
        published_assets: sceneOption.assets,
      },
    };
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(sceneSelected);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      sceneOption,
    ]);
    render(<CharacterSelection projectId="scene" variant="inline" sceneOnly />);
    expect(await screen.findByAltText("已选场景图")).toHaveAttribute(
      "src",
      "/scene.jpg",
    );
    expect(screen.getAllByRole("img", { name: "已选场景图" })).toHaveLength(1);
    expect(screen.getByLabelText("人物场景形象")).toBeInTheDocument();
    expect(api.getAssetDownloadUrl).toHaveBeenCalledWith("asset-FRONT_FULL");
  });

  it("sceneOnly 预览失败时显示可重试提示", async () => {
    const sceneSelected: api.ProjectMainCharacter = {
      ...selected,
      character_version_id: sceneOption.character_version_id,
      character_snapshot: {
        ...selected.character_snapshot,
        character_version_id: sceneOption.character_version_id,
        character_version_number: sceneOption.version_number,
        persona_id: sceneOption.persona_id,
        persona_snapshot_json: sceneOption.persona_snapshot_json,
        published_assets: sceneOption.assets,
      },
    };
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(sceneSelected);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      sceneOption,
    ]);
    vi.mocked(api.getAssetDownloadUrl)
      .mockRejectedValueOnce(new Error("预览加载失败"))
      .mockResolvedValueOnce({ url: "/scene-retried.jpg" });

    render(<CharacterSelection projectId="scene" variant="inline" sceneOnly />);

    expect(await screen.findByText("场景图预览加载失败。")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试预览" }));
    expect(await screen.findByAltText("已选场景图")).toHaveAttribute(
      "src",
      "/scene-retried.jpg",
    );
    expect(api.getAssetDownloadUrl).toHaveBeenCalledTimes(2);
  });

  it("sceneOnly 图片渲染失败后可重试，且旧重试不覆盖新场景", async () => {
    const sceneSelected: api.ProjectMainCharacter = {
      ...selected,
      character_version_id: sceneOption.character_version_id,
      character_snapshot: {
        ...selected.character_snapshot,
        character_version_id: sceneOption.character_version_id,
        character_version_number: sceneOption.version_number,
        persona_id: sceneOption.persona_id,
        persona_snapshot_json: sceneOption.persona_snapshot_json,
        published_assets: sceneOption.assets,
      },
    };
    const secondScene = {
      ...sceneOption,
      character_version_id: "character-version-scene-2",
      version_number: 2,
      persona_snapshot_json: {
        ...sceneOption.persona_snapshot_json,
        name: "室内洽谈",
      },
      assets: sceneOption.assets.map((asset) => ({
        ...asset,
        character_asset_id: `${asset.character_asset_id}-2`,
        asset_id: `${asset.asset_id}-2`,
      })),
    } satisfies api.ProjectCharacterVersionOption;
    const secondSelection: api.ProjectMainCharacter = {
      ...sceneSelected,
      character_version_id: secondScene.character_version_id,
      character_snapshot: {
        ...sceneSelected.character_snapshot,
        character_version_id: secondScene.character_version_id,
        character_version_number: secondScene.version_number,
        persona_snapshot_json: secondScene.persona_snapshot_json,
        published_assets: secondScene.assets,
      },
    };
    let resolveOldRetry: ((value: api.DownloadUrl) => void) | undefined;
    const oldRetry = new Promise<api.DownloadUrl>((resolve) => {
      resolveOldRetry = resolve;
    });
    let firstSceneRequestCount = 0;
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(sceneSelected);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      sceneOption,
      secondScene,
    ]);
    vi.mocked(api.chooseProjectMainCharacterVersion).mockResolvedValue(
      secondSelection,
    );
    vi.mocked(api.getAssetDownloadUrl).mockImplementation((assetId) => {
      if (assetId === "asset-FRONT_FULL") {
        firstSceneRequestCount += 1;
        return firstSceneRequestCount === 1
          ? Promise.resolve({ url: "/scene-a.jpg" })
          : oldRetry;
      }
      return Promise.resolve({ url: "/scene-b.jpg" });
    });

    render(<CharacterSelection projectId="scene" variant="inline" sceneOnly />);
    const preview = await screen.findByAltText("已选场景图");
    fireEvent.error(preview);
    expect(await screen.findByText("场景图预览加载失败。")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "重试预览" }));

    const dropdown = screen.getByRole("combobox", {
      name: "人物场景形象",
    });
    fireEvent.change(dropdown, {
      target: { value: secondScene.character_version_id },
    });
    expect(await screen.findByAltText("已选场景图")).toHaveAttribute(
      "src",
      "/scene-b.jpg",
    );

    await act(async () => {
      resolveOldRetry?.({ url: "/scene-a-late.jpg" });
      await oldRetry;
    });
    expect(screen.getByAltText("已选场景图")).toHaveAttribute(
      "src",
      "/scene-b.jpg",
    );
  });

  it("sceneOnly 切换到新场景时重建预览节点", async () => {
    const sceneSelected: api.ProjectMainCharacter = {
      ...selected,
      character_version_id: sceneOption.character_version_id,
      character_snapshot: {
        ...selected.character_snapshot,
        character_version_id: sceneOption.character_version_id,
        character_version_number: sceneOption.version_number,
        persona_id: sceneOption.persona_id,
        persona_snapshot_json: sceneOption.persona_snapshot_json,
        published_assets: sceneOption.assets,
      },
    };
    const secondScene = {
      ...sceneOption,
      character_version_id: "character-version-scene-2",
      version_number: 2,
      assets: sceneOption.assets.map((asset) => ({
        ...asset,
        character_asset_id: `${asset.character_asset_id}-2`,
        asset_id: `${asset.asset_id}-2`,
      })),
    } satisfies api.ProjectCharacterVersionOption;
    const secondSelection: api.ProjectMainCharacter = {
      ...sceneSelected,
      character_version_id: secondScene.character_version_id,
      character_snapshot: {
        ...sceneSelected.character_snapshot,
        character_version_id: secondScene.character_version_id,
        character_version_number: secondScene.version_number,
        published_assets: secondScene.assets,
      },
    };
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(sceneSelected);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      sceneOption,
      secondScene,
    ]);
    vi.mocked(api.chooseProjectMainCharacterVersion).mockResolvedValue(
      secondSelection,
    );
    vi.mocked(api.getAssetDownloadUrl).mockResolvedValue({
      url: "/shared-scene.jpg",
    });

    render(<CharacterSelection projectId="scene" variant="inline" sceneOnly />);
    const oldPreview = await screen.findByAltText("已选场景图");
    fireEvent.change(screen.getByRole("combobox", { name: "人物场景形象" }), {
      target: { value: secondScene.character_version_id },
    });

    await waitFor(() =>
      expect(api.getAssetDownloadUrl).toHaveBeenCalledWith(
        "asset-FRONT_FULL-2",
      ),
    );
    const newPreview = await screen.findByAltText("已选场景图");
    expect(newPreview).not.toBe(oldPreview);
    expect(newPreview).toHaveAttribute("src", "/shared-scene.jpg");
  });

  it("sceneOnly 切换项目时立即隐藏旧项目预览", async () => {
    const sceneSelected: api.ProjectMainCharacter = {
      ...selected,
      character_version_id: sceneOption.character_version_id,
      character_snapshot: {
        ...selected.character_snapshot,
        character_version_id: sceneOption.character_version_id,
        character_version_number: sceneOption.version_number,
        persona_id: sceneOption.persona_id,
        persona_snapshot_json: sceneOption.persona_snapshot_json,
        published_assets: sceneOption.assets,
      },
    };
    let resolveNextProject:
      | ((selection: api.ProjectMainCharacter | null) => void)
      | undefined;
    const nextProject = new Promise<api.ProjectMainCharacter | null>(
      (resolve) => {
        resolveNextProject = resolve;
      },
    );
    vi.mocked(api.getProjectMainCharacter)
      .mockResolvedValueOnce(sceneSelected)
      .mockReturnValueOnce(nextProject);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      sceneOption,
    ]);

    const page = render(
      <CharacterSelection projectId="scene-a" variant="inline" sceneOnly />,
    );
    expect(await screen.findByAltText("已选场景图")).toBeVisible();

    page.rerender(
      <CharacterSelection projectId="scene-b" variant="inline" sceneOnly />,
    );
    expect(screen.queryByAltText("已选场景图")).toBeNull();
    await act(async () => {
      resolveNextProject?.(null);
      await nextProject;
    });
  });

  it("sceneOnly 切换人物场景后立即保存并通知父级", async () => {
    const secondScene = {
      ...sceneOption,
      character_version_id: "character-version-scene-2",
      version_number: 2,
      persona_snapshot_json: {
        ...sceneOption.persona_snapshot_json,
        name: "室内洽谈",
        scene_description: "在会客厅讲解方案",
      },
    } satisfies api.ProjectCharacterVersionOption;
    const secondSelection: api.ProjectMainCharacter = {
      ...selected,
      character_version_id: secondScene.character_version_id,
      character_snapshot: {
        ...selected.character_snapshot,
        character_version_id: secondScene.character_version_id,
        character_version_number: secondScene.version_number,
        persona_id: secondScene.persona_id,
        persona_snapshot_json: secondScene.persona_snapshot_json,
        published_assets: secondScene.assets,
      },
    };
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      sceneOption,
      secondScene,
    ]);
    vi.mocked(api.chooseProjectMainCharacterVersion).mockResolvedValue(
      secondSelection,
    );
    const onVersionChange = vi.fn();
    render(
      <CharacterSelection
        projectId="scene"
        variant="inline"
        sceneOnly
        onVersionChange={onVersionChange}
      />,
    );

    const dropdown = await screen.findByRole("combobox", {
      name: "人物场景形象",
    });
    await screen.findByRole("option", { name: /室内洽谈/ });
    expect(dropdown).toBeEnabled();
    fireEvent.change(dropdown, {
      target: { value: secondScene.character_version_id },
    });

    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "scene",
        secondScene.character_version_id,
      ),
    );
    expect(onVersionChange).toHaveBeenLastCalledWith(secondSelection);
  });

  it("sceneOnly 空列表引导到人物库创建场景形象", async () => {
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([]);
    render(<CharacterSelection projectId="scene" variant="inline" sceneOnly />);

    expect(
      await screen.findByRole("option", { name: "暂无可用场景形象" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("请到人物库创建并发布人物场景形象。"),
    ).toBeInTheDocument();
  });

  it("restores the frozen role version as soon as the project opens", async () => {
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(selected);
    const onSelectionChange = vi.fn();
    const onVersionChange = vi.fn();

    render(
      <CharacterSelection
        onSelectionChange={onSelectionChange}
        onVersionChange={onVersionChange}
        projectId="project-1"
      />,
    );

    expect(await screen.findByText("当前角色：林夏")).toBeInTheDocument();
    expect(screen.getByText("乡墅项目管理专家 · V3")).toBeInTheDocument();
    expect(screen.getByText("授权有效至 2035-01-01")).toBeInTheDocument();
    expect(api.getProjectMainCharacter).toHaveBeenCalledWith("project-1");
    expect(onSelectionChange).toHaveBeenLastCalledWith(true);
    expect(onVersionChange).toHaveBeenLastCalledWith(selected);
  });

  it("prompts the project to switch when the same character has a newer published version", async () => {
    const olderOption: api.ProjectCharacterVersionOption = {
      ...option,
      character_version_id: "character-version-2",
      version_number: 2,
      published_at: "2029-01-01T00:00:00Z",
    };
    const olderSelection: api.ProjectMainCharacter = {
      ...selected,
      character_version_id: olderOption.character_version_id,
      character_snapshot: {
        ...selected.character_snapshot,
        character_version_id: olderOption.character_version_id,
        character_version_number: 2,
      },
    };
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(olderSelection);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      olderOption,
      option,
    ]);

    render(<CharacterSelection projectId="project-1" variant="inline" />);

    expect(
      await screen.findByText("人物库已有新版本 V3，当前项目仍使用 V2。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "切换到 V3" }));
    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
  });

  it("shows only server-approved options with all five published assets", async () => {
    render(<CharacterSelection projectId="project-1" />);

    await screen.findByText(/已自动选择角色版本/);
    fireEvent.click(screen.getByRole("button", { name: "更换" }));

    const optionLabel =
      await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/);
    const optionCard = optionLabel.closest("label");
    expect(optionCard).not.toBeNull();
    expect(
      within(optionCard as HTMLElement).getByText("项目管理"),
    ).toBeInTheDocument();
    expect(
      within(optionCard as HTMLElement).getByText("授权有效至 2035-01-01"),
    ).toBeInTheDocument();

    fireEvent.click(optionLabel);
    const assets = await screen.findByRole("list", { name: "五类已发布资产" });
    expect(within(assets).getAllByRole("listitem")).toHaveLength(5);
    expect(within(assets).getByText("正脸近景")).toBeInTheDocument();
    expect(within(assets).getByText("左侧面")).toBeInTheDocument();
  });

  it("saves the immutable version id and advances the project workflow", async () => {
    const onSelectionChange = vi.fn();
    render(
      <CharacterSelection
        onSelectionChange={onSelectionChange}
        projectId="project-1"
      />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "选择角色版本" }),
    );
    fireEvent.click(await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/));
    fireEvent.click(screen.getByRole("button", { name: "确认角色版本" }));

    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
    expect(
      await screen.findByText("已选择角色“林夏 · 乡墅项目管理专家 V3”。"),
    ).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(true);
  });

  it("reports selection writes as busy until the server mutation settles", async () => {
    let resolveSelection:
      | ((value: api.ProjectMainCharacter) => void)
      | undefined;
    vi.mocked(api.chooseProjectMainCharacterVersion).mockReturnValue(
      new Promise((resolve) => {
        resolveSelection = resolve;
      }),
    );
    const onBusyChange = vi.fn();
    render(
      <CharacterSelection onBusyChange={onBusyChange} projectId="project-1" />,
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "选择角色版本" }),
    );
    fireEvent.click(await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/));
    fireEvent.click(screen.getByRole("button", { name: "确认角色版本" }));

    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(true));
    resolveSelection?.(selected);
    await waitFor(() => expect(onBusyChange).toHaveBeenLastCalledWith(false));
  });

  it("lets auditors inspect options without rendering a write action", async () => {
    render(<CharacterSelection projectId="project-1" readOnly />);

    fireEvent.click(
      await screen.findByRole("button", { name: "查看角色版本" }),
    );
    const radio = await screen.findByLabelText(/林夏.*乡墅项目管理专家.*V3/);
    expect(radio).toBeDisabled();
    expect(screen.queryByRole("button", { name: "确认角色版本" })).toBeNull();
    expect(
      screen.getByText("只读身份不能更改项目角色版本。"),
    ).toBeInTheDocument();
  });

  // P0-03-01：角色版本自动预选（红灯先行）。

  it("auto-selects the latest published version and persists it when no selection exists", async () => {
    const olderOption: api.ProjectCharacterVersionOption = {
      ...option,
      character_version_id: "character-version-2",
      version_number: 2,
      published_at: "2029-01-01T00:00:00Z",
    };
    // 乱序返回，验证按 published_at 取最新而非列表首位。
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      olderOption,
      option,
    ]);
    const onSelectionChange = vi.fn();
    const onVersionChange = vi.fn();

    render(
      <CharacterSelection
        onSelectionChange={onSelectionChange}
        onVersionChange={onVersionChange}
        projectId="project-1"
      />,
    );

    // 自动预选落库：最近发布版本，重复选择服务端原子复用快照（任务 11 语义）。
    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
    expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("当前角色：林夏")).toBeInTheDocument();
    expect(onSelectionChange).toHaveBeenLastCalledWith(true);
    expect(onVersionChange).toHaveBeenLastCalledWith(selected);

    // 可撤回提示条：点击「更换」进入角色卡片改选。
    expect(
      await screen.findByText("已自动选择角色版本 林夏 · V3"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "更换" }));
    expect(
      await screen.findByText("选择一个不可变角色版本"),
    ).toBeInTheDocument();
  });

  it("prefers the base appearance over a newer scene look for automatic selection", async () => {
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      sceneOption,
      option,
    ]);

    render(<CharacterSelection projectId="project-1" />);

    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
  });

  it("labels scene looks explicitly in the inline first-frame selector", async () => {
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(selected);
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([
      option,
      sceneOption,
    ]);

    render(<CharacterSelection projectId="project-1" variant="inline" />);

    expect(
      await screen.findByRole("option", {
        name: "林夏 · 场景：工地巡检 · V1",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("option", {
        name: "林夏 · 人物基准：乡墅项目管理专家 · V3",
      }),
    ).toBeInTheDocument();
  });

  it("does not auto-select when a selection already exists", async () => {
    vi.mocked(api.getProjectMainCharacter).mockResolvedValue(selected);

    render(<CharacterSelection projectId="project-1" />);

    expect(await screen.findByText("当前角色：林夏")).toBeInTheDocument();
    expect(api.listProjectCharacterVersions).not.toHaveBeenCalled();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });

  it("shows guidance instead of an error when no versions are available", async () => {
    vi.mocked(api.listProjectCharacterVersions).mockResolvedValue([]);

    render(<CharacterSelection projectId="project-1" />);

    expect(
      await screen.findByText(/暂无可选角色版本，请先在人物库发布角色/),
    ).toBeInTheDocument();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "更换" })).toBeNull();
    expect(screen.queryByText(/失败。/)).toBeNull();
  });

  // P0-03-01 评审 Critical 1：生产接线中 onBusyChange 是内联回调，
  // busy 上报会触发父级重渲染并产生新引用；自动选择不得因此中止。
  // 用 pending list 复现真实网络时序：重渲染必须插进 list 在途窗口。
  it("completes auto-selection even when the busy callback identity changes mid-flight", async () => {
    let resolveList:
      | ((value: api.ProjectCharacterVersionOption[]) => void)
      | undefined;
    vi.mocked(api.listProjectCharacterVersions).mockReturnValue(
      new Promise((resolve) => {
        resolveList = resolve;
      }),
    );
    function Host() {
      const [, setTick] = useState(0);
      // 内联回调：Host 每次渲染都是新引用，模拟 App 接线的重渲染反馈。
      const handleBusyChange = () => setTick((tick) => tick + 1);
      return (
        <CharacterSelection
          onBusyChange={handleBusyChange}
          projectId="project-1"
        />
      );
    }

    render(<Host />);

    // 自动选择开始：busy 上报触发 Host 重渲染（新回调引用），list 仍在途。
    await waitFor(() =>
      expect(api.listProjectCharacterVersions).toHaveBeenCalledTimes(1),
    );
    resolveList?.([option]);

    await waitFor(() =>
      expect(api.chooseProjectMainCharacterVersion).toHaveBeenCalledWith(
        "project-1",
        option.character_version_id,
      ),
    );
    expect(
      await screen.findByText("已自动选择角色版本 林夏 · V3"),
    ).toBeInTheDocument();
  });

  // P0-03-01 评审 Major 2：restore 失败时快照状态未知，
  // 不得自动改绑，交回用户手动选择。
  it("does not auto-select when restoring the selection fails", async () => {
    vi.mocked(api.getProjectMainCharacter).mockRejectedValue(
      new Error("网络不可用"),
    );

    render(<CharacterSelection projectId="project-1" />);

    // restore 结束但无选择（引导文案出现）：快照状态未知，不得自动改绑。
    expect(
      await screen.findByText(
        "选择一个已发布且授权有效的角色版本，用于后续人物参考匹配。",
      ),
    ).toBeInTheDocument();
    expect(api.listProjectCharacterVersions).not.toHaveBeenCalled();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });

  it("skips auto-selection for read-only visitors", async () => {
    render(<CharacterSelection projectId="project-1" readOnly />);

    expect(
      await screen.findByRole("button", { name: "查看角色版本" }),
    ).toBeInTheDocument();
    expect(api.listProjectCharacterVersions).not.toHaveBeenCalled();
    expect(api.chooseProjectMainCharacterVersion).not.toHaveBeenCalled();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });

  it("falls back to manual selection guidance when the auto-selection write fails", async () => {
    vi.mocked(api.chooseProjectMainCharacterVersion).mockRejectedValue(
      new Error("写入失败"),
    );

    render(<CharacterSelection projectId="project-1" />);

    expect(
      await screen.findByText("未自动选择角色版本，请手动选择角色版本。"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/已自动选择角色版本/)).toBeNull();
  });
});
