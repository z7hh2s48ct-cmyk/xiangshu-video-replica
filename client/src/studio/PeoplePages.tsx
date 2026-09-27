import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import {
  confirmOralVoice,
  createMaterialUploadIntent,
  createOralAvatarClone,
  createOralConsent,
  createOralVoiceClone,
  customerVisibleErrorMessage,
  deleteOralAvatar,
  deleteOralVoice,
  deleteSimpleCharacterIdentity,
  listOralAvatars,
  listOralVoices,
  type OralVoiceLanguage,
  type OralVoiceSettings,
  putMaterial,
  refreshOralAvatar,
  refreshOralVoice,
  renameOralAvatar,
  renameOralVoice,
  updateOralVoiceSettings,
  updateSimpleCharacterProfile,
} from "../api";
import { CharacterScenePanel } from "../CharacterScenePanel";
import {
  CloneNameSearch,
  CloneSearchStatus,
  DeleteIconButton,
  InlineCloneName,
  useCloneNameSearch,
} from "./cloneLibraryTools";
import { useStudio } from "./context";
import {
  loadMorePeople,
  loadPersonAssets,
  loadStudioVoice,
  readAudioDuration,
  uploadOralAudioMaterial,
  VOICE_CLONE_ACCEPT,
  validateOralAudioFile,
} from "./live";
import type {
  StudioAvatar,
  StudioPage,
  StudioPerson,
  StudioVoice,
} from "./types";
import {
  Button,
  Empty,
  Field,
  Hint,
  Icon,
  Media,
  Panel,
  StudioDialog,
  Tabs,
} from "./ui";
import {
  DEFAULT_VOICE_LANGUAGE,
  DEFAULT_VOICE_SETTINGS,
  describeVoiceSetting,
  formatVoiceSettings,
  roundVoiceSetting,
  VOICE_LANGUAGE_OPTIONS,
  VOICE_SETTING_FIELDS,
  VOICE_SETTING_STEP,
  voiceLanguageLabel,
} from "./voiceOptions";
import "./people.css";
import "./oral.css";

const personTabs: Array<{ id: StudioPage; label: string }> = [
  { id: "person-ip", label: "IP 定位" },
  { id: "person-photos", label: "形象照片" },
  { id: "person-avatars", label: "口播分身" },
  { id: "person-voices", label: "声音档案" },
];

const MAX_ORAL_SOURCE_BYTES = 50 * 1024 * 1024;
const ORAL_POLL_INTERVAL_MS = 6_000;
const ORAL_POLL_MAX_ATTEMPTS = 50;

type UploadedOralSource = { assetId: string; fileName: string };
type CloneSubmission = {
  fingerprint: string;
  idempotencyKey: string;
  consentId?: string;
};

function createCloneIdempotencyKey(kind: "avatar" | "voice") {
  const suffix =
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `oral-${kind}-${suffix}`;
}

function validateOralSource(file: File, kind: "video" | "audio") {
  const suffix = file.name.toLowerCase().split(".").pop();
  const allowed = kind === "video" ? ["mp4", "mov"] : ["mp3"];
  if (!suffix || !allowed.includes(suffix)) {
    return kind === "video" ? "仅支持 MP4 或 MOV 视频。" : "仅支持 MP3 音频。";
  }
  if (file.size <= 0) return "上传文件不能为空。";
  if (file.size > MAX_ORAL_SOURCE_BYTES) return "上传文件不能超过 50 MB。";
  return undefined;
}

async function uploadOralSource(
  file: File,
  group: string,
  onProgress: (progress: number) => void,
  signal?: AbortSignal,
): Promise<UploadedOralSource> {
  const intent = await createMaterialUploadIntent(file, {
    title: file.name,
    group,
  });
  const material = await putMaterial(intent, file, onProgress, signal);
  const assetId = material.asset_id ?? intent.asset_id;
  return { assetId, fileName: file.name };
}

function useOralStatusPolling(
  enabled: boolean,
  ids: string[],
  refreshItem: (id: string) => Promise<unknown>,
  refreshPage: (() => void) | undefined,
  setError: (message: string) => void,
  fallbackError: string,
) {
  const idKey = ids.join("\n");
  useEffect(() => {
    if (!enabled || !idKey) return;
    let active = true;
    let attempts = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      const results = await Promise.allSettled(
        idKey.split("\n").map((id) => refreshItem(id)),
      );
      if (!active) return;
      const rejected = results.find(
        (result): result is PromiseRejectedResult =>
          result.status === "rejected",
      );
      if (rejected) {
        setError(customerVisibleErrorMessage(rejected.reason, fallbackError));
      }
      if (results.some((result) => result.status === "fulfilled"))
        refreshPage?.();
      attempts += 1;
      if (attempts < ORAL_POLL_MAX_ATTEMPTS) {
        timer = setTimeout(poll, ORAL_POLL_INTERVAL_MS);
      }
    };
    timer = setTimeout(poll, ORAL_POLL_INTERVAL_MS);
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [enabled, fallbackError, idKey, refreshItem, refreshPage, setError]);
}

function selectedPerson(people: StudioPerson[], id?: string) {
  return id ? people.find((person) => person.id === id) : people[0];
}

export function PeoplePage() {
  const { data, navigate, openLive, review, updateData, notify, user } =
    useStudio();
  const readOnly = user.role === "auditor";
  const [loadingMore, setLoadingMore] = useState(false);
  const people = data.people;
  const peoplePage = data.pagination?.people;
  const loadNextPeoplePage = async () => {
    if (review || loadingMore || !peoplePage?.nextCursor) return;
    setLoadingMore(true);
    try {
      const result = await loadMorePeople(peoplePage.nextCursor);
      updateData((current) => {
        const knownPeople = new Set(current.people.map((person) => person.id));
        const knownAssets = new Set(current.assets.map((asset) => asset.id));
        return {
          ...current,
          people: [
            ...current.people,
            ...result.people.filter((person) => !knownPeople.has(person.id)),
          ],
          assets: [
            ...current.assets,
            ...result.assets.filter((asset) => !knownAssets.has(asset.id)),
          ],
          errors: [...current.errors, ...result.errors],
          pagination: {
            ...current.pagination,
            people: {
              nextCursor: result.nextCursor,
              total: result.total,
            },
          },
        };
      });
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "加载更多人物失败，请重试。"));
    } finally {
      setLoadingMore(false);
    }
  };
  return (
    <section className="people-page" aria-label="人物库">
      <div className="people-toolbar">
        <div>
          <h1>人物库</h1>
          <p>统一管理人物定位、形象照片、口播分身与声音</p>
        </div>
        <Button
          aria-label="新增人物"
          disabled={readOnly}
          variant="primary"
          onClick={() => {
            if (!readOnly) openLive("characters");
          }}
        >
          ＋ 新增人物
        </Button>
      </div>
      {people.length === 0 ? (
        <Empty
          title="还没有人物"
          description="从人物库创建第一位乡墅行业 IP。"
          action={
            <Button
              disabled={readOnly}
              variant="primary"
              onClick={() => {
                if (readOnly) return;
                openLive("characters");
              }}
            >
              新增人物
            </Button>
          }
        />
      ) : (
        <div className="people-grid">
          {people.map((person) => (
            <PersonCard
              key={person.id}
              person={person}
              onOpen={() =>
                navigate("person-ip", { selectedPersonId: person.id })
              }
            />
          ))}
        </div>
      )}
      {peoplePage && peoplePage.total > people.length ? (
        <div className="people-pagination">
          <span>
            已加载 {people.length} / {peoplePage.total} 位人物
          </span>
          <Button
            disabled={loadingMore || !peoplePage.nextCursor}
            onClick={() => void loadNextPeoplePage()}
          >
            {loadingMore ? "正在加载…" : "加载更多人物"}
          </Button>
        </div>
      ) : null}
      <Hint>
        形象照片用于画面创作；数字人口播使用视频分身、已确认的克隆声音和文案。
      </Hint>
    </section>
  );
}

