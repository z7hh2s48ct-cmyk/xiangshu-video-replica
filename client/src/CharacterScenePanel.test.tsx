import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import * as api from "./api";
import { CharacterScenePanel } from "./CharacterScenePanel";

vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  listCharacterSceneLooks: vi.fn(),
  getLatestSceneLookTask: vi.fn(),
  waitForCharacterSheetTask: vi.fn(),
  createCharacterSceneLook: vi.fn(),
  getCachedCharacterAssetUrl: vi.fn(),
}));
const look: api.SimpleSceneLook = {
  identity_id: "p1",
  persona_id: "scene1",
  character_version_id: "v1",
  scene_name: "办公室职场",
  scene_description: "办公室",
  costume_description: "正装",
  contact_sheet_asset_id: "sheet1",
  generation_source: "image_provider",
  views: [{ asset_id: "face1", view_type: "FRONT_FACE" }],
  published_at: "2026-09-13",
};
const task = (status: api.CharacterSheetTask["status"]) =>
  ({
    id: "task1",
    identity_id: "p1",
    status,
    operation: "SCENE",
    result: status === "SUCCEEDED" ? look : null,
  }) as api.CharacterSheetTask;
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listCharacterSceneLooks).mockResolvedValue([]);
  vi.mocked(api.getLatestSceneLookTask).mockResolvedValue(null);
  vi.mocked(api.getCachedCharacterAssetUrl).mockImplementation(async (id) => ({
    url: `/${id}.png`,
    expires_at: "2099-01-01",
  }));
});

it("返回人物后恢复后台任务，完成后刷新并按一套展示", async () => {
  vi.mocked(api.getLatestSceneLookTask).mockResolvedValue(task("RUNNING"));
  const finished = deferred<api.CharacterSheetTask>();
  vi.mocked(api.waitForCharacterSheetTask).mockReturnValue(finished.promise);
  const changed = vi.fn();
  render(
    <CharacterScenePanel
      identityId="p1"
      displayName="人物"
      canManage
      onChanged={changed}
    />,
  );
  expect(
    await screen.findByRole("status", { name: "场景造型生成进度" }),
  ).toHaveTextContent("正在生成五视图");
  fireEvent.click(screen.getByRole("button", { name: "新增场景造型" }));
  expect(screen.getByRole("button", { name: /正在生成，预计/ })).toBeDisabled();
  await act(async () => finished.resolve(task("SUCCEEDED")));
  expect(
    await screen.findByRole("button", { name: "查看办公室职场五视图" }),
  ).toBeInTheDocument();
  expect(changed).toHaveBeenCalledTimes(1);
  expect(api.createCharacterSceneLook).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "查看办公室职场五视图" }));
  expect(await screen.findByAltText("办公室职场 正面特写")).toBeInTheDocument();
});

it("列表读取失败仍恢复任务；任务读取失败时禁止重复生成，刷新后可恢复", async () => {
  vi.mocked(api.listCharacterSceneLooks).mockRejectedValueOnce(
    new Error("列表暂不可用"),
  );
  vi.mocked(api.getLatestSceneLookTask).mockResolvedValueOnce(task("RUNNING"));
  vi.mocked(api.waitForCharacterSheetTask).mockReturnValue(
    new Promise(() => {}),
  );
  const mounted = render(
    <CharacterScenePanel identityId="p1" displayName="人物" canManage />,
  );
  expect(
    await screen.findByRole("status", { name: "场景造型生成进度" }),
  ).toBeInTheDocument();
  mounted.unmount();
  vi.mocked(api.getLatestSceneLookTask).mockRejectedValueOnce(
    new Error("网络失败"),
  );
  render(
    <CharacterScenePanel
      identityId="p1"
      displayName="人物"
      canManage
      createRequest={1}
    />,
  );
  expect(await screen.findByText(/读取生成任务状态失败/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "生成场景五视图" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "刷新场景" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "生成场景五视图" }),
    ).toBeEnabled(),
  );
});

it("未知提交态禁止重提，只读用户没有生成入口", async () => {
  vi.mocked(api.getLatestSceneLookTask).mockResolvedValue(
    task("SUBMISSION_UNCERTAIN"),
  );
  const mounted = render(
    <CharacterScenePanel
      identityId="p1"
      displayName="人物"
      canManage
      createRequest={1}
    />,
  );
  expect(await screen.findByText(/提交结果待核对/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "生成场景五视图" })).toBeDisabled();
  mounted.unmount();
  render(
    <CharacterScenePanel
      identityId="p1"
      displayName="人物"
      canManage={false}
      createRequest={1}
    />,
  );
  await waitFor(() =>
    expect(api.getLatestSceneLookTask).toHaveBeenCalledTimes(2),
  );
  expect(screen.queryByLabelText("场景名称")).not.toBeInTheDocument();
});

it("切换人物后忽略旧人物迟到的生成结果", async () => {
  const finished = deferred<api.CharacterSheetTask>();
  vi.mocked(api.getLatestSceneLookTask).mockResolvedValueOnce(task("RUNNING"));
  vi.mocked(api.waitForCharacterSheetTask).mockReturnValue(finished.promise);
  const changed = vi.fn();
  const mounted = render(
    <CharacterScenePanel
      key="p1"
      identityId="p1"
      displayName="人物甲"
      canManage
      onChanged={changed}
    />,
  );
  await screen.findByRole("status", { name: "场景造型生成进度" });
  mounted.rerender(
    <CharacterScenePanel
      key="p2"
      identityId="p2"
      displayName="人物乙"
      canManage
      onChanged={changed}
    />,
  );
  await screen.findByText("还没有场景形象照");
  await act(async () => finished.resolve(task("SUCCEEDED")));
  expect(screen.queryByText("办公室职场")).not.toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
});

it("提交回执丢失后先核对后台任务，不直接重复生成", async () => {
  vi.mocked(api.createCharacterSceneLook).mockRejectedValue(
    new Error("连接中断"),
  );
  render(
    <CharacterScenePanel
      identityId="p1"
      displayName="人物"
      canManage
      createRequest={1}
    />,
  );
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "生成场景五视图" }),
    ).toBeEnabled(),
  );
  fireEvent.change(screen.getByLabelText("场景名称"), {
    target: { value: "办公室" },
  });
  fireEvent.change(screen.getByLabelText("场景描述"), {
    target: { value: "日间办公室" },
  });
  fireEvent.change(screen.getByLabelText("服装描述"), {
    target: { value: "西装" },
  });
  fireEvent.click(screen.getByRole("button", { name: "生成场景五视图" }));
  expect(
    await screen.findByText(/请先刷新场景核对提交状态/),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "生成场景五视图" })).toBeDisabled();
  vi.mocked(api.getLatestSceneLookTask).mockResolvedValue(task("RUNNING"));
  vi.mocked(api.waitForCharacterSheetTask).mockReturnValue(
    new Promise(() => {}),
  );
  fireEvent.click(screen.getByRole("button", { name: "刷新场景" }));
  expect(
    await screen.findByRole("status", { name: "场景造型生成进度" }),
  ).toHaveTextContent("正在生成五视图");
  expect(api.createCharacterSceneLook).toHaveBeenCalledTimes(1);
});