function PersonCard({
  person,
  onOpen,
}: {
  person: StudioPerson;
  onOpen(): void;
}) {
  const { navigate, review, notify, refresh, updateData } = useStudio();
  const portrait = person.portrait;
  const voices = person.voices.filter((voice) => voice.confirmed).length;
  const avatars = person.avatars.filter((avatar) => avatar.ready).length;
  const photoCount =
    person.photoCount ?? (review ? person.photoIds.length : undefined);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteMode, setDeleteMode] = useState<"purge" | "keep">("purge");
  const [removeProjectRefs, setRemoveProjectRefs] = useState(false);
  // 删除是级联硬删：oral_avatars / oral_voices 对 person_identities 的外键是
  // ON DELETE CASCADE，口播成片与声音试听样例的 asset 行也在删除清单内，最后
  // 还会清理对象存储。逐条列出，别让用户以为只是「从列表移除」。
  const casualties = [
    `${person.avatars.length} 个口播分身`,
    `${person.voices.length} 个声音档案`,
    photoCount === undefined ? "全部形象照片" : `${photoCount} 张形象照片`,
    "已生成的口播成片与声音试听样例",
  ];
  const deletePerson = async () => {
    if (deleting) return;
    setDeleting(true);
    try {
      // 审核示例没有真实身份可删，只做本地移除。
      if (!review) {
        await deleteSimpleCharacterIdentity(person.id, {
          keepAssets: deleteMode === "keep",
          removeProjectRefs,
        });
        refresh();
      } else {
        updateData((current) => ({
          ...current,
          people: current.people.filter((item) => item.id !== person.id),
        }));
      }
      setConfirmingDelete(false);
      notify(
        deleteMode === "keep"
          ? `人物「${person.name}」已删除，素材已保留在素材库。`
          : `人物「${person.name}」已删除。`,
      );
    } catch (cause) {
      // 三种 409（账务历史 / 进行中任务 / 已被项目选用）后端返回的 message
      // 本身就是可直接展示的中文原因，原样透出比再包一层兜底更有用。
      notify(customerVisibleErrorMessage(cause, "删除人物失败，请稍后重试。"));
    } finally {
      setDeleting(false);
    }
  };
  return (
    <article className="person-card">
      {/* 固定 4:5 封面容器：Media 不带 fitContainer 时会按图片自然比例写内联
          样式，是人物卡片高度参差的根源；交给固定比例容器统一约束。 */}
      <div className="person-card__portrait">
        <Media
          asset={
            portrait
              ? {
                  id: `${person.id}-portrait`,
                  name: person.name,
                  kind: "image",
                  url: portrait,
                  group: "人物",
                  source: "人物库",
                  saved: true,
                }
              : undefined
          }
          alt={`${person.name}形象`}
          className="person-card__portrait-media"
          fitContainer
        />
      </div>
      <div className="person-card__body">
        <h2>{person.name}</h2>
        <p>{person.role}</p>
        <div className="person-card__meta">
          <span>
            {photoCount === undefined
              ? "形象照片数量未知"
              : `✓ 形象照片 ${photoCount} 张`}
          </span>
          <span>✓ 口播分身 {avatars} 个</span>
          <span className={voices ? "" : "is-warning"}>
            {voices ? `✓ 可用声音 ${voices} 个` : "！声音待添加"}
          </span>
        </div>
        <div className="person-card__actions">
          <Button variant="primary" onClick={onOpen}>
            查看人物
          </Button>
          <Button
            variant="outline"
            onClick={() =>
              navigate("person-photos", { selectedPersonId: person.id })
            }
          >
            用于创作
          </Button>
          <Button
            variant="quiet"
            disabled={deleting}
            onClick={() => {
              setDeleteMode("purge");
              setRemoveProjectRefs(false);
              setConfirmingDelete(true);
            }}
          >
            <Icon name="close" size={16} />
            删除
          </Button>
        </div>
      </div>
      {confirmingDelete ? (
        <StudioDialog
          title="删除人物"
          onClose={() => setConfirmingDelete(false)}
        >
          <div className="oral-upload-panel">
            <p className="oral-dialog-intro">
              确定删除人物「{person.name}」吗？此操作<strong>不可撤销</strong>
              ，请选择关联素材的处理方式：
            </p>
            <fieldset className="copy-methods" disabled={deleting}>
              <legend>素材处理</legend>
              <label>
                <input
                  type="radio"
                  name="person-delete-mode"
                  checked={deleteMode === "purge"}
                  onChange={() => setDeleteMode("purge")}
                />
                <strong>连同资产一起删除</strong>
                <small>该人物的全部图片、成片与样本一并删除</small>
              </label>
              <label>
                <input
                  type="radio"
                  name="person-delete-mode"
                  checked={deleteMode === "keep"}
                  onChange={() => setDeleteMode("keep")}
                />
                <strong>只删除人物，保留素材</strong>
                <small>
                  形象照片、场景照、口播成片与声音样本转存素材库「我的上传」；
                  口播分身与声音档案配置会一并删除
                </small>
              </label>
            </fieldset>
            {deleteMode === "purge" ? (
              <ul className="person-delete-casualties">
                {casualties.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            ) : null}
            <label className="oral-consent">
              <input
                aria-label="同时移除项目选用记录"
                checked={removeProjectRefs}
                disabled={deleting}
                type="checkbox"
                onChange={(event) => setRemoveProjectRefs(event.target.checked)}
              />
              <span>
                若该人物已被项目选用，同时移除项目选用记录
                （项目会回到「未选择人物」状态，可重新选择）
              </span>
            </label>
            <p className="oral-dialog-intro">
              若该人物已产生口播账务或存在进行中的任务，后端会拒绝删除并说明原因。
            </p>
            <div className="oral-dialog-actions">
              <Button
                variant="quiet"
                onClick={() => setConfirmingDelete(false)}
              >
                取消
              </Button>
              <Button
                variant="primary"
                disabled={deleting}
                onClick={() => void deletePerson()}
              >
                {deleting ? "删除中…" : "确认删除"}
              </Button>
            </div>
          </div>
        </StudioDialog>
      ) : null}
    </article>
  );
}

export function PersonPage() {
  const { state, data, navigate } = useStudio();
  const person = selectedPerson(data.people, state.selectedPersonId);
  const tab = state.page === "people" ? "person-ip" : state.page;
  if (!person)
    return (
      <Empty title="未选择人物" description="请先从人物库选择一位人物。" />
    );
  return (
    <section className="person-page" aria-label="人物详情">
      <button
        className="people-back"
        type="button"
        onClick={() =>
          navigate(state.returnTo ?? "people", { returnTo: undefined })
        }
      >
        ← 返回{state.returnTo ? "创作" : "人物库"}
      </button>
      <PersonHeader person={person} />
      <Tabs
        items={personTabs}
        value={tab}
        onChange={(next) =>
          navigate(next as StudioPage, { selectedPersonId: person.id })
        }
      />
      <div className="person-content">
        {tab === "person-ip" ? (
          <IpPanel key={person.id} person={person} />
        ) : null}
        {tab === "person-photos" ? (
          <PhotosPanel key={person.id} person={person} />
        ) : null}
        {tab === "person-avatars" ? (
          <AvatarPanel key={person.id} person={person} />
        ) : null}
        {tab === "person-voices" ? (
          <VoicePanel key={person.id} person={person} />
        ) : null}
      </div>
    </section>
  );
}

function PersonHeader({ person }: { person: StudioPerson }) {
  const portrait = person.portrait;
  return (
    <header className="person-header">
      <Media
        asset={
          portrait
            ? {
                id: `${person.id}-portrait`,
                name: person.name,
                kind: "image",
                url: portrait,
                group: "人物",
                source: "人物库",
                saved: true,
              }
            : undefined
        }
        alt={`${person.name}头像`}
        className="person-header__portrait"
      />
      <div>
        <h1>{person.name}</h1>
        <p>{person.role}</p>
        {person.version > 0 ? (
          <span>人物版本 V{person.version} · 乡墅行业 IP</span>
        ) : null}
      </div>
    </header>
  );
}

function IpPanel({ person }: { person: StudioPerson }) {
  const { review, updateData, navigate, notify, patchDraft, user } =
    useStudio();
  const readOnly = user.role === "auditor";
  const [saving, setSaving] = useState(false);
  const [draft, setDraft] = useState(() => ({
    name: person.name,
    role: person.role,
    scope: person.scope,
    audience: person.audience,
    expression: person.expression,
    audience_needs: person.audience_needs ?? "",
    factual_background: person.factual_background ?? "",
    sample_script: person.sample_script ?? "",
    forbidden_claims: person.forbidden_claims ?? "",
  }));
  const update = (key: keyof typeof draft, value: string) =>
    setDraft((current) => ({ ...current, [key]: value }));
  const save = async () => {
    if (readOnly || saving) return;
    if (review) {
      updateData((data) => ({
        ...data,
        people: data.people.map((item) =>
          item.id === person.id ? { ...item, ...draft } : item,
        ),
      }));
      notify("定位草稿已更新");
      return;
    }
    setSaving(true);
    try {
      await updateSimpleCharacterProfile(person.id, {
        display_name: draft.name,
        role: draft.role,
        service_scope: draft.scope,
        target_audience: draft.audience,
        expression_style: draft.expression,
        audience_needs: draft.audience_needs,
        factual_background: draft.factual_background,
        sample_script: draft.sample_script,
        forbidden_claims: draft.forbidden_claims,
      });
      updateData((data) => ({
        ...data,
        people: data.people.map((item) =>
          item.id === person.id ? { ...item, ...draft } : item,
        ),
      }));
      notify("IP 定位已保存。");
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "IP 定位保存失败"));
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="ip-layout">
      <Panel>
        <h2>身份与业务</h2>
        <Hint>
          这些信息长期用于文案二创；本次长度和临时要求在文案工坊设置。
        </Hint>
        <Field label="姓名">
          <input
            disabled={readOnly}
            value={draft.name}
            onChange={(event) => update("name", event.target.value)}
          />
        </Field>
        <Field label="身份">
          <textarea
            rows={3}
            disabled={readOnly}
            value={draft.role}
            onChange={(event) => update("role", event.target.value)}
          />
        </Field>
        <Field label="服务范围">
          <textarea
            rows={3}
            disabled={readOnly}
            value={draft.scope}
            onChange={(event) => update("scope", event.target.value)}
          />
        </Field>
        <h3>目标受众</h3>
        <Field label="目标人群">
          <textarea
            rows={3}
            disabled={readOnly}
            value={draft.audience}
            onChange={(event) => update("audience", event.target.value)}
          />
        </Field>
        <Field label="客户关心的问题">
          <textarea
            aria-label="客户关心的问题"
            rows={3}
            maxLength={600}
            disabled={readOnly}
            value={draft.audience_needs}
            onChange={(event) => update("audience_needs", event.target.value)}
            placeholder="例如：预算如何规划、布局是否实用、施工如何避坑"
          />
        </Field>
        <h3>表达风格</h3>
        <Field label="表达特点">
          <textarea
            rows={3}
            disabled={readOnly}
            value={draft.expression}
            onChange={(event) => update("expression", event.target.value)}
          />
        </Field>
      </Panel>
      <Panel>
        <h2>真实素材与表达边界</h2>
        <Field label="可引用的真实资料">
          <textarea
            aria-label="可引用的真实资料"
            rows={5}
            maxLength={2000}
            disabled={readOnly}
            value={draft.factual_background}
            onChange={(event) =>
              update("factual_background", event.target.value)
            }
            placeholder="填写已核实的服务、资质或案例，注明来源或适用范围。没有资料可以留空。"
          />
        </Field>
        <Field label="代表性口播">
          <textarea
            aria-label="代表性口播"
            rows={5}
            maxLength={2000}
            disabled={readOnly}
            value={draft.sample_script}
            onChange={(event) => update("sample_script", event.target.value)}
            placeholder="粘贴一段你认可的口播，用于参考句式、节奏和用词。不会直接移植其中的经历或数字。"
          />
        </Field>
        <Field label="禁用表达与承诺">
          <textarea
            aria-label="禁用表达与承诺"
            rows={3}
            maxLength={600}
            disabled={readOnly}
            value={draft.forbidden_claims}
            onChange={(event) => update("forbidden_claims", event.target.value)}
            placeholder="例如：不承诺最低价、不保证固定工期、不虚构客户案例"
          />
        </Field>
        <Hint>
          选填。真实资料最多2000字符，代表口播最多2000字符，其余补充项最多600字符。
        </Hint>
        <h2>人物简介预览</h2>
        <p className="ip-preview">
          大家好，我是{draft.name}，一名{draft.role}。我专注于{draft.scope}
          ，服务{draft.audience}，表达风格是{draft.expression}。
        </p>
        <Hint>文案工坊会引用此定位生成更贴合乡墅行业的内容。</Hint>
      </Panel>
      <div className="person-footer">
        <Button
          variant="primary"
          disabled={readOnly || saving}
          onClick={() => void save()}
        >
          {review ? "保存定位草稿" : saving ? "保存中…" : "保存 IP 定位"}
        </Button>
        <Button
          disabled={readOnly}
          variant="outline"
          onClick={() => {
            if (readOnly) return;
            patchDraft({ ipId: person.id });
            navigate("copy", { selectedPersonId: person.id });
          }}
        >
          去文案工坊创作
        </Button>
      </div>
    </div>
  );
}

function PhotosPanel({ person }: { person: StudioPerson }) {
  const {
    openLive,
    navigate,
    patchDraft,
    data,
    notify,
    review,
    updateData,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [loadingMore, setLoadingMore] = useState(false);
  const assets = data.assets.filter(
    (asset) => asset.personId === person.id && asset.kind === "image",
  );
  const scenePage = data.pagination?.scenes?.[person.id];
  const [sceneRequest, setSceneRequest] = useState(0);
  const [expandedPhoto, setExpandedPhoto] = useState<string>();
  const baseAsset =
    (person.sheetId
      ? assets.find((asset) => asset.id === person.sheetId)
      : assets.find((asset) => asset.composite)) ??
    (person.sheetId
      ? {
          id: person.sheetId,
          name: "基础五视图",
          kind: "image" as const,
          group: "人物",
          source: "人物库",
          saved: true,
          composite: true,
        }
      : undefined);
  const refreshScenes = async () => {
    try {
      const result = await loadPersonAssets(person.id);
      updateData((current) => ({
        ...current,
        assets: [
          ...current.assets.filter(
            (asset) =>
              !(
                asset.personId === person.id &&
                asset.source === "人物库场景造型"
              ),
          ),
          ...result.assets,
        ],
        people: current.people.map((entry) =>
          entry.id === person.id
            ? {
                ...entry,
                photoIds: result.assets.map((asset) => asset.id),
                photoCount: result.total,
                sceneLookCount: result.total,
              }
            : entry,
        ),
        pagination: {
          ...current.pagination,
          scenes: {
            ...current.pagination?.scenes,
            [person.id]: { loaded: result.loaded, total: result.total },
          },
        },
        errors: [...current.errors, ...result.errors],
      }));
    } catch (cause) {
      notify(
        customerVisibleErrorMessage(
          cause,
          "场景已生成，读取照片失败，请刷新场景重试。",
        ),
      );
    }
  };
  const loadNextScenes = async () => {
    if (review || loadingMore || !scenePage) return;
    setLoadingMore(true);
    try {
      const result = await loadPersonAssets(person.id, scenePage.loaded);
      updateData((current) => {
        const knownAssets = new Set(current.assets.map((asset) => asset.id));
        const nextAssets = result.assets.filter(
          (asset) => !knownAssets.has(asset.id),
        );
        return {
          ...current,
          assets: [...current.assets, ...nextAssets],
          people: current.people.map((entry) =>
            entry.id === person.id
              ? {
                  ...entry,
                  photoIds: [
                    ...entry.photoIds,
                    ...nextAssets.map((asset) => asset.id),
                  ],
                  photoCount: result.total,
                }
              : entry,
          ),
          errors: [...current.errors, ...result.errors],
          pagination: {
            ...current.pagination,
            scenes: {
              ...current.pagination?.scenes,
              [person.id]: { loaded: result.loaded, total: result.total },
            },
          },
        };
      });
    } catch (cause) {
      notify(customerVisibleErrorMessage(cause, "加载更多场景失败，请重试。"));
    } finally {
      setLoadingMore(false);
    }
  };
  return (
    <div className="photos-panel">
      <div className="panel-heading">
        <div>
          <h2>形象照片</h2>
          <p>五视图是一张合成图；场景形象照按套单独管理。</p>
        </div>
        <div>
          <Button
            disabled={readOnly}
            variant="outline"
            onClick={() => {
              if (readOnly) return;
              openLive("characters", { identityId: person.id, tab: "base" });
            }}
          >
            管理形象照
          </Button>
          <Button
            disabled={readOnly}
            variant="primary"
            onClick={() => {
              if (readOnly) return;
              if (review) {
                notify(
                  "示例模式不提交生成任务，请登录后为当前人物创建场景照。",
                );
                return;
              }
              setSceneRequest((value) => value + 1);
            }}
          >
            AI 生成场景照
          </Button>
        </div>
      </div>
      <Panel>
        <h3>基础五视图 · 1 张合成图</h3>
        {baseAsset ? (
          <button
            type="button"
            className="scene-preview-button"
            aria-label="放大查看基础五视图"
            onClick={() => setExpandedPhoto(baseAsset.id)}
          >
            <Media
              asset={baseAsset}
              alt="五视图合成图"
              className="people-sheet"
            />
          </button>
        ) : (
          <Empty
            title="暂无五视图合成图"
            description="前往人物管理上传照片创建。"
            action={
              <Button
                disabled={readOnly}
                variant="outline"
                onClick={() => {
                  if (readOnly) return;
                  openLive("characters", {
                    identityId: person.id,
                    tab: "base",
                  });
                }}
              >
                打开人物管理
              </Button>
            }
          />
        )}
      </Panel>
      {!review ? (
        <CharacterScenePanel
          key={person.id}
          identityId={person.id}
          displayName={person.name}
          canManage={!readOnly}
          createRequest={sceneRequest}
          hideCreateButton
          showResults={false}
          resultCount={
            scenePage
              ? assets.filter((asset) => !asset.composite).length
              : undefined
          }
          onChanged={() => void refreshScenes()}
          onRefresh={() => void refreshScenes()}
        />
      ) : (
        <h3>场景形象照</h3>
      )}
      {review && assets.filter((asset) => !asset.composite).length === 0 ? (
        <Empty
          title={data.loading ? "正在读取场景形象照…" : "还没有场景形象照"}
          description="生成完成的场景会保存在当前人物下。可点击右上角 AI 生成场景照创建第一套。"
        />
      ) : null}
      <div className="scene-grid">
        {assets
          .filter((asset) => !asset.composite)
          .map((asset) => (
            <Panel key={asset.id}>
              <button
                type="button"
                className="scene-preview-button"
                aria-label={`放大查看${asset.name}`}
                onClick={() => setExpandedPhoto(asset.id)}
              >
                <Media
                  asset={
                    asset.contactSheetId
                      ? {
                          ...asset,
                          url: asset.contactSheetUrl,
                          composite: true,
                        }
                      : asset
                  }
                  alt={asset.name}
                  className={
                    asset.contactSheetId
                      ? "scene-image scene-image--sheet"
                      : "scene-image"
                  }
                />
              </button>
              <strong>{asset.name}</strong>
              {asset.contactSheetId ? (
                <p className="scene-set-label">1 套 · 五视图合成图</p>
              ) : null}
              <div>
                <Button
                  disabled={
                    readOnly ||
                    asset.allowedUses?.includes("first_frame") === false
                  }
                  variant="primary"
                  onClick={() => {
                    if (readOnly) return;
                    patchDraft({ ipId: person.id, imageId: asset.id });
                    navigate("replacement", {
                      selectedPersonId: person.id,
                      selectedAssetId: asset.id,
                    });
                  }}
                >
                  用于人物置换
                </Button>
              </div>
            </Panel>
          ))}
      </div>
      {expandedPhoto ? (
        <StudioDialog
          title={
            assets.find((asset) => asset.id === expandedPhoto)?.name ??
            "场景形象照"
          }
          onClose={() => setExpandedPhoto(undefined)}
        >
          <Media
            asset={(() => {
              const asset =
                assets.find((item) => item.id === expandedPhoto) ??
                (expandedPhoto === baseAsset?.id ? baseAsset : undefined);
              return asset?.contactSheetId
                ? { ...asset, url: asset.contactSheetUrl, composite: true }
                : asset;
            })()}
            alt={
              expandedPhoto === baseAsset?.id
                ? "基础五视图大图"
                : "场景形象大图"
            }
            className="scene-expanded-image"
          />
          <Button
            variant="outline"
            onClick={() => {
              setExpandedPhoto(undefined);
              openLive("characters", {
                identityId: person.id,
                tab: expandedPhoto === baseAsset?.id ? "base" : "scenes",
              });
            }}
          >
            查看单独视角
          </Button>
        </StudioDialog>
      ) : null}
      {scenePage && scenePage.loaded < scenePage.total ? (
        <div className="people-pagination">
          <span>
            已加载 {scenePage.loaded} / {scenePage.total} 套场景
          </span>
          <Button disabled={loadingMore} onClick={() => void loadNextScenes()}>
            {loadingMore ? "正在加载…" : "加载更多场景"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}

function AvatarPanel({ person }: { person: StudioPerson }) {
  const {
    data,
    review,
    navigate,
    patchDraft,
    updateData,
    notify,
    refresh,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [title, setTitle] = useState(`${person.name}视频分身`);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [consentedSourceId, setConsentedSourceId] = useState<string>();
  const [error, setError] = useState<string>();
  const [pendingDelete, setPendingDelete] = useState<StudioAvatar>();
  const [renamingAvatarId, setRenamingAvatarId] = useState<string>();
  const [renameError, setRenameError] = useState<string>();
  const [uploadProgress, setUploadProgress] = useState<number>();
  const [uploadedVideo, setUploadedVideo] = useState<UploadedOralSource>();
  const inputRef = useRef<HTMLInputElement>(null);
  const uploadAbortRef = useRef<AbortController | undefined>(undefined);
  const operationRef = useRef(0);
  const mountedRef = useRef(true);
  const cloneSubmissionRef = useRef<CloneSubmission | undefined>(undefined);
  const nameSearch = useCloneNameSearch({
    search: (query) => listOralAvatars(person.id, query),
    review,
    resetKey: person.id,
  });
  const ready = person.avatars.filter(
    (avatar) =>
      avatar.ready &&
      avatar.origin === "视频制作" &&
      nameSearch.matches(avatar),
  );
  const pending = person.avatars.filter(
    (avatar) => !avatar.ready && nameSearch.matches(avatar),
  );
  const isCurrent = (operation: number) =>
    mountedRef.current && operation === operationRef.current;
  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      operationRef.current += 1;
      uploadAbortRef.current?.abort();
    };
  }, []);
  useOralStatusPolling(
    !review,
    pending
      .filter(
        (avatar) =>
          avatar.submissionState !== "SUBMISSION_UNKNOWN" &&
          (avatar.status === "PENDING" || avatar.status === "RUNNING"),
      )
      .map((avatar) => avatar.id),
    refreshOralAvatar,
    refresh,
    setError,
    "分身状态自动刷新失败",
  );
  const consented = Boolean(
    uploadedVideo && consentedSourceId === uploadedVideo.assetId,
  );
  const cancelUpload = () => {
    operationRef.current += 1;
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = undefined;
    setBusy(false);
    setUploadProgress(undefined);
    if (inputRef.current) inputRef.current.value = "";
  };
  const handleVideoUpload = async (file: File) => {
    if (review || readOnly || busy) return;
    const validationError = validateOralSource(file, "video");
    if (validationError) {
      setError(validationError);
      return;
    }
    const operation = ++operationRef.current;
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    setBusy(true);
    setError(undefined);
    setUploadedVideo(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
    setUploadProgress(0);
    try {
      const uploaded = await uploadOralSource(
        file,
        "口播分身素材",
        (progress) => {
          if (isCurrent(operation)) setUploadProgress(progress);
        },
        controller.signal,
      );
      if (isCurrent(operation)) setUploadedVideo(uploaded);
    } catch (cause) {
      if (isCurrent(operation))
        setError(customerVisibleErrorMessage(cause, "人物视频上传失败"));
    } finally {
      if (isCurrent(operation)) {
        setBusy(false);
        setUploadProgress(undefined);
        uploadAbortRef.current = undefined;
        if (inputRef.current) inputRef.current.value = "";
      }
    }
  };
  const startClone = async () => {
    if (review || readOnly || busy || !uploadedVideo || !consented) return;
    const operation = ++operationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      const cloneTitle = title.trim() || `${person.name}视频分身`;
      const fingerprint = JSON.stringify([
        person.id,
        uploadedVideo.assetId,
        "VIDEO",
        cloneTitle,
      ]);
      let submission = cloneSubmissionRef.current;
      if (!submission || submission.fingerprint !== fingerprint) {
        submission = {
          fingerprint,
          idempotencyKey: createCloneIdempotencyKey("avatar"),
        };
        cloneSubmissionRef.current = submission;
      }
      if (!submission.consentId) {
        const consent = await createOralConsent({
          identityId: person.id,
          sourceAssetId: uploadedVideo.assetId,
          purpose: "AVATAR",
        });
        if (!isCurrent(operation)) return;
        submission.consentId = consent.id;
      }
      await createOralAvatarClone({
        identityId: person.id,
        title: cloneTitle,
        sourceAssetId: uploadedVideo.assetId,
        sourceKind: "VIDEO",
        consentId: submission.consentId,
        idempotencyKey: submission.idempotencyKey,
      });
      if (!isCurrent(operation)) return;
      notify("视频分身已提交，可在本页查看制作状态。");
      setCreating(false);
      setUploadedVideo(undefined);
      setConsentedSourceId(undefined);
      cloneSubmissionRef.current = undefined;
      refresh();
    } catch (cause) {
      if (!isCurrent(operation)) return;
      const message = customerVisibleErrorMessage(cause, "口播分身制作失败");
      setError(message);
      notify(message);
    } finally {
      if (isCurrent(operation)) setBusy(false);
    }
  };
  const refreshClone = async (avatarId: string) => {
    if (busy) return;
    const operation = ++operationRef.current;
    setBusy(true);
    try {
      await refreshOralAvatar(avatarId);
      if (isCurrent(operation)) refresh();
    } catch (cause) {
      if (isCurrent(operation))
        setError(customerVisibleErrorMessage(cause, "口播分身状态刷新失败"));
    } finally {
      if (isCurrent(operation)) setBusy(false);
    }
  };
  const openCreate = () => {
    if (!readOnly) {
      setError(undefined);
      setCreating(true);
    }
  };
  const deleteClone = async (avatar: StudioAvatar) => {
    if (readOnly || busy) return;
    const operation = ++operationRef.current;
    setBusy(true);
    setError(undefined);
    try {
      if (!review) await deleteOralAvatar(avatar.id);
      if (!isCurrent(operation)) return;
      setPendingDelete(undefined);
      if (review) {
        updateData((current) => ({
          ...current,
          people: current.people.map((item) =>
            item.id !== person.id
              ? item
              : {
                  ...item,
                  avatars: item.avatars.filter(
                    (candidate) => candidate.id !== avatar.id,
                  ),
                },
          ),
        }));
      } else {
        refresh();
      }
      notify("口播分身已删除");
    } catch (cause) {
      if (!isCurrent(operation)) return;
      const message = customerVisibleErrorMessage(cause, "删除口播分身失败");
      setError(message);
      notify(message);
    } finally {
      if (isCurrent(operation)) setBusy(false);
    }
  };
  const saveAvatarName = async (title: string) => {
    const avatarId = renamingAvatarId;
    if (!avatarId || readOnly || busy) return;
    setBusy(true);
    setRenameError(undefined);
    try {
      const saved = review
        ? title
        : (await renameOralAvatar(avatarId, title)).title;
      if (!mountedRef.current) return;
      updateData((current) => ({
        ...current,
        people: current.people.map((item) =>
          item.id !== person.id
            ? item
            : {
                ...item,
                avatars: item.avatars.map((candidate) =>
                  candidate.id === avatarId
                    ? { ...candidate, name: saved }
                    : candidate,
                ),
              },
        ),
      }));
      setRenamingAvatarId(undefined);
      nameSearch.rerun();
      notify("口播分身名称已更新");
    } catch (cause) {
      if (!mountedRef.current) return;
      setRenameError(
        customerVisibleErrorMessage(cause, "修改口播分身名称失败"),
      );
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };
  const avatarName = (avatar: StudioAvatar) => (
    <InlineCloneName
      name={avatar.name}
      kindLabel="分身"
      editing={renamingAvatarId === avatar.id}
      busy={busy}
      disabled={readOnly || busy}
      error={renamingAvatarId === avatar.id ? renameError : undefined}
      onStart={() => {
        setRenameError(undefined);
        setRenamingAvatarId(avatar.id);
      }}
      onCancel={() => setRenamingAvatarId(undefined)}
      onSave={(title) => void saveAvatarName(title)}
    />
  );
  const manageVoice = () =>
    navigate("person-voices", {
      selectedPersonId: person.id,
      returnTo: "oral",
    });
  const confirmedVoice = person.voices.find((voice) => voice.confirmed);
  return (
    <div className="oral-library-page">
      <div className="oral-library-main">
        <header className="oral-library-heading">
          <div>
            <h2>
              我的口播分身 <span>{person.name}</span>
            </h2>
            <p>上传一段本人视频创建分身，搭配克隆声音和文案生成口播。</p>
          </div>
          <CloneNameSearch
            label="搜索分身名称"
            value={nameSearch.query}
            onChange={nameSearch.setQuery}
          />
        </header>
        <CloneSearchStatus
          search={nameSearch}
          count={ready.length + pending.length}
          noun="分身"
        />
        <div className="oral-library-cards">
          {ready.map((avatar) => (
            <article className="oral-library-card" key={avatar.id}>
              <div className="oral-library-preview">
                <Media
                  asset={data.assets.find(
                    (asset) => asset.id === avatar.imageId,
                  )}
                  alt={avatar.name}
                  presentation="video"
                />
                {review ? <span className="oral-demo-badge">示例</span> : null}
                <span className="oral-video-badge">
                  <Icon name="video" size={14} />
                  视频分身
                </span>
              </div>
              <div className="oral-library-card-body">
                {avatarName(avatar)}
                <div>
                  <span className="oral-ready">
                    <Icon name="check" size={14} />
                    可用于口播
                  </span>
                  <Button
                    disabled={readOnly}
                    variant="primary"
                    onClick={() => {
                      patchDraft({ ipId: person.id, avatarId: avatar.id });
                      navigate("oral", {
                        selectedPersonId: person.id,
                        returnTo: undefined,
                      });
                    }}
                  >
                    使用此分身
                  </Button>
                  <DeleteIconButton
                    disabled={readOnly || busy}
                    onClick={() => setPendingDelete(avatar)}
                  />
                </div>
              </div>
            </article>
          ))}
          <button
            className="oral-create-tile"
            disabled={readOnly}
            type="button"
            onClick={openCreate}
          >
            <Icon name="plus" size={40} />
            <strong>
              {ready.length ? "创建新的分身" : "创建你的第一个分身"}
            </strong>
            <span>MP4 / MOV · 最大 50 MB</span>
          </button>
        </div>
        {pending.length ? (
          <div className="oral-pending-list">
            {pending.map((avatar) => (
              <article className="oral-pending-card" key={avatar.id}>
                <Icon name="clock" size={20} />
                <div>
                  {avatarName(avatar)}
                  <p>
                    {avatar.submissionState === "SUBMISSION_UNKNOWN"
                      ? "提交结果待人工核对，禁止重复提交"
                      : avatar.error || avatar.duration}
                  </p>
                  {avatar.submissionState !== "SUBMISSION_UNKNOWN" &&
                  (avatar.status === "PENDING" ||
                    avatar.status === "RUNNING") ? (
                    <Button
                      variant="quiet"
                      disabled={busy}
                      onClick={() => void refreshClone(avatar.id)}
                    >
                      刷新制作状态
                    </Button>
                  ) : null}
                </div>
              </article>
            ))}
          </div>
        ) : null}
        {!creating && error ? (
          <p className="oral-error" role="alert">
            {error}
          </p>
        ) : null}
        <footer className="oral-library-footer">
          <Icon name="video" size={20} />
          <div>
            <p>视频分身 + 克隆声音 + 文案 → 数字人口播</p>
            <span>一次创建，可在后续口播中重复使用。</span>
          </div>
        </footer>
      </div>
      <aside className="oral-library-guide">
        <div className="oral-library-actions">
          <Button variant="quiet" onClick={manageVoice}>
            声音档案
            <Icon name="chevron" size={16} />
          </Button>
          <Button variant="primary" disabled={readOnly} onClick={openCreate}>
            <Icon name="upload" size={18} />
            上传视频创建分身
          </Button>
        </div>
        <h3>创建分身，只需一段视频</h3>
        <p>按照以下建议拍摄，帮助生成更自然的数字人口播效果。</p>
        <ul className="oral-shooting-guide">
          {[
            ["person", "单人正面出镜", "请保持本人出镜，面部清晰可见。"],
            ["audio", "嘴部清晰无遮挡", "说话时避免手部或其他物体遮挡嘴部。"],
            [
              "video",
              "画面稳定、光线均匀",
              "建议在光线充足、稳定的环境下拍摄。",
            ],
          ].map(([icon, title, description]) => (
            <li key={title}>
              <span>
                <Icon name={icon} size={24} />
              </span>
              <div>
                <h4>{title}</h4>
                <p>{description}</p>
              </div>
            </li>
          ))}
        </ul>
        <h3>创建流程</h3>
        <ol className="oral-create-steps">
          {[
            ["upload", "上传视频", "上传一段真人出镜的视频"],
            ["pen", "命名并确认授权", "为分身命名并确认使用授权"],
            ["check", "等待分身完成", "系统处理中，完成即可使用"],
          ].map(([icon, title, description]) => (
            <li key={title}>
              <span>
                <Icon name={icon} size={22} />
              </span>
              <strong>{title}</strong>
              <small>{description}</small>
            </li>
          ))}
        </ol>
        <section className="oral-library-voice">
          <h3>我的声音</h3>
          <div>
            <Icon name="audio" size={24} />
            <p>
              <strong>{confirmedVoice?.name ?? "尚未配置克隆声音"}</strong>
              <span>
                {confirmedVoice
                  ? "已试听确认，可用于口播"
                  : "配置后生成与你声音一致的口播内容。"}
              </span>
            </p>
            <Button variant="quiet" onClick={manageVoice}>
              {confirmedVoice ? "管理声音" : "去克隆声音"}
              <Icon name="chevron" size={16} />
            </Button>
          </div>
        </section>
      </aside>
      {creating ? (
        <StudioDialog
          title="创建视频分身"
          onClose={() => {
            if (uploadProgress !== undefined) cancelUpload();
            setCreating(false);
          }}
        >
          <div className="oral-upload-panel">
            <p className="oral-dialog-intro">
              为{person.name}上传真人出镜视频，创建可重复使用的口播分身。
            </p>
            <input
              ref={inputRef}
              accept=".mp4,.mov,video/mp4,video/quicktime"
              aria-label="选择人物视频"
              disabled={readOnly || busy || review}
              hidden
              type="file"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void handleVideoUpload(file);
              }}
            />
            <button
              className={`oral-upload-zone${uploadedVideo ? " is-uploaded" : ""}`}
              type="button"
              disabled={readOnly || busy}
              onClick={() =>
                review
                  ? notify("审核模式保留示例素材，不执行真实上传")
                  : inputRef.current?.click()
              }
            >
              <span className="oral-upload-icon">
                <Icon name={uploadedVideo ? "check" : "upload"} size={28} />
              </span>
              <strong>
                {uploadedVideo ? "视频已上传，准备创建" : "上传你的真人视频"}
              </strong>
              <span>
                {uploadedVideo
                  ? `已上传：${uploadedVideo.fileName}`
                  : "选择电脑中的 MP4 / MOV 视频"}
              </span>
              <small>
                {uploadedVideo ? "点击更换视频" : "单个文件不超过 50 MB"}
              </small>
            </button>
            {uploadProgress !== undefined ? (
              <div className="oral-upload-progress" role="status">
                <span>上传中 {uploadProgress}%</span>
                <progress value={uploadProgress} max={100} />
                <Button variant="quiet" onClick={cancelUpload}>
                  取消上传
                </Button>
              </div>
            ) : null}
            <Field label="分身名称">
              <input
                disabled={readOnly || busy}
                value={title}
                maxLength={60}
                onChange={(event) => {
                  setTitle(event.target.value);
                  cloneSubmissionRef.current = undefined;
                }}
                placeholder="例如：张工 · 设计室讲解"
              />
            </Field>
            <label className="oral-consent">
              <input
                aria-label="确认分身克隆授权"
                checked={consented}
                disabled={review || readOnly || busy || !uploadedVideo}
                type="checkbox"
                onChange={(event) => {
                  cloneSubmissionRef.current = undefined;
                  setConsentedSourceId(
                    event.target.checked ? uploadedVideo?.assetId : undefined,
                  );
                }}
              />
              <span>
                我确认这是本人素材，或已获得用于数字人分身的明确授权。
              </span>
            </label>
            {error ? (
              <p className="oral-error" role="alert">
                {error}
              </p>
            ) : null}
            <Button
              className="oral-submit"
              variant="primary"
              disabled={
                review || readOnly || busy || !uploadedVideo || !consented
              }
              onClick={() => void startClone()}
            >
              {busy ? "处理中…" : "开始制作视频分身"}
            </Button>
            <p className="oral-footnote">
              提交后自动更新制作状态，完成的分身可重复使用。
            </p>
          </div>
        </StudioDialog>
      ) : null}
      {pendingDelete ? (
        <StudioDialog
          title="删除口播分身"
          onClose={() => setPendingDelete(undefined)}
        >
          <div className="oral-upload-panel">
            <p className="oral-dialog-intro">
              确定删除「{pendingDelete.name}
              」吗？删除后将不再出现在分身列表，源视频素材会保留。
            </p>
            {error ? (
              <p className="oral-error" role="alert">
                {error}
              </p>
            ) : null}
            <div className="oral-dialog-actions">
              <Button
                variant="quiet"
                onClick={() => setPendingDelete(undefined)}
              >
                取消
              </Button>
              <Button
                variant="primary"
                disabled={busy}
                onClick={() => void deleteClone(pendingDelete)}
              >
                {busy ? "删除中…" : "确认删除"}
              </Button>
            </div>
          </div>
        </StudioDialog>
      ) : null}
    </div>
  );
}

function VoicePanel({ person }: { person: StudioPerson }) {
  const {
    state,
    data,
    review,
    openPicker,
    navigate,
    patchDraft,
    updateData,
    notify,
    refresh,
    user,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [title, setTitle] = useState(`${person.name}本人音色`);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [consentedSourceId, setConsentedSourceId] = useState<string>();
  const [error, setError] = useState<string>();
  const [pendingDelete, setPendingDelete] = useState<StudioVoice>();
  const [language, setLanguage] = useState<OralVoiceLanguage>(
    DEFAULT_VOICE_LANGUAGE,
  );
  const [tuning, setTuning] = useState<{
    voice: StudioVoice;
    values: OralVoiceSettings;
  }>();
  const [renamingVoiceId, setRenamingVoiceId] = useState<string>();
  const [renameError, setRenameError] = useState<string>();
  const nameSearch = useCloneNameSearch({
    search: (query) => listOralVoices(person.id, query),
    review,
    resetKey: person.id,
  });
  const visibleVoices = person.voices.filter(nameSearch.matches);
  const [uploadProgress, setUploadProgress] = useState<number>();
  const [uploadedAudio, setUploadedAudio] = useState<UploadedOralSource>();
  const uploadInputRef = useRef<HTMLInputElement>(null);
  const uploadAbortRef = useRef<AbortController | undefined>(undefined);
  const uploadOperationRef = useRef(0);
  const personContextRef = useRef(person.id);
  const mountedRef = useRef(true);
  const cloneSubmissionRef = useRef<CloneSubmission | undefined>(undefined);
  const cloneContextRef = useRef(0);
  const sourceAsset = data.assets.find(
    (asset) =>
      asset.id === state.draft.audioId &&
      asset.kind === "audio" &&
      asset.allowedUses?.includes("voice_clone"),
  );
  const selectedSourceAssetId = sourceAsset?.id;
  const previousSourceAssetId = useRef(selectedSourceAssetId);
  useLayoutEffect(() => {
    personContextRef.current = person.id;
    uploadOperationRef.current += 1;
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = undefined;
    setBusy(false);
    setUploadProgress(undefined);
    setUploadedAudio(undefined);
    setConsentedSourceId(undefined);
    setLanguage(DEFAULT_VOICE_LANGUAGE);
    setTuning(undefined);
    cloneSubmissionRef.current = undefined;
  }, [person.id]);
  useLayoutEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      uploadOperationRef.current += 1;
      uploadAbortRef.current?.abort();
      uploadAbortRef.current = undefined;
    };
  }, []);
  useEffect(() => {
    if (previousSourceAssetId.current === selectedSourceAssetId) return;
    previousSourceAssetId.current = selectedSourceAssetId;
    setUploadedAudio(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
  }, [selectedSourceAssetId]);
  const voiceRequests = useRef(new Map<string, number>());
  const refreshVoice = useCallback(
    async (voiceId: string) => {
      const request = (voiceRequests.current.get(voiceId) ?? 0) + 1;
      voiceRequests.current.set(voiceId, request);
      const result = await refreshOralVoice(voiceId);
      const next = await loadStudioVoice(result);
      if (!mountedRef.current || voiceRequests.current.get(voiceId) !== request)
        return;
      updateData((current) => {
        let changed = false;
        const people = current.people.map((item) => {
          if (item.id !== person.id) return item;
          const voices = item.voices.map((existing) => {
            if (
              existing.id !== voiceId ||
              Object.entries(next).every(
                ([key, value]) =>
                  existing[key as keyof typeof existing] === value,
              )
            )
              return existing;
            changed = true;
            return next;
          });
          return changed ? { ...item, voices } : item;
        });
        return changed ? { ...current, people } : current;
      });
    },
    [person.id, updateData],
  );
  useOralStatusPolling(
    !review,
    person.voices
      .filter(
        (voice) =>
          !voice.confirmed &&
          voice.submissionState !== "SUBMISSION_UNKNOWN" &&
          (voice.status === "PENDING" ||
            voice.status === "RUNNING" ||
            (voice.status === "READY" && !voice.url)),
      )
      .map((voice) => voice.id),
    refreshVoice,
    undefined,
    setError,
    "声音状态自动刷新失败",
  );
  const source = uploadedAudio
    ? { id: uploadedAudio.assetId, name: uploadedAudio.fileName }
    : sourceAsset
      ? { id: sourceAsset.id, name: sourceAsset.name }
      : undefined;
  const consented = Boolean(source && consentedSourceId === source.id);
  const voiceSourceId = source?.id;
  useLayoutEffect(() => {
    void voiceSourceId;
    cloneContextRef.current += 1;
    setBusy(false);
  }, [voiceSourceId]);
  const cancelAudioUpload = () => {
    uploadOperationRef.current += 1;
    uploadAbortRef.current?.abort();
    uploadAbortRef.current = undefined;
    setBusy(false);
    setUploadProgress(undefined);
    if (uploadInputRef.current) uploadInputRef.current.value = "";
  };
  const handleAudioUpload = async (file: File) => {
    if (review || readOnly || busy) return;
    const validationError =
      file.size > 20 * 1024 * 1024
        ? "声音克隆样本不能超过 20 MB。"
        : validateOralAudioFile(file, "voice_clone");
    if (validationError) {
      setError(validationError);
      return;
    }
    const operation = ++uploadOperationRef.current;
    const personId = person.id;
    uploadAbortRef.current?.abort();
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    setBusy(true);
    setError(undefined);
    setConsentedSourceId(undefined);
    cloneSubmissionRef.current = undefined;
    setUploadProgress(0);
    try {
      // The server verifies the actual audio track even when the browser lacks its codec.
      const duration = await readAudioDuration(file).catch(() => undefined);
      if (
        !mountedRef.current ||
        personId !== personContextRef.current ||
        operation !== uploadOperationRef.current
      )
        return;
      if (duration !== undefined && (duration < 5 || duration > 180)) {
        setError("声音克隆样本时长必须为 5–180 秒。");
        return;
      }
      const uploaded = await uploadOralAudioMaterial(
        file,
        "voice_clone",
        duration,
        (progress) => {
          if (
            mountedRef.current &&
            personId === personContextRef.current &&
            operation === uploadOperationRef.current
          )
            setUploadProgress(progress);
        },
        controller.signal,
      );
      if (
        !mountedRef.current ||
        personId !== personContextRef.current ||
        operation !== uploadOperationRef.current
      )
        return;
      setUploadedAudio({ assetId: uploaded.id, fileName: file.name });
    } catch (cause) {
      if (
        !mountedRef.current ||
        personId !== personContextRef.current ||
        operation !== uploadOperationRef.current
      )
        return;
      setError(customerVisibleErrorMessage(cause, "声音样本上传失败"));
    } finally {
      if (
        mountedRef.current &&
        personId === personContextRef.current &&
        operation === uploadOperationRef.current
      ) {
        uploadAbortRef.current = undefined;
        setBusy(false);
        setUploadProgress(undefined);
        if (uploadInputRef.current) uploadInputRef.current.value = "";
      }
    }
  };
  const startClone = async () => {
    if (review || readOnly || busy || !source || !consented) return;
    const context = cloneContextRef.current;
    const isCurrent = () =>
      mountedRef.current && context === cloneContextRef.current;
    setBusy(true);
    setError(undefined);
    try {
      const cloneTitle = title.trim() || `${person.name}本人音色`;
      const fingerprint = JSON.stringify([
        person.id,
        source.id,
        cloneTitle,
        language,
      ]);
      let submission = cloneSubmissionRef.current;
      if (!submission || submission.fingerprint !== fingerprint) {
        submission = {
          fingerprint,
          idempotencyKey: createCloneIdempotencyKey("voice"),
        };
        cloneSubmissionRef.current = submission;
      }
      if (!submission.consentId) {
        const consent = await createOralConsent({
          identityId: person.id,
          sourceAssetId: source.id,
          purpose: "VOICE",
        });
        if (!isCurrent()) return;
        submission.consentId = consent.id;
      }
      await createOralVoiceClone({
        identityId: person.id,
        title: cloneTitle,
        sourceAssetId: source.id,
        consentId: submission.consentId,
        idempotencyKey: submission.idempotencyKey,
        language,
      });
      if (!isCurrent()) return;
      setCreating(false);
      setUploadedAudio(undefined);
      setConsentedSourceId(undefined);
      setLanguage(DEFAULT_VOICE_LANGUAGE);
      notify("声音克隆已提交，通常在 10 分钟内完成。");
      refresh();
    } catch (cause) {
      if (!isCurrent()) return;
      const message = customerVisibleErrorMessage(cause, "声音克隆失败");
      setError(message);
      notify(message);
    } finally {
      if (isCurrent()) setBusy(false);
    }
  };
  const refreshClone = async (voiceId: string) => {
    if (busy) return;
    setBusy(true);
    try {
      await refreshVoice(voiceId);
    } catch (cause) {
      if (!mountedRef.current) return;
      notify(customerVisibleErrorMessage(cause, "声音克隆状态刷新失败"));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };
  const confirmVoice = async (voiceId: string) => {
    if (readOnly || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      if (!review) await confirmOralVoice(voiceId);
      if (!mountedRef.current) return;
      if (review) {
        updateData((data) => ({
          ...data,
          people: data.people.map((item) =>
            item.id !== person.id
              ? item
              : {
                  ...item,
                  voices: item.voices.map((candidate) =>
                    candidate.id === voiceId
                      ? { ...candidate, confirmed: true }
                      : candidate,
                  ),
                },
          ),
        }));
      } else {
        refresh();
      }
      patchDraft({ ipId: person.id, voiceId });
      navigate(state.returnTo ?? "oral", {
        selectedPersonId: person.id,
        returnTo: undefined,
      });
    } catch (cause) {
      if (!mountedRef.current) return;
      const message = customerVisibleErrorMessage(cause, "声音确认失败");
      setError(message);
      notify(message);
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };
  const openTuning = (voice: StudioVoice) => {
    setError(undefined);
    setTuning({
      voice,
      values: {
        speechRate: voice.speechRate ?? DEFAULT_VOICE_SETTINGS.speechRate,
        volume: voice.volume ?? DEFAULT_VOICE_SETTINGS.volume,
        pitch: voice.pitch ?? DEFAULT_VOICE_SETTINGS.pitch,
      },
    });
  };
  const saveVoiceSettings = async () => {
    if (!tuning || readOnly || busy) return;
    const { voice, values } = tuning;
    setBusy(true);
    setError(undefined);
    try {
      let saved: OralVoiceSettings = values;
      if (!review) {
        const record = await updateOralVoiceSettings(voice.id, values);
        saved = {
          speechRate: record.speech_rate,
          volume: record.volume,
          pitch: record.pitch,
        };
      }
      if (!mountedRef.current) return;
      updateData((data) => ({
        ...data,
        people: data.people.map((item) =>
          item.id !== person.id
            ? item
            : {
                ...item,
                voices: item.voices.map((candidate) =>
                  candidate.id === voice.id
                    ? { ...candidate, ...saved }
                    : candidate,
                ),
              },
        ),
      }));
      setTuning(undefined);
      notify("声音已调整，之后生成的口播都会使用新设置。");
    } catch (cause) {
      if (!mountedRef.current) return;
      setError(customerVisibleErrorMessage(cause, "保存声音参数失败"));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };
  const saveVoiceName = async (title: string) => {
    const voiceId = renamingVoiceId;
    if (!voiceId || readOnly || busy) return;
    setBusy(true);
    setRenameError(undefined);
    try {
      const saved = review
        ? title
        : (await renameOralVoice(voiceId, title)).title;
      if (!mountedRef.current) return;
      updateData((data) => ({
        ...data,
        people: data.people.map((item) =>
          item.id !== person.id
            ? item
            : {
                ...item,
                voices: item.voices.map((candidate) =>
                  candidate.id === voiceId
                    ? { ...candidate, name: saved }
                    : candidate,
                ),
              },
        ),
      }));
      setRenamingVoiceId(undefined);
      nameSearch.rerun();
      notify("声音名称已更新");
    } catch (cause) {
      if (!mountedRef.current) return;
      setRenameError(customerVisibleErrorMessage(cause, "修改声音名称失败"));
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };
  const deleteClone = async (voice: StudioVoice) => {
    if (readOnly || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      if (!review) await deleteOralVoice(voice.id);
      if (!mountedRef.current) return;
      setPendingDelete(undefined);
      if (review) {
        updateData((data) => ({
          ...data,
          people: data.people.map((item) =>
            item.id !== person.id
              ? item
              : {
                  ...item,
                  voices: item.voices.filter(
                    (candidate) => candidate.id !== voice.id,
                  ),
                },
          ),
        }));
      } else {
        refresh();
      }
      notify("声音已删除");
    } catch (cause) {
      if (!mountedRef.current) return;
      const message = customerVisibleErrorMessage(cause, "删除声音失败");
      setError(message);
      notify(message);
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };
  return (
    <div className="oral-voice-page">
      <header className="oral-voice-heading">
        <div>
          <h2>
            我的声音 <span>{person.name}</span>
          </h2>
          <p>克隆本人音色，试听确认后即可用于数字人口播。</p>
        </div>
        <div className="oral-voice-heading__actions">
          <CloneNameSearch
            label="搜索声音名称"
            value={nameSearch.query}
            onChange={nameSearch.setQuery}
          />
          <Button
            variant="primary"
            disabled={readOnly}
            onClick={() => setCreating(true)}
          >
            <Icon name="plus" size={18} />
            克隆新声音
          </Button>
        </div>
      </header>
      <div className="oral-voice-grid">
        <section className="oral-voice-list">
          <CloneSearchStatus
            search={nameSearch}
            count={visibleVoices.length}
            noun="声音"
          />
          {visibleVoices.map((voice) => {
            const ready = voice.status === "READY" || (review && !voice.status);
            const cloning =
              voice.status === "PENDING" || voice.status === "RUNNING";
            return (
              <article
                className={
                  voice.confirmed ? "voice-card is-confirmed" : "voice-card"
                }
                key={voice.id}
              >
                <div className="voice-card__info">
                  <InlineCloneName
                    name={voice.name}
                    kindLabel="声音"
                    editing={renamingVoiceId === voice.id}
                    busy={busy}
                    disabled={readOnly || busy}
                    error={
                      renamingVoiceId === voice.id ? renameError : undefined
                    }
                    onStart={() => {
                      setRenameError(undefined);
                      setRenamingVoiceId(voice.id);
                    }}
                    onCancel={() => setRenamingVoiceId(undefined)}
                    onSave={(title) => void saveVoiceName(title)}
                  />
                  <p>
                    {voice.confirmed
                      ? "已确认，可用于文案口播"
                      : voice.submissionState === "SUBMISSION_UNKNOWN"
                        ? "提交结果待人工核对，禁止重复提交"
                        : voice.status === "FAILED"
                          ? voice.error || "克隆失败，请更换样本重试"
                          : cloning
                            ? "声音克隆中，暂不可选用"
                            : voice.status === "READY" && !voice.url
                              ? "试听样例归档中"
                              : "待试听确认，暂不可选用"}
                  </p>
                  {voice.language ? (
                    <p className="voice-card__meta">
                      {voiceLanguageLabel(voice.language)} ·{" "}
                      {formatVoiceSettings({
                        speechRate:
                          voice.speechRate ?? DEFAULT_VOICE_SETTINGS.speechRate,
                        volume: voice.volume ?? DEFAULT_VOICE_SETTINGS.volume,
                        pitch: voice.pitch ?? DEFAULT_VOICE_SETTINGS.pitch,
                      })}
                    </p>
                  ) : null}
                </div>
                <div className="voice-card__actions">
                  <div className="voice-card__preview">
                    {voice.url &&
                    (voice.confirmed || voice.status === "READY") ? (
                      <audio
                        controls
                        preload="none"
                        src={voice.url}
                        aria-label={`${voice.name}试听`}
                      >
                        <track kind="captions" label="声音样本" />
                      </audio>
                    ) : (
                      <Button
                        variant="outline"
                        disabled
                        aria-label={`${voice.name}试听尚未就绪`}
                      >
                        试听准备中
                      </Button>
                    )}
                  </div>
                  <div className="voice-card__action">
                    {voice.confirmed ? (
                      <Button
                        disabled={readOnly}
                        variant="primary"
                        onClick={() => {
                          if (readOnly) return;
                          patchDraft({ ipId: person.id, voiceId: voice.id });
                          navigate(state.returnTo ?? "oral", {
                            selectedPersonId: person.id,
                            returnTo: undefined,
                          });
                        }}
                      >
                        使用此声音
                      </Button>
                    ) : voice.submissionState ===
                      "SUBMISSION_UNKNOWN" ? null : cloning ? (
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={() => void refreshClone(voice.id)}
                      >
                        刷新克隆状态
                      </Button>
                    ) : (voice.status === "READY" && Boolean(voice.url)) ||
                      (review && !voice.status) ? (
                      <Button
                        variant="primary"
                        disabled={readOnly || busy}
                        onClick={() => void confirmVoice(voice.id)}
                      >
                        确认使用此声音
                      </Button>
                    ) : null}
                    {ready ? (
                      <Button
                        variant="outline"
                        disabled={readOnly || busy}
                        aria-label={`调整${voice.name}的声音`}
                        onClick={() => openTuning(voice)}
                      >
                        调整声音
                      </Button>
                    ) : null}
                    {cloning ? null : (
                      <DeleteIconButton
                        disabled={readOnly || busy}
                        onClick={() => setPendingDelete(voice)}
                      />
                    )}
                  </div>
                </div>
              </article>
            );
          })}
          {person.voices.length === 0 ? (
            <Empty
              title="暂无声音档案"
              description="上传一段本人的清晰录音，就能克隆出你的声音。"
            />
          ) : null}
        </section>
        <aside className="oral-voice-guide">
          <h3>三步用上本人声音</h3>
          <ol className="oral-steps-guide">
            <li>
              <span>1</span>
              <div>
                <h4>上传一段清晰录音</h4>
                <p>5 秒到 3 分钟，安静环境里本人说话即可。</p>
              </div>
            </li>
            <li>
              <span>2</span>
              <div>
                <h4>试听后确认</h4>
                <p>听起来像本人，就点「确认使用此声音」。</p>
              </div>
            </li>
            <li>
              <span>3</span>
              <div>
                <h4>按需调整声音</h4>
                <p>觉得太快、太轻？点「调整声音」选一个档位就好。</p>
              </div>
            </li>
          </ol>
          <Button
            variant="outline"
            onClick={() =>
              navigate("person-avatars", {
                selectedPersonId: person.id,
                returnTo: "oral",
              })
            }
          >
            前往口播分身
            <Icon name="arrow" size={16} />
          </Button>
        </aside>
      </div>
      {!creating && !tuning && error ? (
        <p className="oral-error" role="alert">
          {error}
        </p>
      ) : null}
      {creating ? (
        <StudioDialog
          title="克隆新声音"
          onClose={() => {
            if (uploadProgress !== undefined) cancelAudioUpload();
            setCreating(false);
          }}
        >
          <div className="oral-upload-panel oral-clone-steps">
            <section className="oral-clone-step">
              <h3>
                <span>1</span>上传录音
              </h3>
              {review ? null : (
                <Field label="上传声音样本">
                  <input
                    ref={uploadInputRef}
                    accept={VOICE_CLONE_ACCEPT}
                    aria-label="选择声音样本"
                    disabled={readOnly || busy}
                    hidden
                    type="file"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void handleAudioUpload(file);
                    }}
                  />
                </Field>
              )}
              <button
                type="button"
                className="oral-upload-zone"
                disabled={readOnly || busy}
                onClick={() =>
                  review
                    ? notify("审核模式不执行真实上传")
                    : uploadInputRef.current?.click()
                }
              >
                <span className="oral-upload-icon">
                  <Icon name="upload" size={28} />
                </span>
                <strong>上传录音</strong>
                <span>5 秒到 3 分钟 · MP3、M4A、WAV 等 · 最大 20 MB</span>
              </button>
              <Button
                disabled={readOnly}
                variant="quiet"
                onClick={() => {
                  if (readOnly) return;
                  patchDraft({ ipId: person.id });
                  openPicker("voice-audio");
                }}
              >
                从素材选择声音样本
              </Button>
              <p className="oral-clone-source">
                {uploadedAudio
                  ? `已上传：${uploadedAudio.fileName}`
                  : sourceAsset
                    ? `已选：${sourceAsset.name}`
                    : "请先选择声音样本。"}
              </p>
              {uploadProgress !== undefined ? (
                <div className="oral-clone-progress">
                  <span>上传中 {uploadProgress}%</span>
                  <Button variant="quiet" onClick={cancelAudioUpload}>
                    取消上传
                  </Button>
                </div>
              ) : null}
            </section>
            <section className="oral-clone-step">
              <h3>
                <span>2</span>填写信息
              </h3>
              <Field label="声音名称">
                <input
                  disabled={readOnly || busy}
                  maxLength={60}
                  value={title}
                  onChange={(event) => {
                    setTitle(event.target.value);
                    cloneSubmissionRef.current = undefined;
                  }}
                  placeholder="例如：张工本人音色 V2"
                />
              </Field>
              <Field label="录音里说的是">
                <select
                  disabled={readOnly || busy}
                  value={language}
                  onChange={(event) =>
                    setLanguage(event.target.value as OralVoiceLanguage)
                  }
                >
                  {VOICE_LANGUAGE_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
              <p className="oral-field-note">
                按录音实际说的语言选，不确定就选普通话。
              </p>
            </section>
            <section className="oral-clone-step">
              <h3>
                <span>3</span>确认授权
              </h3>
              <label className="oral-consent">
                <input
                  aria-label="确认声音克隆授权"
                  checked={consented}
                  disabled={review || readOnly || busy}
                  type="checkbox"
                  onChange={(event) => {
                    cloneSubmissionRef.current = undefined;
                    setConsentedSourceId(
                      event.target.checked ? source?.id : undefined,
                    );
                  }}
                />
                这是我本人的声音，或已获得声音主人的明确授权。
              </label>
            </section>
            {error ? <p role="alert">{error}</p> : null}
            <Button
              variant="primary"
              disabled={review || readOnly || busy || !source || !consented}
              onClick={() => void startClone()}
            >
              {busy ? "提交中…" : "开始克隆"}
            </Button>
            <p className="oral-field-note oral-clone-footnote">
              通常 10 分钟内完成，完成后在列表里直接试听。
            </p>
          </div>
        </StudioDialog>
      ) : null}
      {tuning ? (
        <StudioDialog
          title="调整声音"
          onClose={() => {
            if (!busy) setTuning(undefined);
          }}
        >
          <div className="oral-upload-panel">
            <p className="oral-dialog-intro">
              {tuning.voice.name} ·
              选一个档位即可，保存后新生成的口播都会使用；已生成的视频和试听样例不会改变。
            </p>
            {VOICE_SETTING_FIELDS.map((field) => {
              const inputId = `voice-setting-${field.key}`;
              const value = tuning.values[field.key];
              const described = describeVoiceSetting(field.key, value);
              const setValue = (next: number) =>
                setTuning((current) =>
                  current
                    ? {
                        ...current,
                        values: {
                          ...current.values,
                          [field.key]: roundVoiceSetting(next),
                        },
                      }
                    : current,
                );
              return (
                <fieldset className="voice-setting" key={field.key}>
                  <legend className="voice-setting__head">
                    <span>{field.label}</span>
                    <output htmlFor={inputId}>
                      {described === value.toFixed(1)
                        ? `自定义 ${described}`
                        : `${described} ${value.toFixed(1)}`}
                    </output>
                  </legend>
                  <p className="voice-setting__hint">{field.description}</p>
                  <div className="voice-setting__presets">
                    {field.presets.map((preset) => {
                      const active =
                        roundVoiceSetting(preset.value) ===
                        roundVoiceSetting(value);
                      return (
                        <button
                          type="button"
                          key={preset.label}
                          className={
                            active ? "voice-preset is-active" : "voice-preset"
                          }
                          aria-label={`${field.label}${preset.label}`}
                          aria-pressed={active}
                          disabled={readOnly || busy}
                          onClick={() => setValue(preset.value)}
                        >
                          {preset.label}
                        </button>
                      );
                    })}
                  </div>
                  <div className="voice-setting__fine">
                    <label htmlFor={inputId}>微调</label>
                    <input
                      id={inputId}
                      aria-label={`${field.label}微调`}
                      type="range"
                      min={field.min}
                      max={field.max}
                      step={VOICE_SETTING_STEP}
                      value={value}
                      disabled={readOnly || busy}
                      onChange={(event) => setValue(Number(event.target.value))}
                    />
                  </div>
                </fieldset>
              );
            })}
            {error ? (
              <p className="oral-error" role="alert">
                {error}
              </p>
            ) : null}
            <div className="oral-dialog-actions voice-setting__actions">
              <Button
                variant="quiet"
                disabled={readOnly || busy}
                onClick={() =>
                  setTuning((current) =>
                    current
                      ? { ...current, values: { ...DEFAULT_VOICE_SETTINGS } }
                      : current,
                  )
                }
              >
                全部恢复标准
              </Button>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => setTuning(undefined)}
              >
                取消
              </Button>
              <Button
                variant="primary"
                disabled={readOnly || busy}
                onClick={() => void saveVoiceSettings()}
              >
                {busy ? "保存中…" : "保存"}
              </Button>
            </div>
          </div>
        </StudioDialog>
      ) : null}
      {pendingDelete ? (
        <StudioDialog
          title="删除声音"
          onClose={() => setPendingDelete(undefined)}
        >
          <div className="oral-upload-panel">
            <p className="oral-dialog-intro">
              确定删除「{pendingDelete.name}
              」吗？删除后将不再出现在声音列表，声音样本素材会保留。
            </p>
            {error ? (
              <p className="oral-error" role="alert">
                {error}
              </p>
            ) : null}
            <div className="oral-dialog-actions">
              <Button
                variant="quiet"
                onClick={() => setPendingDelete(undefined)}
              >
                取消
              </Button>
              <Button
                variant="primary"
                disabled={busy}
                onClick={() => void deleteClone(pendingDelete)}
              >
                {busy ? "删除中…" : "确认删除"}
              </Button>
            </div>
          </div>
        </StudioDialog>
      ) : null}
    </div>
  );
}
