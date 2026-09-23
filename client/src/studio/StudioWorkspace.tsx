import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  createIndependentVideoTask,
  createOralTask,
  customerGetWallet,
  customerVisibleErrorMessage,
  defaultBatchProvider,
  type GenerationBatch,
  type GenerationPriceQuote,
  getAssetDownloadUrl,
  getGenerationBatch,
  getGenerationPriceQuote,
  getIndependentCapabilities,
  getOralPrice,
  getWallet,
  type IndependentCapabilities,
  listMaterials,
  type Project,
} from "../api";
import { BrandIdentity } from "../BrandIdentity";
import { CustomerCenterPage } from "../customer/CustomerCenterPage";
import { isInsufficientCredits } from "../insufficientCredits";
import {
  discountSourceLabel,
  formatDiscountZhe,
} from "../rechargePackageDisplay";
import { SettingsPanel } from "../SettingsPanel";
import type { WorkspaceShellProps } from "../workspace-shell";
import { AnalyticsPage } from "./AnalyticsPage";
import {
  MaterialsPage,
  PublishPage,
  ViralDetailPage,
  ViralPage,
} from "./ContentPages";
import {
  CopyPage,
  normalizeCustomerDuration,
  OralPage,
  ReplacementPage,
  ReplicaPage,
  VideoPage,
} from "./CreationPages";
import { createCloudDraftQueue } from "./cloudDraftQueue";
import { StudioContext, useStudio } from "./context";
import { clearCreationDraftResidue } from "./draftResidue";
import { LiveWorkspacePanel } from "./LiveWorkspacePanel";
import {
  awaitScriptFromAudioTask,
  type CloudDraftRestore,
  discardCloudDraft,
  extractScriptFromUpload as extractScriptFromUploadLive,
  loadCloudDraft,
  loadDraftMaterials,
  loadLatestScriptFromUpload,
  loadPersonAssets,
  loadProjectDraft,
  loadSavedScriptList,
  loadStudioData,
  loadViralVideos,
  persistCloudDraft,
  persistSavedScript,
  publishScriptVersion,
  reloadStats,
  reloadTasks,
  sameTasks,
  startViralCopyExtraction,
  studioAssetFromMaterial,
} from "./live";
import {
  ProfilePage,
  type StudioAccountSummary,
  TaskDetailPage,
  TasksPage,
  WorkbenchPage,
} from "./MainPages";
import { PeoplePage, PersonPage } from "./PeoplePages";
import { constrainReferenceVideoPrompt } from "./referencePrompt";
import {
  buildOralInput,
  createDraft,
  createState,
  DEFAULT_MAX_REFERENCE_AUDIOS,
  DEFAULT_MAX_REFERENCE_IMAGES,
  DEFAULT_MAX_REFERENCE_VIDEOS,
  hasCopyResult,
  isReferenceAsset,
  MAX_REFERENCE_MEDIA_SECONDS,
  mergeStudioAssets,
  navigateStudioState,
  pageTitles,
  patchStudioDraft,
  resolveSubmittedRatio,
  resolveVideoMode,
  studioHashForState,
  studioRouteFromHash,
  validateReferences,
  withImportedProject,
} from "./state";
import type {
  LivePanel,
  PickerKind,
  StudioAsset,
  StudioContextValue,
  StudioData,
  StudioDraft,
  StudioPage,
  StudioState,
  StudioTask,
} from "./types";
import { Button, Empty, Hint, Icon, Media, StudioDialog } from "./ui";
import { WorkspaceNotifications } from "./WorkspaceNotifications";
import { WorkspaceSearch } from "./WorkspaceSearch";
import "./studio.css";

type Props = WorkspaceShellProps & {
  reviewData?: StudioData;
  initialState?: StudioState;
};

type QuoteStatus = "idle" | "loading" | "ready" | "error";

type GenerationQuoteInput = {
  resolution: "768P" | "2K";
  duration_seconds: number;
  quantity: number;
};

type SubmissionEnvelope = { fingerprint: string; key: string };
type WalletSummary = Pick<
  StudioAccountSummary,
  "walletStatus" | "availableCredits"
>;

// LEFTOVER-ON-OPEN：太旧的草稿连问都不问——上个月的半截活儿弹出来只是噪音。
// 超时只是不再提示，绝不静默删除：那是用户的内容，删除必须由用户点。
const DRAFT_PROMPT_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

/** 草稿是否新到值得打断用户。时间戳解析不出来时按「值得」处理：
 * 宁可多问一次，也不要把还热乎的草稿悄悄藏起来。 */
function shouldPromptRestore(restore: CloudDraftRestore): boolean {
  const savedAt = Date.parse(restore.updatedAt);
  if (Number.isNaN(savedAt)) return true;
  return Date.now() - savedAt <= DRAFT_PROMPT_MAX_AGE_MS;
}

function formatDraftSavedAt(updatedAt: string): string {
  const savedAt = Date.parse(updatedAt);
  if (Number.isNaN(savedAt)) return "";
  return new Date(savedAt).toLocaleString("zh-CN", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// 侧边栏折叠是设备级偏好：仅存本地，不上服务端。
const SIDEBAR_COLLAPSED_KEY = "studio.sidebar.collapsed";

function readSidebarCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

function persistSidebarCollapsed(collapsed: boolean) {
  try {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    // 存储不可用（隐私模式等）时静默降级为会话内状态
  }
}

function quoteMatchesInput(
  quote: GenerationPriceQuote | null,
  input: GenerationQuoteInput,
): quote is GenerationPriceQuote {
  return Boolean(
    quote &&
      quote.resolution === input.resolution &&
      quote.duration_seconds === input.duration_seconds &&
      quote.quantity === input.quantity &&
      quote.estimated_seconds === input.duration_seconds * input.quantity,
  );
}

function videoQuoteInput(draft: StudioDraft): GenerationQuoteInput {
  return {
    resolution: draft.resolution === "2K" ? "2K" : "768P",
    // P1-4：非法时长钳位（与复刻路径同口径），不再静默回落 8 秒——
    // 报价与提交同源归一，用户看到的时长即实际生成时长。
    duration_seconds: normalizeCustomerDuration(draft.duration),
    quantity: draft.count === 2 || draft.count === 4 ? draft.count : 1,
  };
}

const emptyData: StudioData = {
  people: [],
  assets: [],
  materials: [],
  videos: [],
  tasks: [],
  projects: [],
  errors: [],
  loading: true,
  stats: null,
  analytics7: null,
  analytics30: null,
};
const TASKS_POLL_INTERVAL_MS = 20_000;
const ORAL_SUBTITLE_PRESET = {
  st_show: true,
  st_font_size: 30,
  st_primary_color: "0xFFFFFF",
  st_outline_color: "0x000000",
};

function savedDraftScope(accountId: string, draft: StudioDraft) {
  return JSON.stringify([accountId, draft]);
}
const creationPages = new Set<StudioPage>([
  "replica",
  "replacement",
  "video",
  "reference",
  "oral",
  "oral-audio",
]);
export const navGroups: {
  label?: string;
  pages: { id: StudioPage; title: string; icon: string }[];
}[] = [
  {
    pages: [
      { id: "workbench", title: "工作台", icon: "home" },
      { id: "tasks", title: "任务中心", icon: "tasks" },
    ],
  },
  {
    label: "创作",
    pages: [
      { id: "viral", title: "爆款视频", icon: "fire" },
      { id: "copy", title: "文案工坊", icon: "pen" },
      { id: "replica", title: "视频创作", icon: "video" },
    ],
  },
  {
    label: "资产",
    pages: [
      { id: "people", title: "人物库", icon: "person" },
      { id: "materials", title: "素材库", icon: "folder" },
    ],
  },
  {
    label: "分发",
    pages: [
      { id: "publishing", title: "发布管理", icon: "upload" },
      { id: "analytics", title: "数据看板", icon: "chart" },
    ],
  },
];

function WorkspaceUserAvatar({
  currentUser,
  review,
}: {
  currentUser: Props["currentUser"];
  review: boolean;
}) {
  if (review) {
    return <img className="studio-user-avatar" src="/studio/li.png" alt="" />;
  }
  return (
    <span className="studio-user-initial">
      {currentUser.display_name?.slice(0, 1) ||
        currentUser.username?.slice(0, 1) ||
        "我"}
    </span>
  );
}

export function StudioWorkspace({
  currentUser,
  customerAccount,
  customerWallet,
  reviewData,
  initialState,
}: Props) {
  // Profile polling can return a new object with identical identity fields.
  // Keep those refreshes from replacing assets underneath playing media.
  const workspaceUser = useMemo(
    () => ({
      id: currentUser.id,
      username: currentUser.username,
      display_name: currentUser.display_name,
      role: currentUser.role,
    }),
    [
      currentUser.id,
      currentUser.username,
      currentUser.display_name,
      currentUser.role,
    ],
  );
  // Review is an explicit development entry; failed requests never enable it.
  const review = Boolean(import.meta.env.DEV && reviewData);
  const [state, setState] = useState<StudioState>(() => {
    if (initialState) return initialState;
    const route = studioRouteFromHash(window.location.hash);
    return { ...createState(route.page), ...route };
  });
  const studioPageRef = useRef(state.page);
  studioPageRef.current = state.page;
  const [data, setData] = useState<StudioData>(() =>
    review && reviewData ? reviewData : emptyData,
  );
  const [revision, setRevision] = useState(0);
  const [notice, setNotice] = useState("");
  const noticeTimerRef = useRef<number>(undefined);
  const [picker, setPicker] = useState<PickerKind>();
  const [livePanel, setLivePanel] = useState<LivePanel>();
  const [characterTarget, setCharacterTarget] = useState<{
    identityId: string;
    tab: "base" | "scenes";
  }>();
  const [handoffBatch, setHandoffBatch] = useState<GenerationBatch | null>(
    null,
  );
  const [generation, setGeneration] = useState<StudioTask["type"]>();
  const generationRef = useRef<StudioTask["type"] | undefined>(undefined);
  const generationDialogRevisionRef = useRef(0);
  const mountedRef = useRef(true);
  const [newCreation, setNewCreation] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButtonRef = useRef<HTMLButtonElement>(null);
  const closeMenu = () => {
    setMenuOpen(false);
    menuButtonRef.current?.focus();
  };
  const [sidebarCollapsed, setSidebarCollapsed] =
    useState(readSidebarCollapsed);
  const toggleSidebarCollapsed = () => {
    const next = !sidebarCollapsed;
    persistSidebarCollapsed(next);
    setSidebarCollapsed(next);
  };
  const [search, setSearch] = useState("");
  const [showSearch, setShowSearch] = useState(false);
  const [walletRevision, setWalletRevision] = useState(0);
  const walletRequestRef = useRef<{
    userId: string;
    revision: number;
  } | null>(null);
  const walletStore = customerAccount?.store ?? customerWallet?.store;
  const onWalletSessionExpired =
    customerAccount?.onSessionExpired ?? customerWallet?.onSessionExpired;
  const [walletSummary, setWalletSummary] = useState<WalletSummary>(() => ({
    walletStatus: review
      ? "ready"
      : walletStore || currentUser.role !== "customer"
        ? "loading"
        : "unknown",
    availableCredits: review ? 2680 : null,
  }));
  const retryWallet = useCallback(
    () => setWalletRevision((value) => value + 1),
    [],
  );
  const accountSummary: StudioAccountSummary = {
    ...walletSummary,
    retryWallet,
    retryProfile: customerAccount
      ? () => void customerAccount.onRefreshProfile()
      : undefined,
    profile: customerAccount?.profile ?? null,
    profileLoadError: customerAccount?.profileLoadError ?? "",
  };
  const [oralBudget, setOralBudget] = useState<{
    seconds: number;
    credits: number;
  }>();
  const [oralPriceFen, setOralPriceFen] = useState<number | null>(null);
  const [oralPriceCredits, setOralPriceCredits] = useState<number | null>(null);
  const [oralQuoteStatus, setOralQuoteStatus] = useState<QuoteStatus>("idle");
  const [oralQuoteError, setOralQuoteError] = useState("");
  const [oralQuoteRevision, setOralQuoteRevision] = useState(0);
  const [oralSubmitting, setOralSubmitting] = useState(false);
  // ---- 视频生成（C2 独立创作）----
  const [videoCapabilities, setVideoCapabilities] =
    useState<IndependentCapabilities>();
  const [videoCapabilitiesStatus, setVideoCapabilitiesStatus] = useState<
    "loading" | "ready" | "error"
  >(review ? "ready" : "loading");
  const [videoCapabilitiesRevision, setVideoCapabilitiesRevision] = useState(0);
  const [referenceAssetsPending, setReferenceAssetsPending] = useState(false);
  const [referenceAssetsError, setReferenceAssetsError] = useState(false);
  const [videoQuote, setVideoQuote] = useState<GenerationPriceQuote | null>(
    null,
  );
  const [videoQuoteStatus, setVideoQuoteStatus] = useState<QuoteStatus>("idle");
  const [videoQuoteError, setVideoQuoteError] = useState("");
  const [videoQuoteRevision, setVideoQuoteRevision] = useState(0);
  const [videoSubmitting, setVideoSubmitting] = useState(false);
  const [videoSubmitError, setVideoSubmitError] = useState("");
  const [videoSubmitRejected, setVideoSubmitRejected] = useState(false);
  const operationRef = useRef(0);
  const loadedPeopleRef = useRef(new Set<string>());
  const restoredAssetsRef = useRef<StudioAsset[]>([]);
  const oralSubmittingRef = useRef(false);
  const videoSubmittingRef = useRef(false);
  const oralSubmitAttemptRef = useRef(0);
  const videoSubmitAttemptRef = useRef(0);
  const oralSubmissionRef = useRef<SubmissionEnvelope | null>(null);
  const videoSubmissionRef = useRef<SubmissionEnvelope | null>(null);
  const currentUserRoleRef = useRef(currentUser.role);
  const permissionGenerationRef = useRef(0);
  // LEFTOVER-ON-OPEN：读回来但还没回填的上次草稿，等用户决定恢复还是放弃。
  const [pendingRestore, setPendingRestore] =
    useState<CloudDraftRestore | null>(null);
  const [discardBusy, setDiscardBusy] = useState(false);
  const [restoreError, setRestoreError] = useState("");
  if (currentUserRoleRef.current !== currentUser.role) {
    currentUserRoleRef.current = currentUser.role;
    permissionGenerationRef.current += 1;
    operationRef.current += 1;
    generationDialogRevisionRef.current += 1;
    oralSubmitAttemptRef.current += 1;
    videoSubmitAttemptRef.current += 1;
  }
  const notify = useCallback((message: string) => {
    window.clearTimeout(noticeTimerRef.current);
    setNotice(message);
    noticeTimerRef.current = window.setTimeout(() => setNotice(""), 3000);
  }, []);

  useEffect(() => () => window.clearTimeout(noticeTimerRef.current), []);

  useEffect(() => {
    const request = { userId: currentUser.id, revision: walletRevision };
    walletRequestRef.current = request;
    if (review) {
      setWalletSummary({
        walletStatus: "ready",
        availableCredits: 2680,
      });
      return;
    }
    if (!walletStore || !onWalletSessionExpired) {
      if (currentUser.role !== "customer") {
        setWalletSummary({ walletStatus: "loading", availableCredits: null });
        void getWallet()
          .then((wallet) => {
            if (request !== walletRequestRef.current) return;
            setWalletSummary({
              walletStatus: "ready",
              availableCredits: wallet.available_credits,
            });
          })
          .catch(() => {
            if (request !== walletRequestRef.current) return;
            setWalletSummary({ walletStatus: "error", availableCredits: null });
          });
        return () => {
          if (walletRequestRef.current === request) {
            walletRequestRef.current = null;
          }
        };
      }
      setWalletSummary({
        walletStatus: "unknown",
        availableCredits: null,
      });
      return;
    }
    setWalletSummary({
      walletStatus: "loading",
      availableCredits: null,
    });
    void walletStore
      .loadSessionToken()
      .then((token) => {
        if (request !== walletRequestRef.current) return null;
        if (!token) {
          onWalletSessionExpired();
          throw new Error("登录已失效");
        }
        return customerGetWallet({ kind: "session", token });
      })
      .then((wallet) => {
        if (!wallet || request !== walletRequestRef.current) return;
        setWalletSummary({
          walletStatus: "ready",
          availableCredits: wallet.available_credits,
        });
      })
      .catch(() => {
        if (request !== walletRequestRef.current) return;
        setWalletSummary({
          walletStatus: "error",
          availableCredits: null,
        });
      });
    return () => {
      if (walletRequestRef.current === request) walletRequestRef.current = null;
    };
  }, [
    currentUser.id,
    currentUser.role,
    onWalletSessionExpired,
    review,
    walletRevision,
    walletStore,
  ]);

  // ---- 云端草稿（C7）----
  // 编辑后防抖自动保存；恢复只在用户尚未做任何编辑时生效，绝不覆盖进行中的输入。
  const DRAFT_AUTOSAVE_DELAY_MS = 2000;
  const latestDraftRef = useRef(state.draft);
  generationRef.current = generation;
  const closeGenerationDialog = useCallback(() => {
    generationDialogRevisionRef.current += 1;
    generationRef.current = undefined;
    setGeneration(undefined);
  }, []);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationDialogRevisionRef.current += 1;
    };
  }, []);
  const draftTouchedRef = useRef(false);
  const draftSaveTimerRef = useRef<number | undefined>(undefined);

  const [cloudDraftQueue] = useState(() =>
    createCloudDraftQueue(persistCloudDraft),
  );
  const saveOperationRef = useRef(0);
  const saveMountedRef = useRef(false);
  const saveAccountRef = useRef(currentUser.id);
  const savePermissionGenerationRef = useRef(permissionGenerationRef.current);
  const saveScopeRef = useRef(savedDraftScope(currentUser.id, state.draft));
  const renderedSaveScope = savedDraftScope(currentUser.id, state.draft);
  const renderedPermissionGeneration = permissionGenerationRef.current;
  useLayoutEffect(() => {
    if (
      saveAccountRef.current === currentUser.id &&
      saveScopeRef.current === renderedSaveScope &&
      savePermissionGenerationRef.current === renderedPermissionGeneration
    )
      return;
    if (
      saveAccountRef.current !== currentUser.id ||
      currentUserRoleRef.current === "auditor"
    ) {
      draftTouchedRef.current = false;
    }
    window.clearTimeout(draftSaveTimerRef.current);
    draftSaveTimerRef.current = undefined;
    saveAccountRef.current = currentUser.id;
    saveScopeRef.current = renderedSaveScope;
    savePermissionGenerationRef.current = renderedPermissionGeneration;
    saveOperationRef.current += 1;
  }, [currentUser.id, renderedPermissionGeneration, renderedSaveScope]);
  const referenceAssetsRequestRef = useRef(0);
  const referenceAssetsDraftRef = useRef<StudioDraft | undefined>(undefined);
  useEffect(() => {
    saveMountedRef.current = true;
    return () => {
      saveMountedRef.current = false;
      saveOperationRef.current += 1;
      window.clearTimeout(draftSaveTimerRef.current);
    };
  }, []);
  const persistCloudDraftForScope = useCallback(
    (draft: StudioDraft, accountId: string, isCurrent: () => boolean) =>
      cloudDraftQueue.persist(
        savedDraftScope(accountId, draft),
        draft,
        isCurrent,
      ),
    [cloudDraftQueue],
  );
  const scheduleDraftSave = useCallback(
    (draft: StudioDraft) => {
      if (review || currentUserRoleRef.current === "auditor") return;
      const accountId = currentUser.id;
      const expectedScope = savedDraftScope(accountId, draft);
      const operation = saveOperationRef.current;
      const permissionGeneration = permissionGenerationRef.current;
      window.clearTimeout(draftSaveTimerRef.current);
      draftSaveTimerRef.current = window.setTimeout(() => {
        if (
          !saveMountedRef.current ||
          currentUserRoleRef.current === "auditor" ||
          permissionGenerationRef.current !== permissionGeneration ||
          operation !== saveOperationRef.current ||
          accountId !== saveAccountRef.current ||
          expectedScope !== saveScopeRef.current
        )
          return;
        void persistCloudDraftForScope(draft, accountId, () =>
          Boolean(
            saveMountedRef.current &&
              currentUserRoleRef.current !== "auditor" &&
              permissionGenerationRef.current === permissionGeneration &&
              operation === saveOperationRef.current &&
              accountId === saveAccountRef.current &&
              expectedScope === saveScopeRef.current,
          ),
        ).catch(() => {
          if (
            saveMountedRef.current &&
            currentUserRoleRef.current !== "auditor" &&
            permissionGenerationRef.current === permissionGeneration &&
            operation === saveOperationRef.current &&
            accountId === saveAccountRef.current &&
            expectedScope === saveScopeRef.current
          )
            notify("云端草稿保存失败，内容仍在本机，请稍后继续编辑。");
        });
      }, DRAFT_AUTOSAVE_DELAY_MS);
    },
    [review, currentUser.id, notify, persistCloudDraftForScope],
  );

  useEffect(() => {
    if (currentUser.role !== "auditor") return;
    window.clearTimeout(draftSaveTimerRef.current);
    draftSaveTimerRef.current = undefined;
    generationRef.current = undefined;
    oralSubmissionRef.current = null;
    videoSubmissionRef.current = null;
    oralSubmittingRef.current = false;
    videoSubmittingRef.current = false;
    setPicker(undefined);
    setGeneration(undefined);
    setOralSubmitting(false);
    setVideoSubmitting(false);
  }, [currentUser.role]);

  const restoreDraftAssets = useCallback(
    async (draft: StudioDraft) => {
      const request = ++referenceAssetsRequestRef.current;
      referenceAssetsDraftRef.current = draft;
      setReferenceAssetsPending(true);
      setReferenceAssetsError(false);
      try {
        const restored = await loadDraftMaterials(draft);
        if (
          request !== referenceAssetsRequestRef.current ||
          latestDraftRef.current.id !== draft.id
        )
          return;
        setReferenceAssetsPending(false);
        restoredAssetsRef.current = restored.assets;
        setData((previous) => ({
          ...previous,
          assets: mergeStudioAssets(previous.assets, restored.assets),
        }));
        if (restored.unavailableIds.length) {
          notify("草稿已恢复，部分原素材已不可用，请重新选择。");
        }
      } catch {
        if (
          request !== referenceAssetsRequestRef.current ||
          latestDraftRef.current.id !== draft.id
        )
          return;
        setReferenceAssetsPending(false);
        setReferenceAssetsError(true);
      }
    },
    [notify],
  );

  const retryReferenceAssets = useCallback(() => {
    const draft = referenceAssetsDraftRef.current;
    if (!draft || draft.id !== latestDraftRef.current.id) return;
    void restoreDraftAssets(draft);
  }, [restoreDraftAssets]);

  // 挂载时读云端草稿与我的文案；失败静默（只读路径，不阻塞工作区）。
  // LEFTOVER-ON-OPEN：草稿只挂成一条可拒绝的提示，绝不直接回填。以前这里
  // 直接 setState 覆盖 draft，于是每次打开软件都带着上次的项目、来源视频和
  // 分镜进来——复刻页拿到 projectId 就自动把上次的拆解结果拉回来，用户永远
  // 看不到干净的首屏，也没有任何入口能把它清掉。
  // biome-ignore lint/correctness/useExhaustiveDependencies: loadCloudDraft 读的是「当前会话那个账号」的草稿，依赖是语义上的而非语法上的——换账号必须重读，否则提示里挂的还是上一个人的内容。
  useEffect(() => {
    if (review) return;
    let active = true;
    // 换账号要连提示一起换：留着上一个账号的待恢复草稿，就等于给下一个人
    // 一个能把别人内容读进工作区的按钮。
    setPendingRestore(null);
    setRestoreError("");
    void loadCloudDraft()
      .then(async (restore) => {
        const saved = await loadSavedScriptList().catch(() => []);
        if (!active) return;
        if (saved.length)
          setState((previous) => ({ ...previous, savedScripts: saved }));
        if (restore && !draftTouchedRef.current && shouldPromptRestore(restore))
          setPendingRestore(restore);
      })
      .catch(() => {});
    return () => {
      active = false;
      referenceAssetsRequestRef.current += 1;
      window.clearTimeout(draftSaveTimerRef.current);
    };
  }, [review, currentUser.id]);

  /** 已排队/在途的自动保存必须先作废，否则删掉云端那行之后，
   * 迟到的一次 PUT 会把同一份旧草稿原样写回来。 */
  const cancelPendingDraftSave = useCallback(() => {
    window.clearTimeout(draftSaveTimerRef.current);
    draftSaveTimerRef.current = undefined;
    saveOperationRef.current += 1;
  }, []);

  /** 放弃上次内容：云端那一行与本地未保存文本一起清。 */
  const discardStoredDraft = useCallback(async () => {
    cancelPendingDraftSave();
    await discardCloudDraft();
    clearCreationDraftResidue(currentUser.id);
  }, [cancelPendingDraftSave, currentUser.id]);

  /** 页面级重来入口（复刻页「开始新的复刻」等）用的放弃：即发即忘，
   * 失败只提示，不阻塞页面自己的清空动作。 */
  const discardSavedDraft = () => {
    if (review || currentUserRoleRef.current === "auditor") return;
    draftTouchedRef.current = false;
    setPendingRestore(null);
    void discardStoredDraft().catch(() => {
      if (mountedRef.current) notify("云端上次内容未能清除，请稍后重试。");
    });
  };

  const acceptPendingRestore = () => {
    const restore = pendingRestore;
    if (!restore) return;
    setPendingRestore(null);
    setRestoreError("");
    latestDraftRef.current = restore.draft;
    setState((previous) => ({ ...previous, draft: restore.draft }));
    notify("已恢复上次云端草稿，请核对内容并确认终稿。");
    void restoreDraftAssets(restore.draft);
  };

  // 放弃只删「上次存下的那份」，不动用户这次已经敲进去的内容——
  // 提示还挂着时用户完全可能已经开始写新的了。
  const discardPendingRestore = () => {
    if (currentUserRoleRef.current === "auditor" || discardBusy) return;
    setDiscardBusy(true);
    setRestoreError("");
    void discardStoredDraft()
      .then(() => {
        if (!mountedRef.current) return;
        setPendingRestore(null);
        notify("上次内容已清除，这次从空白开始。");
      })
      .catch(() => {
        if (mountedRef.current)
          setRestoreError("未能清除上次内容，请检查网络后重试。");
      })
      .finally(() => {
        if (mountedRef.current) setDiscardBusy(false);
      });
  };
  // 自动保存始终跟随最新草稿：导入项目、任务快照回填等不经 patchDraft 的
  // 路径也在这里并入追踪。
  useEffect(() => {
    latestDraftRef.current = state.draft;
    const restoringDraft = referenceAssetsDraftRef.current;
    if (restoringDraft && restoringDraft.id !== state.draft.id) {
      referenceAssetsRequestRef.current += 1;
      referenceAssetsDraftRef.current = undefined;
      setReferenceAssetsPending(false);
      setReferenceAssetsError(false);
    }
    if (draftTouchedRef.current) scheduleDraftSave(state.draft);
  }, [state.draft, scheduleDraftSave]);
  // 获取服务端按秒预算；按实际成功时长结算，最多扣受理时预留的积分。
  useEffect(() => {
    void oralQuoteRevision;
    if (review || generation !== "数字人口播") {
      setOralPriceFen(null);
      setOralQuoteStatus("idle");
      setOralQuoteError("");
      return;
    }
    let active = true;
    setOralPriceFen(null);
    setOralQuoteStatus("loading");
    setOralQuoteError("");
    void getOralPrice({ script_text: state.draft.script.text })
      .then((price) => {
        if (active) {
          setOralPriceFen(price.unit_price_fen);
          setOralPriceCredits(price.unit_credits ?? null);
          setOralBudget(
            price.budget_seconds !== undefined &&
              price.estimated_credits !== undefined
              ? {
                  seconds: price.budget_seconds,
                  credits: price.estimated_credits,
                }
              : undefined,
          );
          setOralQuoteStatus("ready");
        }
      })
      .catch((cause: unknown) => {
        if (active) {
          setOralPriceFen(null);
          setOralQuoteStatus("error");
          setOralQuoteError(
            customerVisibleErrorMessage(cause, "口播报价读取失败，请重试。"),
          );
        }
      });
    return () => {
      active = false;
    };
  }, [review, generation, oralQuoteRevision, state.draft.script.text]);

  const retryOralQuote = useCallback(
    () => setOralQuoteRevision((value) => value + 1),
    [],
  );

  // 视频生成能力探测（扩展模式是否开放、单批上限）；审核模式不探测。
  // biome-ignore lint/correctness/useExhaustiveDependencies: 修订号专门用于用户点击后重新发起能力请求。
  useEffect(() => {
    if (review) return;
    let active = true;
    setVideoCapabilities(undefined);
    setVideoCapabilitiesStatus("loading");
    void getIndependentCapabilities()
      .then((capabilities) => {
        if (active) {
          setVideoCapabilities(capabilities);
          setVideoCapabilitiesStatus("ready");
        }
      })
      .catch(() => {
        if (active) {
          setVideoCapabilities(undefined);
          setVideoCapabilitiesStatus("error");
        }
      });
    return () => {
      active = false;
    };
  }, [review, videoCapabilitiesRevision]);

  const retryVideoCapabilities = useCallback(
    () => setVideoCapabilitiesRevision((value) => value + 1),
    [],
  );

  // 视频生成确认弹窗：按分辨率/时长/条数拉取按秒报价；草稿参数变化自动刷新。
  // P1-4：与提交/报价同源钳位（4–15），三处不得再出现「非法 → 8」旁路。
  const videoDuration = normalizeCustomerDuration(state.draft.duration);
  const videoResolution: GenerationQuoteInput["resolution"] =
    state.draft.resolution === "2K" ? "2K" : "768P";
  const videoCount =
    state.draft.count === 2 || state.draft.count === 4 ? state.draft.count : 1;
  const currentVideoQuoteInput: GenerationQuoteInput = {
    resolution: videoResolution,
    duration_seconds: videoDuration,
    quantity: videoCount,
  };
  const videoQuoteReady =
    videoQuoteStatus === "ready" &&
    quoteMatchesInput(videoQuote, currentVideoQuoteInput);
  // 套餐折扣（后端「取更优」后随报价带出）：费用行标注折后口径。
  const videoQuoteDiscountZhe = videoQuoteReady
    ? formatDiscountZhe(videoQuote?.discount_rate)
    : null;
  const videoQuoteDiscountSource = videoQuoteReady
    ? discountSourceLabel(videoQuote?.discount_source)
    : null;
  const videoQuoteDiscountNote = videoQuoteDiscountZhe
    ? `已享${videoQuoteDiscountZhe}优惠${videoQuoteDiscountSource ? `（${videoQuoteDiscountSource}）` : ""}`
    : null;
  useEffect(() => {
    void videoQuoteRevision;
    if (review || generation !== "视频生成") {
      setVideoQuote(null);
      setVideoQuoteStatus("idle");
      setVideoQuoteError("");
      return;
    }
    let active = true;
    setVideoQuote(null);
    setVideoQuoteStatus("loading");
    setVideoQuoteError("");
    const input = {
      resolution: videoResolution,
      duration_seconds: videoDuration,
      quantity: videoCount,
    };
    void getGenerationPriceQuote(input)
      .then((quote) => {
        if (!active) return;
        if (!quoteMatchesInput(quote, input)) {
          setVideoQuoteStatus("error");
          setVideoQuoteError("视频报价参数与当前生成参数不一致，请重新获取。");
          return;
        }
        setVideoQuote(quote);
        setVideoQuoteStatus("ready");
      })
      .catch((cause: unknown) => {
        if (active) {
          setVideoQuote(null);
          setVideoQuoteStatus("error");
          setVideoQuoteError(
            customerVisibleErrorMessage(cause, "视频报价读取失败，请重试。"),
          );
        }
      });
    return () => {
      active = false;
    };
  }, [
    review,
    generation,
    videoDuration,
    videoResolution,
    videoCount,
    videoQuoteRevision,
  ]);

  const retryVideoQuote = useCallback(
    () => setVideoQuoteRevision((value) => value + 1),
    [],
  );

  const submitOralTask = async () => {
    if (currentUserRoleRef.current === "auditor") {
      notify("当前账号为只读权限，不能提交生成。");
      return;
    }
    if (
      oralSubmittingRef.current ||
      generationRef.current !== "数字人口播" ||
      oralQuoteStatus !== "ready" ||
      oralPriceFen === null
    ) {
      if (!oralSubmittingRef.current) notify("请先取得有效口播报价后再提交。");
      return;
    }
    const dialogRevision = generationDialogRevisionRef.current;
    const permissionGeneration = permissionGenerationRef.current;
    const attempt = ++oralSubmitAttemptRef.current;
    const draft = latestDraftRef.current;
    const draftFingerprint = JSON.stringify(draft);
    const page = studioPageRef.current;
    const isCurrent = () =>
      mountedRef.current &&
      currentUserRoleRef.current !== "auditor" &&
      permissionGenerationRef.current === permissionGeneration &&
      generationDialogRevisionRef.current === dialogRevision &&
      generationRef.current === "数字人口播" &&
      studioPageRef.current === page &&
      JSON.stringify(latestDraftRef.current) === draftFingerprint;
    oralSubmittingRef.current = true;
    setOralSubmitting(true);
    try {
      const mode = "text";
      const input = buildOralInput(draft, mode);
      const request = {
        identityId: input.ipId,
        avatarId: input.avatarId,
        voiceId: input.voiceId,
        mode: "TTS" as const,
        title: draft.script.title || "未命名口播",
        scriptText: draft.script.text,
        audioAssetId: input.audioAssetId,
        ...(input.mode === "text" && input.subtitles
          ? { subtitle: ORAL_SUBTITLE_PRESET }
          : {}),
      };
      const fingerprint = JSON.stringify(request);
      if (oralSubmissionRef.current?.fingerprint !== fingerprint) {
        oralSubmissionRef.current = {
          fingerprint,
          key: crypto.randomUUID(),
        };
      }
      const result = await createOralTask({
        ...request,
        idempotencyKey: oralSubmissionRef.current.key,
      });
      if (!isCurrent()) return;
      oralSubmissionRef.current = null;
      closeGenerationDialog();
      if (result.status === "FAILED") {
        notify("口播任务提交未成功，请核对素材后重试。");
      } else {
        notify("口播任务已提交，可在任务中心查看进度。");
        navigate("tasks");
        refresh();
      }
    } catch (cause: unknown) {
      if (isCurrent()) {
        notify(
          customerVisibleErrorMessage(cause, "口播任务提交失败，请稍后重试。"),
        );
        // 服务端已给出需要多少积分；把钱包侧栏一并打开，省掉用户自己找入口。
        if (isInsufficientCredits(cause)) openLive("wallet");
      }
    } finally {
      if (oralSubmitAttemptRef.current === attempt) {
        oralSubmittingRef.current = false;
        setOralSubmitting(false);
      }
    }
  };
  const refresh = useCallback(() => {
    setRevision((value) => value + 1);
    setWalletRevision((value) => value + 1);
  }, []);

  const referenceDraftError = (draft: StudioDraft): string | undefined => {
    if (referenceAssetsPending) return "草稿参考图仍在恢复，请稍后重试。";
    if (referenceAssetsError) return "草稿参考图读取失败，请先重试。";
    if (videoCapabilitiesStatus === "error")
      return "视频生成能力读取失败，请先重试。";
    if (!videoCapabilities) return "视频生成能力尚未读取完成，请稍后重试。";
    if (!videoCapabilities.r2v_enabled) return "该模式暂未开放，敬请期待。";
    const validation = validateReferences(
      draft.referenceIds,
      [...data.assets, ...data.materials],
      {
        maxReferenceImages: videoCapabilities.max_reference_images,
        maxReferenceVideos: videoCapabilities.max_reference_videos,
        maxReferenceAudios: videoCapabilities.max_reference_audios,
      },
    );
    if (validation.issues[0]) return validation.issues[0];
    if (validation.referenceIds.length === 0) return "请至少选择一个参考素材";
    return undefined;
  };

  const submitVideoTask = async () => {
    if (currentUserRoleRef.current === "auditor") {
      notify("当前账号为只读权限，不能提交生成。");
      return;
    }
    if (videoSubmittingRef.current || generationRef.current !== "视频生成")
      return;
    const draft = latestDraftRef.current;
    if (draft.replicaPreparationPending) {
      notify("请先完成复刻准备并交接新提示词与采用首帧。");
      return;
    }
    const draftFingerprint = JSON.stringify(draft);
    const page = studioPageRef.current;
    const dialogRevision = generationDialogRevisionRef.current;
    const permissionGeneration = permissionGenerationRef.current;
    const attempt = ++videoSubmitAttemptRef.current;
    const isCurrent = () =>
      mountedRef.current &&
      currentUserRoleRef.current !== "auditor" &&
      permissionGenerationRef.current === permissionGeneration &&
      generationDialogRevisionRef.current === dialogRevision &&
      generationRef.current === "视频生成" &&
      studioPageRef.current === page &&
      JSON.stringify(latestDraftRef.current) === draftFingerprint;
    if (draft.promptBindingsStale) {
      notify("请先核对当前提示词与参考素材的绑定。");
      return;
    }
    const quoteInput = videoQuoteInput(draft);
    if (
      videoQuoteStatus !== "ready" ||
      !quoteMatchesInput(videoQuote, quoteInput)
    ) {
      notify("请先取得与当前参数一致的视频报价后再提交。");
      return;
    }
    videoSubmittingRef.current = true;
    setVideoSubmitting(true);
    setVideoSubmitError("");
    setVideoSubmitRejected(false);
    try {
      const mode = resolveVideoMode(
        state.page,
        Boolean(draft.firstFrameId),
        Boolean(draft.tailFrameId),
      );
      if (mode === "l2v")
        throw new Error("仅尾帧生成尚待验证，可先编辑或优化提示词。");
      if (mode === "r2v") {
        const error = referenceDraftError(draft);
        if (error) throw new Error(error);
      }
      const request = {
        display_name: draft.videoName?.trim() || "未命名视频",
        mode,
        prompt_text:
          mode === "r2v"
            ? constrainReferenceVideoPrompt(draft.prompt)
            : draft.prompt,
        first_frame_asset_id:
          mode === "i2v" ? (draft.firstFrameId ?? null) : null,
        last_frame_asset_id:
          mode === "i2v" && videoCapabilities?.last_frame_enabled !== false
            ? (draft.tailFrameId ?? null)
            : null,
        reference_asset_ids: mode === "r2v" ? draft.referenceIds : [],
        output_duration_seconds: normalizeCustomerDuration(draft.duration),
        resolution:
          draft.resolution === "2K" ? ("2K" as const) : ("768P" as const),
        ratio: resolveSubmittedRatio(mode, draft.ratio),
        quantity: draft.count === 2 || draft.count === 4 ? draft.count : 1,
        provider: defaultBatchProvider(),
      };
      const fingerprint = JSON.stringify(request);
      if (videoSubmissionRef.current?.fingerprint !== fingerprint) {
        videoSubmissionRef.current = {
          fingerprint,
          key: crypto.randomUUID(),
        };
      }
      const result = await createIndependentVideoTask({
        ...request,
        idempotency_key: videoSubmissionRef.current.key,
      });
      if (!isCurrent()) return;
      videoSubmissionRef.current = null;
      closeGenerationDialog();
      patchDraft({ videoBatchId: result.id });
      notify("视频生成任务已提交，可在预览区查看进度。");
      refresh();
    } catch (cause: unknown) {
      if (isCurrent()) {
        const message = customerVisibleErrorMessage(
          cause,
          "视频生成任务提交失败，请稍后重试。",
        );
        setVideoSubmitError(message);
        // Validation runs before task creation and billing; transport errors
        // remain uncertain and keep the existing idempotent retry guidance.
        setVideoSubmitRejected(
          cause instanceof Error && "status" in cause && cause.status === 422,
        );
      }
    } finally {
      if (videoSubmitAttemptRef.current === attempt) {
        videoSubmittingRef.current = false;
        setVideoSubmitting(false);
      }
    }
  };

  // biome-ignore lint/correctness/useExhaustiveDependencies: revision explicitly requests a full reload.
  useEffect(() => {
    if (review) return;
    // Every core reload replaces the asset slice, including a same-account
    // profile refresh. Its person cache must be invalidated at the same time.
    loadedPeopleRef.current.clear();
    let active = true;
    setData((previous) => ({ ...previous, loading: true }));
    const coreLoad = loadStudioData(workspaceUser, { includeViral: false });
    void coreLoad
      .then((result) => {
        if (active)
          setData({
            ...result,
            assets: mergeStudioAssets(result.assets, restoredAssetsRef.current),
          });
      })
      .catch((cause: unknown) => {
        if (active)
          setData({
            ...emptyData,
            loading: false,
            errors: [
              customerVisibleErrorMessage(cause, "工作区暂不可用，请重试。"),
            ],
          });
      });
    void loadViralVideos()
      .then((viral) =>
        coreLoad.then(() => {
          if (!active) return;
          setData((previous) => ({
            ...previous,
            homepageVideos: viral.videos,
            errors: [
              ...previous.errors.filter(
                (message) => !message.includes("爆款失败"),
              ),
              ...viral.errors,
            ],
          }));
        }),
      )
      .catch(() => {
        // 爆款区独立容错，不覆盖已加载的项目和任务。
      });
    return () => {
      active = false;
    };
    // MATERIAL-PERF-C（P0-5）：bootstrap 只随 user.id 重跑——profile 异步到达
    // 只改 display_name/username，若随整个 workspaceUser 依赖会把首屏全量
    // 加载整体重跑一遍（double bootstrap，第一遍全部作废）。
  }, [review, workspaceUser.id, revision]);

  // Silent tasks poll: the shell reads everything once on entry, so a batch
  // that finishes while the customer watches would otherwise stay "running"
  // until a manual refresh. Only the tasks slice updates, failures stay
  // quiet (the next tick retries; the explicit 重试加载 path reports errors),
  // and the poll pauses while the tab is hidden or a live panel is busy.
  useEffect(() => {
    if (review) return;
    const timer = window.setInterval(() => {
      if (document.hidden) return;
      retryWallet();
      void reloadTasks(currentUser)
        .then((tasks) => {
          setData((previous) => {
            // MATERIAL-PERF-D（P1-3）：任务无实质变化时返回原引用，跳过
            // 全树重渲染（此前每 20s 必然重渲染整个工作区）。
            const refreshedIds = new Set(tasks.map((task) => task.id));
            const merged = [
              ...tasks,
              ...previous.tasks.filter((task) => !refreshedIds.has(task.id)),
            ];
            if (sameTasks(previous.tasks, merged)) return previous;
            return { ...previous, tasks: merged };
          });
        })
        .catch(() => {});
      void reloadStats().then((stats) => {
        if (stats) setData((previous) => ({ ...previous, stats }));
      });
    }, TASKS_POLL_INTERVAL_MS);
    return () => {
      window.clearInterval(timer);
    };
  }, [review, currentUser, retryWallet]);

  const personToLoad = state.page.startsWith("person-")
    ? state.selectedPersonId
    : state.draft.ipId;
  useEffect(() => {
    if (
      review ||
      data.loading ||
      !personToLoad ||
      (loadedPeopleRef.current.has(personToLoad) &&
        state.page !== "person-photos")
    )
      return;
    let active = true;
    loadedPeopleRef.current.add(personToLoad);
    void loadPersonAssets(personToLoad)
      .then((result) => {
        if (!active) {
          loadedPeopleRef.current.delete(personToLoad);
          return;
        }
        setData((previous) => ({
          ...previous,
          assets: [
            ...previous.assets.filter(
              (asset) => !result.assets.some((item) => item.id === asset.id),
            ),
            ...result.assets,
          ],
          people: previous.people.map((person) =>
            person.id === personToLoad
              ? {
                  ...person,
                  sceneLookCount: result.assets.filter(
                    (asset) => asset.source === "人物库场景造型",
                  ).length,
                  photoIds: result.assets
                    .filter((asset) => !asset.composite)
                    .map((asset) => asset.id),
                  photoCount: result.total,
                }
              : person,
          ),
          pagination: {
            ...previous.pagination,
            scenes: {
              ...previous.pagination?.scenes,
              [personToLoad]: {
                loaded: result.loaded,
                total: result.total,
              },
            },
          },
          errors: [...previous.errors, ...result.errors],
        }));
        if (result.errors.length) loadedPeopleRef.current.delete(personToLoad);
      })
      .catch((cause: unknown) => {
        loadedPeopleRef.current.delete(personToLoad);
        if (active)
          notify(customerVisibleErrorMessage(cause, "人物场景照片暂不可用"));
      });
    return () => {
      active = false;
    };
  }, [review, data.loading, personToLoad, notify, state.page]);

  useEffect(() => {
    const onHashChange = () => {
      const route = studioRouteFromHash(window.location.hash);
      operationRef.current += 1;
      setState((previous) => ({ ...previous, ...route }));
      if (livePanel) refresh();
      setLivePanel(undefined);
    };
    window.addEventListener("hashchange", onHashChange);
    window.addEventListener("popstate", onHashChange);
    return () => {
      window.removeEventListener("hashchange", onHashChange);
      window.removeEventListener("popstate", onHashChange);
    };
  }, [livePanel, refresh]);

  const navigate: StudioContextValue["navigate"] = (page, patch = {}) => {
    operationRef.current += 1;
    setState((previous) => {
      const nextState = navigateStudioState(previous, page, patch);
      window.history.pushState(null, "", studioHashForState(nextState));
      return nextState;
    });
    setMenuOpen(false);
    setShowSearch(false);
    if (livePanel) refresh();
    setLivePanel(undefined);
    window.scrollTo?.({ top: 0 });
  };
  const patchDraft = (
    patch: Partial<StudioDraft>,
    options?: { persistNow?: boolean },
  ) => {
    if (currentUserRoleRef.current === "auditor") return;
    if (Object.hasOwn(patch, "ipId") && patch.ipId !== state.draft.ipId)
      notify(
        "人物已更换，请重新选择该人物的分身和声音，并核对文案中的自我介绍。",
      );
    draftTouchedRef.current = true;
    setState((previous) => {
      const next = patchStudioDraft(previous.draft, patch);
      if (patch.ipId && patch.ipId !== previous.draft.ipId) {
        const owner = data.people.find((person) => person.id === patch.ipId);
        // Explicit handoffs may select an asset together with its verified owner.
        if (
          patch.avatarId &&
          owner?.avatars.some(
            (avatar) =>
              avatar.id === patch.avatarId &&
              avatar.ready &&
              avatar.origin === "视频制作",
          )
        )
          next.avatarId = patch.avatarId;
        if (
          patch.voiceId &&
          owner?.voices.some(
            (voice) => voice.id === patch.voiceId && voice.confirmed,
          )
        )
          next.voiceId = patch.voiceId;
        next.script = { ...next.script, confirmed: false };
      }
      return { ...previous, draft: next };
    });
    // persistNow：跳转类调用（复刻页「去 AI 视频创作」）要求立即落库——
    // 用户下一屏就可能刷新或退出，这份草稿不能停在 2 秒防抖窗口里。
    // 与自动保存共用同一条云草稿队列；先清掉排队中的旧防抖保存，避免它
    // 携带更旧的快照稍后回写。保存失败保留 touched 标记，后续编辑仍会兜底。
    if (options?.persistNow && !review) {
      const accountId = currentUser.id;
      const permissionGeneration = permissionGenerationRef.current;
      const draft = patchStudioDraft(state.draft, patch);
      window.clearTimeout(draftSaveTimerRef.current);
      draftSaveTimerRef.current = undefined;
      void persistCloudDraftForScope(draft, accountId, () =>
        Boolean(
          saveMountedRef.current &&
            currentUserRoleRef.current !== "auditor" &&
            permissionGenerationRef.current === permissionGeneration,
        ),
      ).catch(() => {
        if (mountedRef.current)
          notify("云端草稿保存失败，内容仍在本机，请稍后继续编辑。");
      });
    }
  };
  const extractionDraftId = state.draft.id;
  const extractionProjectId = state.draft.projectId;
  const extractionAssetId = state.draft.sourceAssetId;
  useEffect(() => {
    if (
      review ||
      !extractionProjectId ||
      !extractionAssetId ||
      extractingRef.current
    ) {
      return;
    }
    let active = true;
    let timer: number | undefined;
    let reportedRunning = false;
    const restore = async () => {
      try {
        const task = await loadLatestScriptFromUpload(extractionProjectId);
        if (!active || !task) return;
        const current = latestDraftRef.current;
        if (
          current.id !== extractionDraftId ||
          current.projectId !== extractionProjectId ||
          current.sourceAssetId !== extractionAssetId ||
          task.sourceAssetId !== extractionAssetId
        ) {
          return;
        }
        if (task.status === "PENDING" || task.status === "RUNNING") {
          extractingRef.current = true;
          if (!reportedRunning) {
            reportedRunning = true;
            notify("已恢复上次文案提取任务，正在后台继续处理…");
          }
          timer = window.setTimeout(() => void restore(), 2_000);
          return;
        }
        if (task.status === "SUCCEEDED" && task.result?.text.trim()) {
          extractingRef.current = false;
          const text = task.result.text;
          const restored = patchStudioDraft(current, {
            sourceId: extractionAssetId,
            projectId: extractionProjectId,
            sourceAssetId: extractionAssetId,
            script: {
              ...current.script,
              original: text,
              text: hasCopyResult(current.script) ? current.script.text : "",
              resultKind: hasCopyResult(current.script)
                ? "manual"
                : "extracted",
              confirmed: false,
            },
            scriptEdited: true,
          });
          draftTouchedRef.current = true;
          latestDraftRef.current = restored;
          setState((previous) => ({ ...previous, draft: restored }));
          notify(
            current.script.text.trim()
              ? "文案提取已完成，已保留你的编辑并补回来源原文。"
              : "文案提取已完成，已恢复到当前草稿。",
          );
          return;
        }
        if (
          task.status === "FAILED" ||
          task.status === "SUBMISSION_UNCERTAIN"
        ) {
          extractingRef.current = false;
          notify(task.errorMessage || "上次文案提取失败，可点击提取文案重试。");
        }
      } catch (cause: unknown) {
        extractingRef.current = false;
        if (active) {
          notify(
            customerVisibleErrorMessage(
              cause,
              "读取上次文案提取任务失败，可点击提取文案重试。",
            ),
          );
        }
      }
    };
    void restore();
    return () => {
      active = false;
      window.clearTimeout(timer);
      extractingRef.current = false;
    };
  }, [
    review,
    extractionDraftId,
    extractionProjectId,
    extractionAssetId,
    notify,
  ]);
  const openLive: StudioContextValue["openLive"] = (panel, character) => {
    if (panel === "analysis") {
      navigate("replica");
      return;
    }
    if (review) {
      notify(
        "当前为示例审核。此入口在正式登录后打开已实现的上传、分析、人物或账户功能，不调用真实业务接口。",
      );
      return;
    }
    const selectedTask =
      state.page === "task-detail"
        ? data.tasks.find((task) => task.id === state.selectedTaskId)
        : undefined;
    if (panel === "tasks" && selectedTask?.batchId) {
      const operation = ++operationRef.current;
      void getGenerationBatch(selectedTask.batchId)
        .then((batch) => {
          if (operation !== operationRef.current) return;
          setHandoffBatch(batch);
          setLivePanel("tasks");
        })
        .catch((cause: unknown) => {
          if (operation === operationRef.current)
            notify(
              customerVisibleErrorMessage(cause, "无法读取这条任务，请重试。"),
            );
        });
      return;
    }
    setCharacterTarget(character);
    setLivePanel(panel);
  };
  const importProject = async (project: Project) => {
    const operation = ++operationRef.current;
    try {
      const imported = await loadProjectDraft(project);
      if (operation !== operationRef.current) return;
      setState((previous) => {
        return {
          ...withImportedProject(previous, imported.draft),
          page: "replica" as const,
        };
      });
      window.history.pushState(null, "", "#studio/replica");
      setCharacterTarget(undefined);
      setLivePanel(undefined);
      if (imported.errors.length) notify(imported.errors.join("；"));
      else notify("已带入项目来源与已保存文案。请核对内容并确认终稿。");
    } catch (cause) {
      if (operation === operationRef.current)
        notify(customerVisibleErrorMessage(cause, "无法带入项目内容"));
    }
  };
  const requestGeneration = (kind: StudioTask["type"]) => {
    if (
      kind === "视频生成" &&
      latestDraftRef.current.replicaPreparationPending
    ) {
      notify("请先完成复刻准备并交接新提示词与采用首帧。");
      return;
    }
    if (currentUserRoleRef.current === "auditor") {
      notify("当前账号为只读权限，不能提交生成。");
      return;
    }
    try {
      if (kind === "数字人口播") {
        const input = buildOralInput(state.draft, "text");
        const person = data.people.find((item) => item.id === input.ipId);
        if (
          !person?.avatars.some(
            (avatar) =>
              avatar.id === input.avatarId &&
              avatar.ready &&
              avatar.origin === "视频制作",
          )
        )
          throw new Error("请选择当前人物已就绪的口播分身");
        if (
          input.mode === "text" &&
          !person.voices.some(
            (voice) => voice.id === input.voiceId && voice.confirmed,
          )
        )
          throw new Error("请选择当前人物已确认的声音");
        if (
          input.mode === "audio" &&
          !data.assets.some(
            (asset) =>
              asset.id === input.audioAssetId &&
              asset.kind === "audio" &&
              (!asset.allowedUses || asset.allowedUses.includes("oral_audio")),
          )
        )
          throw new Error("完整口播音频已失效，请重新选择");
      }
      if (kind === "视频生成") {
        const mode = resolveVideoMode(
          state.page,
          Boolean(state.draft.firstFrameId),
          Boolean(state.draft.tailFrameId),
        );
        if (!state.draft.prompt.trim()) {
          throw new Error("请先填写提示词");
        }
        if (!state.draft.videoName?.trim()) {
          throw new Error("请先填写视频名称");
        }
        if (mode === "i2v") {
          const firstFrameId = state.draft.firstFrameId;
          const frame =
            data.assets.find((asset) => asset.id === firstFrameId) ??
            data.materials.find((asset) => asset.id === firstFrameId);
          if (!frame) throw new Error("请选择首帧图片");
          if (
            videoCapabilities &&
            state.draft.tailFrameId &&
            !videoCapabilities.last_frame_enabled
          ) {
            throw new Error("尾帧功能暂未开放，敬请期待。");
          }
        }
        if (mode === "r2v" && state.draft.referenceIds.length === 0) {
          throw new Error("请至少选择一个参考素材");
        }
        if (mode === "r2v") {
          const error = referenceDraftError(state.draft);
          if (error) throw new Error(error);
        }
        if (videoCapabilities) {
          const gated =
            (mode === "t2v" && !videoCapabilities.t2v_enabled) ||
            (mode === "r2v" && !videoCapabilities.r2v_enabled);
          if (gated) {
            throw new Error("该模式暂未开放，敬请期待。");
          }
          if (state.draft.count > videoCapabilities.max_quantity) {
            throw new Error(
              `单批最多生成 ${videoCapabilities.max_quantity} 条视频`,
            );
          }
        }
      }
      if (kind === "数字人口播") {
        setOralPriceFen(null);
        setOralQuoteStatus("loading");
        setOralQuoteError("");
      }
      if (kind === "视频生成") {
        setVideoQuote(null);
        setVideoQuoteStatus("loading");
        setVideoQuoteError("");
      }
      generationDialogRevisionRef.current += 1;
      generationRef.current = kind;
      setVideoSubmitError("");
      setVideoSubmitRejected(false);
      setGeneration(kind);
    } catch (cause) {
      notify(cause instanceof Error ? cause.message : "请检查生成原材料");
    }
  };
  const saveDraft = () => {
    if (currentUserRoleRef.current === "auditor") {
      notify("当前账号为只读权限，不能保存或确认创作内容。");
      return;
    }
    if (
      !state.draft.script.text.trim() &&
      !state.draft.prompt.trim() &&
      !state.draft.sourceId
    ) {
      notify("请先填写创作内容。");
      return;
    }
    const draft = state.draft;
    const accountId = currentUser.id;
    const expectedScope = savedDraftScope(accountId, draft);
    const operation = ++saveOperationRef.current;
    const permissionGeneration = permissionGenerationRef.current;
    window.clearTimeout(draftSaveTimerRef.current);
    const isCurrentSave = () =>
      saveMountedRef.current &&
      currentUserRoleRef.current !== "auditor" &&
      permissionGenerationRef.current === permissionGeneration &&
      saveOperationRef.current === operation &&
      saveAccountRef.current === accountId &&
      saveScopeRef.current === expectedScope;
    if (state.page === "video" || state.page === "reference") {
      if (review) {
        notify("已保留在本次工作区，可继续切换页面。");
        return;
      }
      void persistCloudDraftForScope(draft, accountId, isCurrentSave)
        .then(() => {
          if (!isCurrentSave()) return;
          draftTouchedRef.current = false;
          notify("视频草稿已保存到云端，可继续编辑或换设备恢复。");
        })
        .catch(() => {
          if (isCurrentSave())
            notify("云端保存失败，本次仅保留在工作区，请稍后重试。");
        });
      return;
    }
    const script = {
      ...draft.script,
      ipId: draft.ipId,
      sourceProjectId: draft.projectId,
      // 服务端 source_kind 契约为 viral/project/link/upload，无来源项目时归为 upload。
      sourceKind: draft.projectId ? ("project" as const) : ("upload" as const),
    };
    const recordSavedVersion = (cloudSynced: boolean) =>
      setState((previous) => {
        if (savedDraftScope(accountId, previous.draft) !== expectedScope) {
          return previous;
        }
        return {
          ...previous,
          draft: cloudSynced
            ? { ...previous.draft, scriptEdited: false }
            : previous.draft,
          savedScripts: [
            ...previous.savedScripts.filter((item) => item.id !== script.id),
            script,
          ],
        };
      });
    if (review) {
      recordSavedVersion(true);
      notify("已保留在本次工作区，可继续切换页面。");
      return;
    }

    void persistSavedScript(script, draft.projectId, draft.ipId)
      .then(async () => {
        if (!isCurrentSave()) return;
        try {
          await persistCloudDraftForScope(
            {
              ...draft,
              script,
              scriptEdited: false,
            },
            accountId,
            isCurrentSave,
          );
        } catch {
          if (isCurrentSave()) {
            recordSavedVersion(false);
            notify("版本已保存，但云端草稿同步失败，请再次点击保存版本重试。");
          }
          return;
        }
        if (!isCurrentSave()) return;
        draftTouchedRef.current = false;
        recordSavedVersion(true);
        notify("已保存到我的文案，换设备登录也能找回。");
      })
      .catch(() => {
        if (isCurrentSave()) {
          notify("云端保存失败，本次仅保留在工作区，请稍后重试。");
        }
      });
  };
  const confirmFinalDraft = () => {
    if (currentUserRoleRef.current === "auditor") {
      notify("当前账号为只读权限，不能保存或确认创作内容。");
      return;
    }
    const script = { ...state.draft.script, confirmed: true };
    const draft = patchStudioDraft(state.draft, { script });
    const accountId = currentUser.id;
    const previousScope = savedDraftScope(accountId, state.draft);
    const confirmedScope = savedDraftScope(accountId, draft);
    patchDraft({ script });
    if (review) return;
    // 立即持久化终稿（不等防抖），并软发布到项目脚本版本。
    const permissionGeneration = permissionGenerationRef.current;
    void persistCloudDraftForScope(draft, accountId, () =>
      Boolean(
        saveMountedRef.current &&
          currentUserRoleRef.current !== "auditor" &&
          permissionGenerationRef.current === permissionGeneration &&
          saveAccountRef.current === accountId &&
          (saveScopeRef.current === previousScope ||
            saveScopeRef.current === confirmedScope),
      ),
    ).catch(() => {});
    if (!state.draft.projectId) return;
    void publishScriptVersion(state.draft.projectId, script.text).then(
      (result) => {
        if (
          currentUserRoleRef.current === "auditor" ||
          permissionGenerationRef.current !== permissionGeneration
        )
          return;
        // not-applicable = 项目尚无分镜，属正常边界，不提示失败。
        if (result === "failed")
          notify(
            "终稿已确认，但同步到项目脚本版本未成功，可稍后在来源分析中重试。",
          );
      },
    );
  };
  const extractingRef = useRef(false);
  const extractScriptFromUpload = (
    importedProjectId?: string,
    importedAssetId?: string,
    preparedTaskId?: string,
  ) => {
    if (review) {
      notify("审核示例不调用真实接口。");
      return;
    }
    if (currentUserRoleRef.current === "auditor") {
      notify("当前账号为只读权限，不能提交生成。");
      return;
    }
    if (extractingRef.current) return;
    const explicitSource =
      importedProjectId !== undefined || importedAssetId !== undefined;
    const projectId = explicitSource
      ? importedProjectId
      : (state.draft.projectId ?? state.draft.sourceId);
    const assetId = explicitSource
      ? importedAssetId
      : (state.draft.sourceAssetId ??
        data.projects.find((project) => project.id === projectId)
          ?.reference_asset_id ??
        undefined);
    if (!projectId || !assetId) {
      notify("请先上传视频来源，再提取文案。");
      openLive("projects");
      return;
    }
    const extractionScope = JSON.stringify([
      latestDraftRef.current.id,
      latestDraftRef.current.projectId,
      latestDraftRef.current.sourceId,
      latestDraftRef.current.sourceAssetId,
    ]);
    const extractionAccount = currentUser.id;
    extractingRef.current = true;
    const permissionGeneration = permissionGenerationRef.current;
    notify("正在提取音频并转写文案，预计一到两分钟，请勿关闭页面…");
    // 爆款链路已经由服务端建好任务（音轨是客户端本地抽的），此时只能轮询那一条，
    // 再提交一次会撞上「一个项目至多一条在制任务」而 409。
    void (
      preparedTaskId
        ? awaitScriptFromAudioTask(preparedTaskId)
        : extractScriptFromUploadLive(projectId, assetId)
    )
      .then(({ text }) => {
        extractingRef.current = false;
        if (
          currentUserRoleRef.current === "auditor" ||
          permissionGenerationRef.current !== permissionGeneration
        )
          return;
        const currentDraft = latestDraftRef.current;
        if (
          saveAccountRef.current !== extractionAccount ||
          (JSON.stringify([
            currentDraft.id,
            currentDraft.projectId,
            currentDraft.sourceId,
            currentDraft.sourceAssetId,
          ]) !== extractionScope &&
            !(
              explicitSource &&
              currentDraft.projectId === projectId &&
              (currentDraft.sourceAssetId ?? currentDraft.sourceId) === assetId
            ))
        )
          return;
        const sameSource =
          currentDraft.projectId === projectId &&
          (currentDraft.sourceAssetId ?? currentDraft.sourceId) === assetId;
        const currentScript = sameSource
          ? currentDraft.script
          : createDraft().script;
        patchDraft({
          projectId,
          sourceId: assetId,
          sourceAssetId: assetId,
          scriptEdited: true,
          script: {
            ...currentScript,
            original: text,
            text: hasCopyResult(currentScript) ? currentScript.text : "",
            resultKind: hasCopyResult(currentScript) ? "manual" : "extracted",
            confirmed: false,
          },
        });
        navigate("copy", { returnTo: "workbench" });
        setWalletRevision((value) => value + 1);
        notify("文案已提取，请核对原文并选择二创方式。");
      })
      .catch((cause: unknown) => {
        extractingRef.current = false;
        if (
          currentUserRoleRef.current === "auditor" ||
          permissionGenerationRef.current !== permissionGeneration
        )
          return;
        setWalletRevision((value) => value + 1);
        notify(
          customerVisibleErrorMessage(cause, "文案提取失败，请稍后重试。"),
        );
      });
  };
  /**
   * 爆款文案提取：桌面端走「本地抽音轨 → 上传 → 转写」，平台不再需要留存原片。
   *
   * 只由桌面端调用——Web 端没有本地缓存，仍走既有的服务端拉取链路（设计把 Web 降级
   * 列为允许的少量例外，那条降级路径尚未落地）。
   */
  const extractViralCopy = (video: {
    platformKey?: string;
    nativeId?: string;
    playUrl?: string | null;
  }) => {
    if (review) {
      notify("审核示例不调用真实接口。");
      return;
    }
    if (currentUserRoleRef.current === "auditor") {
      notify("当前账号为只读权限，不能提交生成。");
      return;
    }
    if (extractingRef.current) return;
    if (!video.platformKey || !video.nativeId) {
      notify("该视频缺少可导入的平台标识");
      return;
    }
    const permissionGeneration = permissionGenerationRef.current;
    const extractionAccount = currentUser.id;
    // 结果回来时草稿可能已经换过来源：命中缓存的回填没有 projectId/assetId 可核对，
    // 只能靠「点提取那一刻的草稿作用域」判断，否则会把 A 的文案填进 B 的草稿。
    const extractionScope = JSON.stringify([
      latestDraftRef.current.id,
      latestDraftRef.current.projectId,
      latestDraftRef.current.sourceId,
      latestDraftRef.current.sourceAssetId,
    ]);
    extractingRef.current = true;
    notify("正在准备本地音轨并转写文案，预计一到两分钟，请勿关闭页面…");
    void startViralCopyExtraction({
      platformKey: video.platformKey,
      nativeId: video.nativeId,
      playUrl: video.playUrl,
    })
      .then((receipt) => {
        if (receipt.kind === "task") {
          // 未命中共享缓存：服务端已建好任务，交给与上传链路完全相同的轮询与回填通道。
          extractingRef.current = false;
          extractScriptFromUpload(
            receipt.projectId,
            receipt.sourceAssetId,
            receipt.taskId,
          );
          return;
        }
        extractingRef.current = false;
        const currentDraft = latestDraftRef.current;
        if (
          currentUserRoleRef.current === "auditor" ||
          permissionGenerationRef.current !== permissionGeneration ||
          saveAccountRef.current !== extractionAccount ||
          JSON.stringify([
            currentDraft.id,
            currentDraft.projectId,
            currentDraft.sourceId,
            currentDraft.sourceAssetId,
          ]) !== extractionScope
        )
          return;
        // 命中共享缓存：没有项目也没有任务，只把文案填进草稿，不动来源归属。
        const currentScript = currentDraft.script;
        patchDraft({
          scriptEdited: true,
          script: {
            ...currentScript,
            original: receipt.text,
            text: hasCopyResult(currentScript) ? currentScript.text : "",
            resultKind: hasCopyResult(currentScript) ? "manual" : "extracted",
            confirmed: false,
          },
        });
        navigate("copy", { returnTo: "workbench" });
        notify("已命中共享文案缓存，文案已填入，请核对后选择二创方式。");
      })
      .catch((cause: unknown) => {
        extractingRef.current = false;
        if (
          currentUserRoleRef.current === "auditor" ||
          permissionGenerationRef.current !== permissionGeneration
        )
          return;
        notify(
          customerVisibleErrorMessage(cause, "文案提取失败，请稍后重试。"),
        );
      });
  };
  const patchState = useCallback<StudioContextValue["patchState"]>(
    (patch) => setState((previous) => ({ ...previous, ...patch })),
    [],
  );
  const context: StudioContextValue = {
    state,
    data,
    review,
    user: currentUser,
    videoCapabilities,
    videoCapabilitiesStatus,
    retryVideoCapabilities,
    referenceAssetsPending,
    referenceAssetsError,
    retryReferenceAssets,
    navigate,
    patchDraft,
    patchState,
    updateData: setData,
    notify,
    openPicker: setPicker,
    openLive,
    requestGeneration,
    saveDraft,
    discardSavedDraft,
    confirmFinalDraft,
    extractScriptFromUpload,
    extractViralCopy,
    refresh,
  };
  const savedAtLabel = pendingRestore
    ? formatDraftSavedAt(pendingRestore.updatedAt)
    : "";
  const activeNav = state.page.startsWith("person-")
    ? "people"
    : state.page === "viral-detail"
      ? "viral"
      : state.page === "task-detail"
        ? "tasks"
        : creationPages.has(state.page)
          ? "replica"
          : state.page;
  const activeCount = data.tasks.filter((task) =>
    ["running", "queued"].includes(task.status),
  ).length;
  const closeLive = () => {
    operationRef.current += 1;
    setLivePanel(undefined);
    refresh();
  };
  const visibleNavGroups =
    currentUser.role === "admin"
      ? [
          ...navGroups,
          {
            label: "系统",
            pages: [
              { id: "settings" as const, title: "系统设置", icon: "settings" },
            ],
          },
        ]
      : navGroups;

  const creationWorkspace =
    ["replica", "replacement", "video", "reference"].includes(state.page) &&
    !livePanel;

  return (
    <StudioContext.Provider value={context}>
      <div
        className={`studio-shell ${creationWorkspace ? "studio-shell--creation" : ""} ${state.page === "profile" && customerAccount && !livePanel ? "studio-shell--center" : ""} ${menuOpen ? "studio-shell--menu-open" : ""} ${sidebarCollapsed ? "studio-shell--sidebar-collapsed" : ""}`}
      >
        {menuOpen && (
          <button
            type="button"
            className="studio-menu-backdrop"
            aria-label="关闭导航遮罩"
            onClick={closeMenu}
          />
        )}
        <aside className="studio-sidebar" id="studio-sidebar">
          {menuOpen && (
            <button
              type="button"
              className="studio-menu-close"
              aria-label="关闭导航"
              onClick={closeMenu}
            >
              关闭导航
            </button>
          )}
          <button
            type="button"
            className="studio-brand"
            onClick={() => {
              navigate("workbench");
            }}
          >
            <BrandIdentity />
            <small>乡墅爆款视频创作平台</small>
          </button>
          <Button
            variant="primary"
            className="studio-new-button"
            onClick={() => setNewCreation(true)}
          >
            <Icon name="plus" />
            新建创作
          </Button>
          <nav aria-label="主要导航">
            {visibleNavGroups.map((group, index) => (
              <div
                className="studio-nav-group"
                key={group.label || `main-${index}`}
              >
                {group.label && (
                  <span className="studio-nav-label">{group.label}</span>
                )}
                {group.pages.map((item) => (
                  <button
                    type="button"
                    key={item.id}
                    aria-current={activeNav === item.id ? "page" : undefined}
                    className={activeNav === item.id ? "is-active" : ""}
                    title={sidebarCollapsed ? item.title : undefined}
                    onClick={() => navigate(item.id)}
                  >
                    <Icon name={item.icon} />
                    <span>{item.title}</span>
                    {item.id === "tasks" && activeCount > 0 && (
                      <small>{activeCount}</small>
                    )}
                  </button>
                ))}
              </div>
            ))}
          </nav>
          <button
            type="button"
            className={`studio-account-entry ${state.page === "profile" ? "is-active" : ""}`}
            aria-label={`用户档案，${currentUser.username}`}
            onClick={() => navigate("profile")}
          >
            <WorkspaceUserAvatar currentUser={currentUser} review={review} />
            <span className="studio-account-name" title={currentUser.username}>
              {currentUser.username}
            </span>
          </button>
        </aside>
        <main className={`studio-main studio-route-${state.page}`}>
          <div className="studio-topbar">
            <button
              type="button"
              aria-label={sidebarCollapsed ? "展开侧边栏" : "收起侧边栏"}
              aria-expanded={!sidebarCollapsed}
              aria-controls="studio-sidebar"
              className="studio-sidebar-toggle"
              onClick={toggleSidebarCollapsed}
            >
              <Icon name={sidebarCollapsed ? "chevron" : "back"} />
            </button>
            <button
              type="button"
              aria-label="展开导航"
              aria-expanded={menuOpen}
              aria-controls="studio-sidebar"
              ref={menuButtonRef}
              className="studio-menu-button"
              onClick={() => setMenuOpen((value) => !value)}
            >
              <Icon name="more" />
            </button>
            <form
              className="studio-global-search"
              onSubmit={(event) => {
                event.preventDefault();
                setShowSearch(true);
              }}
            >
              <input
                aria-label="搜索工作区"
                placeholder="搜索乡墅视频、文案、人物、素材"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
              <button type="submit" aria-label="搜索">
                <Icon name="search" />
              </button>
            </form>
            <WorkspaceNotifications />
            <button
              type="button"
              aria-label="用户档案"
              className="studio-top-avatar"
              onClick={() => navigate("profile")}
            >
              <WorkspaceUserAvatar currentUser={currentUser} review={review} />
            </button>
          </div>
          <div className="studio-stage">
            {pendingRestore && (
              <div className="studio-restore-prompt" role="status">
                <Icon name="info" />
                <span>
                  上次还有没做完的创作
                  {savedAtLabel && `（保存于 ${savedAtLabel}）`}
                  。不恢复就从空白开始。
                </span>
                <Button variant="outline" onClick={acceptPendingRestore}>
                  恢复上次内容
                </Button>
                <Button
                  variant="quiet"
                  disabled={currentUser.role === "auditor" || discardBusy}
                  onClick={discardPendingRestore}
                >
                  {discardBusy ? "清除中…" : "放弃"}
                </Button>
              </div>
            )}
            {restoreError && (
              <p className="studio-restore-error" role="alert">
                {restoreError}
              </p>
            )}
            {data.errors.length > 0 && (
              <div className="studio-errors" role="alert">
                {data.errors.join("；")}
                <Button variant="quiet" onClick={refresh}>
                  重试加载
                </Button>
              </div>
            )}
            {livePanel ? (
              <LiveWorkspacePanel
                panel={livePanel}
                currentUser={currentUser}
                characterIdentityId={
                  characterTarget?.identityId ?? state.draft.ipId
                }
                characterInitialTab={characterTarget?.tab}
                customerAccount={customerAccount}
                customerWallet={customerWallet}
                handoffBatch={handoffBatch}
                onClose={closeLive}
                onHandoffConsumed={() => {
                  setHandoffBatch(null);
                  refresh();
                }}
                onProjectSelected={(project) => {
                  void importProject(project);
                }}
                onRefresh={refresh}
              />
            ) : (
              <StudioPageContent
                page={state.page}
                accountSummary={accountSummary}
                customerAccount={customerAccount}
              />
            )}
          </div>
          <footer className="studio-version">
            V1.4 · {review ? "示例审核 · 不调用业务接口" : "创作工作区"}
          </footer>
        </main>
        {notice && (
          <div className="studio-toast" role="status">
            <Icon name="info" />
            <span>{notice}</span>
            <button
              type="button"
              aria-label="关闭提示"
              onClick={() => setNotice("")}
            >
              <Icon name="close" />
            </button>
          </div>
        )}
        {picker && (
          <StudioPicker kind={picker} onClose={() => setPicker(undefined)} />
        )}
        {newCreation && (
          <StudioDialog title="新建创作" onClose={() => setNewCreation(false)}>
            <p>选择创作方式。当前文案和素材会保留，可继续复用。</p>
            <div className="studio-new-options">
              {(
                ["replica", "replacement", "video", "oral"] as StudioPage[]
              ).map((page) => (
                <Button
                  key={page}
                  onClick={() => {
                    navigate(page);
                    setNewCreation(false);
                  }}
                >
                  {pageTitles[page]}
                  <Icon name="arrow" />
                </Button>
              ))}
            </div>
            <Button
              variant="quiet"
              onClick={() => {
                setState((previous) => ({ ...previous, draft: createDraft() }));
                // 只清内存的话，云端那一行会在下次打开时把旧内容原样带回来。
                draftTouchedRef.current = false;
                setPendingRestore(null);
                if (currentUserRoleRef.current !== "auditor")
                  void discardStoredDraft().catch(() => {
                    if (mountedRef.current)
                      notify("云端上次内容未能清除，请稍后重试。");
                  });
                navigate("workbench");
                setNewCreation(false);
              }}
            >
              从空白创作开始
            </Button>
          </StudioDialog>
        )}
        {generation && (
          <StudioDialog
            title={`生成确认 · ${generation}`}
            onClose={closeGenerationDialog}
          >
            <Hint>
              {review
                ? "当前为效果审核，不会创建真实生成任务，也不会扣费。"
                : generation === "数字人口播"
                  ? "将创建一条数字人口播任务，提交前请核对文案与声音。"
                  : generation === "视频生成"
                    ? "将按提示词与参数创建视频生成任务，按秒计费，提交前请核对。"
                    : "此独立创作接口尚未接入。现有项目复刻可通过已实现的生成流程报价与提交。"}
            </Hint>
            {generation === "视频生成" ? (
              <label className="studio-generation-name">
                <span>视频名称</span>
                <input
                  aria-label="视频名称"
                  maxLength={120}
                  value={state.draft.videoName ?? ""}
                  onChange={(event) =>
                    patchDraft({ videoName: event.target.value })
                  }
                  placeholder="请输入视频名称"
                />
              </label>
            ) : null}
            <dl className="studio-details">
              <div>
                <dt>作品</dt>
                <dd>
                  {generation === "视频生成"
                    ? state.draft.videoName?.trim() || "未命名视频"
                    : state.draft.script.title || "未命名创作"}
                </dd>
              </div>
              <div>
                <dt>费用</dt>
                <dd>
                  {generation === "数字人口播" &&
                  oralQuoteStatus === "ready" &&
                  oralPriceFen !== null
                    ? oralPriceCredits !== null
                      ? `${oralPriceCredits} 积分/秒${oralBudget ? `，预计 ${oralBudget.seconds} 秒，预留 ${oralBudget.credits} 积分` : ""}（按成功时长结算，多余退回，最高不超过本次预留）`
                      : `${(oralPriceFen / 100).toFixed(2)} 元/秒`
                    : generation === "视频生成" && videoQuoteReady
                      ? videoQuote.estimated_credits !== undefined
                        ? `${videoQuote.estimated_credits} 积分（${videoQuote.unit_credits} 积分/秒 × ${videoQuote.estimated_seconds} 秒）${videoQuoteDiscountNote ? ` · ${videoQuoteDiscountNote}` : ""}`
                        : `${(videoQuote.estimated_price_fen / 100).toFixed(2)} 元（${videoQuote.unit_price_fen_per_second} 分/秒 × ${videoQuote.estimated_seconds} 秒）${videoQuoteDiscountNote ? ` · ${videoQuoteDiscountNote}` : ""}`
                      : oralQuoteStatus === "loading" ||
                          videoQuoteStatus === "loading"
                        ? "正在读取服务端报价…"
                        : "待服务端报价"}
                </dd>
              </div>
              <div>
                <dt>提交状态</dt>
                <dd>
                  {generation === "视频生成" && videoSubmitError
                    ? videoSubmitRejected
                      ? "提交被拒绝 · 未创建任务 · 未扣费"
                      : "提交未成功确认，请核对任务及流水"
                    : "尚未提交 · 未扣费"}
                </dd>
              </div>
            </dl>
            {generation === "视频生成" && videoSubmitError ? (
              <div className="settings-error" role="alert">
                {videoSubmitError}
              </div>
            ) : null}
            {generation === "数字人口播" && oralQuoteError ? (
              <div className="settings-error" role="alert">
                <p>{oralQuoteError}</p>
                <Button onClick={retryOralQuote} variant="outline">
                  重新获取口播报价
                </Button>
              </div>
            ) : null}
            {generation === "视频生成" && videoQuoteError ? (
              <div className="settings-error" role="alert">
                <p>{videoQuoteError}</p>
                <Button onClick={retryVideoQuote} variant="outline">
                  重新获取视频报价
                </Button>
              </div>
            ) : null}
            {review ||
            generation === "人物置换" ||
            generation === "视频复刻" ? (
              <Button variant="primary" disabled>
                确认费用并提交
              </Button>
            ) : generation === "视频生成" ? (
              <Button
                variant="primary"
                disabled={
                  videoSubmitting ||
                  !videoQuoteReady ||
                  !state.draft.videoName?.trim()
                }
                onClick={() => void submitVideoTask()}
              >
                {videoSubmitting ? "提交中…" : "确认费用并提交"}
              </Button>
            ) : (
              <Button
                disabled={
                  oralSubmitting ||
                  oralQuoteStatus !== "ready" ||
                  oralPriceFen === null
                }
                variant="primary"
                onClick={() => void submitOralTask()}
              >
                {oralSubmitting ? "提交中…" : "确认费用并提交"}
              </Button>
            )}
            {!review &&
              generation !== "数字人口播" &&
              generation !== "视频生成" && (
                <Button
                  onClick={() => {
                    closeGenerationDialog();
                    navigate("replica");
                  }}
                >
                  进入视频拆解
                </Button>
              )}
          </StudioDialog>
        )}
        {showSearch && (
          <StudioDialog title="搜索工作区" onClose={() => setShowSearch(false)}>
            <WorkspaceSearch
              query={search}
              onClose={() => setShowSearch(false)}
            />
          </StudioDialog>
        )}
      </div>
    </StudioContext.Provider>
  );
}

function StudioPageContent({
  page,
  accountSummary,
  customerAccount,
}: {
  page: StudioPage;
  accountSummary: StudioAccountSummary;
  customerAccount?: WorkspaceShellProps["customerAccount"];
}) {
  switch (page) {
    case "workbench":
      return <WorkbenchPage />;
    case "viral":
      return <ViralPage />;
    case "viral-detail":
      return <ViralDetailPage />;
    case "copy":
      return <CopyPage />;
    case "replica":
      return <ReplicaPage />;
    case "replacement":
      return <ReplacementPage />;
    case "video":
    case "reference":
      return <VideoPage />;
    case "oral":
    case "oral-audio":
      return <OralPage />;
    case "tasks":
      return <TasksPage />;
    case "task-detail":
      return <TaskDetailPage />;
    case "people":
      return <PeoplePage />;
    case "person-ip":
    case "person-photos":
    case "person-avatars":
    case "person-voices":
      return <PersonPage />;
    case "materials":
      return <MaterialsPage />;
    case "publishing":
      return <PublishPage />;
    case "analytics":
      return <AnalyticsPage />;
    case "settings":
      return <StudioSettingsPage />;
    case "profile":
      return customerAccount ? (
        <CustomerCenterPage
          key={customerAccount.profile?.user_id}
          account={customerAccount}
        />
      ) : (
        <ProfilePage accountSummary={accountSummary} />
      );
  }
}

function StudioSettingsPage() {
  const { user } = useStudio();
  if (user.role !== "admin") {
    return (
      <Empty
        title="无权访问系统设置"
        description="服务密钥和运行参数仅允许管理员维护。"
      />
    );
  }
  return (
    <section className="studio-system-settings">
      <h1>系统设置</h1>
      <Hint>密钥仅在本页保存，不要发送到聊天或提交到代码库。</Hint>
      <SettingsPanel />
    </section>
  );
}

export { StudioDialog } from "./ui";

function AudioMaterialPicker({
  onClose,
  onSelect,
  purpose,
}: {
  onClose: () => void;
  onSelect: (asset: StudioAsset) => void;
  purpose: "oral_audio" | "voice_clone";
}) {
  const [query, setQuery] = useState("");
  const [activeQuery, setActiveQuery] = useState("");
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<StudioAsset[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [retryRevision, setRetryRevision] = useState(0);
  const operationRef = useRef(0);

  useEffect(() => {
    void retryRevision;
    const operation = ++operationRef.current;
    setStatus("loading");
    void listMaterials({
      mediaType: "audio",
      query: activeQuery,
      page,
      pageSize: 12,
    })
      .then(async (result) => {
        const usable = result.items.filter(
          (item) =>
            item.status === "ready" &&
            item.asset_id &&
            item.allowed_uses.includes(purpose),
        );
        const urls = await Promise.allSettled(
          usable.map((item) =>
            getAssetDownloadUrl(item.asset_id as string).then(
              (response) => response.url,
            ),
          ),
        );
        if (operation !== operationRef.current) return;
        const next = usable.map((item, index) => ({
          ...studioAssetFromMaterial(item),
          url:
            urls[index]?.status === "fulfilled" ? urls[index].value : undefined,
        }));
        setItems((current) =>
          page === 1 ? next : mergeStudioAssets(current, next),
        );
        setHasMore(result.page * result.page_size < result.total);
        setStatus("ready");
      })
      .catch(() => {
        if (operation === operationRef.current) setStatus("error");
      });
    return () => {
      operationRef.current += 1;
    };
  }, [activeQuery, page, purpose, retryRevision]);

  return (
    <StudioDialog
      title={purpose === "oral_audio" ? "选择完整口播音频" : "选择声音克隆样本"}
      onClose={onClose}
    >
      <form
        className="studio-picker-search"
        onSubmit={(event) => {
          event.preventDefault();
          setItems([]);
          setPage(1);
          setActiveQuery(query.trim());
        }}
      >
        <input
          aria-label="搜索云端音频"
          onChange={(event) => setQuery(event.target.value)}
          placeholder="输入音频名称"
          value={query}
        />
        <Button type="submit" variant="outline">
          搜索
        </Button>
      </form>
      {status === "error" ? (
        <Empty
          title="音频素材读取失败"
          description="请检查网络后重试。"
          action={
            <Button
              variant="outline"
              onClick={() => setRetryRevision((value) => value + 1)}
            >
              重试
            </Button>
          }
        />
      ) : status === "loading" && items.length === 0 ? (
        <Empty title="正在读取音频" description="正在查询云端素材库。" />
      ) : items.length === 0 ? (
        <Empty
          title="没有可用音频"
          description={
            purpose === "oral_audio"
              ? "可继续查询下一页，或返回口播页上传完整 MP3。"
              : "可继续查询下一页，或返回声音页上传合规样本。"
          }
          action={
            hasMore ? (
              <Button
                variant="outline"
                onClick={() => setPage((value) => value + 1)}
              >
                加载更多音频
              </Button>
            ) : undefined
          }
        />
      ) : (
        <>
          <div className="studio-picker-grid">
            {items.map((asset) => (
              <article key={asset.id} className="studio-picker-audio">
                <strong>{asset.name}</strong>
                <small>
                  {asset.source} · {asset.duration ?? "时长未知"}
                </small>
                {asset.url ? (
                  <audio
                    controls
                    src={asset.url}
                    aria-label={`预听${asset.name}`}
                  >
                    <track kind="captions" label="口播音频" />
                  </audio>
                ) : (
                  <small>预听地址暂不可用</small>
                )}
                <Button variant="outline" onClick={() => onSelect(asset)}>
                  选择{asset.name}
                </Button>
              </article>
            ))}
          </div>
          {hasMore && (
            <Button
              disabled={status === "loading"}
              variant="outline"
              onClick={() => setPage((value) => value + 1)}
            >
              {status === "loading" ? "加载中…" : "加载更多音频"}
            </Button>
          )}
        </>
      )}
    </StudioDialog>
  );
}

function StudioPicker({
  kind,
  onClose,
}: {
  kind: PickerKind;
  onClose: () => void;
}) {
  const {
    state,
    data,
    patchDraft,
    updateData,
    navigate,
    notify,
    user,
    videoCapabilities,
  } = useStudio();
  const readOnly = user.role === "auditor";
  const [referenceMediaFilter, setReferenceMediaFilter] = useState<
    "all" | "image" | "video" | "audio"
  >("all");
  const [referenceFileQuery, setReferenceFileQuery] = useState("");
  const normalizedReferenceQuery = referenceFileQuery.trim().toLowerCase();
  const matchesReferenceFilter = useCallback(
    (asset: StudioAsset) =>
      kind !== "reference" ||
      ((referenceMediaFilter === "all" ||
        asset.kind === referenceMediaFilter) &&
        (!normalizedReferenceQuery ||
          asset.name.toLowerCase().includes(normalizedReferenceQuery))),
    [kind, normalizedReferenceQuery, referenceMediaFilter],
  );
  const person = data.people.find((item) => item.id === state.draft.ipId);
  const usesCloudImages =
    kind === "reference" || kind === "first-frame" || kind === "tail-frame";
  const cloudImageCandidates = useMemo(
    () =>
      usesCloudImages
        ? data.materials.filter(
            (material) =>
              (kind === "reference" || material.kind === "image") &&
              (kind !== "reference" || isReferenceAsset(material)) &&
              matchesReferenceFilter(material) &&
              !data.assets.some((asset) => asset.id === material.id) &&
              (kind !== "reference" ||
                !state.draft.referenceIds.includes(material.id)),
          )
        : [],
    [
      data.assets,
      data.materials,
      kind,
      state.draft.referenceIds,
      usesCloudImages,
      matchesReferenceFilter,
    ],
  );
  const cloudImagePageSize = 6;
  const [cloudImagePage, setCloudImagePage] = useState(1);
  const [cloudImageUrls, setCloudImageUrls] = useState<Record<string, string>>(
    {},
  );
  const cloudImageOperationRef = useRef(0);
  const cloudImagePages = Math.max(
    1,
    Math.ceil(cloudImageCandidates.length / cloudImagePageSize),
  );
  const visibleCloudImages = useMemo(
    () =>
      cloudImageCandidates.slice(
        (cloudImagePage - 1) * cloudImagePageSize,
        cloudImagePage * cloudImagePageSize,
      ),
    [cloudImageCandidates, cloudImagePage],
  );
  useEffect(() => {
    if (!usesCloudImages) return;
    const operation = ++cloudImageOperationRef.current;
    const visible = visibleCloudImages;
    void Promise.allSettled(
      visible.map((asset) =>
        asset.url
          ? Promise.resolve(asset.url)
          : asset.assetId
            ? getAssetDownloadUrl(asset.assetId).then((result) => result.url)
            : Promise.resolve(undefined),
      ),
    ).then((results) => {
      if (operation !== cloudImageOperationRef.current) return;
      const resolved = Object.fromEntries(
        visible.flatMap((asset, index) => {
          const result = results[index];
          return result?.status === "fulfilled" && result.value
            ? [[asset.id, result.value]]
            : [];
        }),
      );
      // REFERENCE-MATERIAL-PREVIEW：按 id 合并而不是整表替换——翻页后上一页已
      // 签发的地址必须保留，否则已选中的素材缩略图会随翻页变空。
      if (!Object.keys(resolved).length) return;
      setCloudImageUrls((previous) => ({ ...previous, ...resolved }));
    });
    return () => {
      cloudImageOperationRef.current += 1;
    };
  }, [usesCloudImages, visibleCloudImages]);
  const title: Record<PickerKind, string> = {
    person: "选择人物 IP",
    image: "选择人物形象照片",
    "original-frame": "选择原始画面",
    "first-frame": "选择起始帧",
    "tail-frame": "选择结束帧",
    reference: "选择参考素材",
    avatar: "选择口播分身",
    voice: "选择已确认声音",
    audio: "选择完整口播音频",
    "voice-audio": "选择声音克隆样本",
    "avatar-photo": "选择单张照片制作分身",
  };
  const select = (patch: Partial<StudioDraft>) => {
    if (readOnly) return;
    patchDraft(patch);
    onClose();
  };
  if (kind === "audio" || kind === "voice-audio") {
    return (
      <AudioMaterialPicker
        onClose={onClose}
        purpose={kind === "audio" ? "oral_audio" : "voice_clone"}
        onSelect={(asset) => {
          updateData((current) => ({
            ...current,
            assets: mergeStudioAssets(current.assets, [asset]),
          }));
          select({ audioId: asset.id, voiceId: undefined });
        }}
      />
    );
  }
  // 视频生成的帧/参考选择额外提供素材库图片（C9 通道，用户归属）。
  const materials = visibleCloudImages.map((material) => ({
    ...material,
    url: material.url ?? cloudImageUrls[material.id],
  }));
  const referenceValidation = validateReferences(
    state.draft.referenceIds,
    [...data.assets, ...data.materials],
    {
      maxReferenceImages:
        videoCapabilities?.max_reference_images ?? DEFAULT_MAX_REFERENCE_IMAGES,
      maxReferenceVideos:
        videoCapabilities?.max_reference_videos ?? DEFAULT_MAX_REFERENCE_VIDEOS,
      maxReferenceAudios:
        videoCapabilities?.max_reference_audios ?? DEFAULT_MAX_REFERENCE_AUDIOS,
    },
  );
  const assets = [...materials, ...data.assets].filter((asset) =>
    kind === "reference"
      ? isReferenceAsset(asset) &&
        matchesReferenceFilter(asset) &&
        !state.draft.referenceIds.includes(asset.id)
      : asset.kind === "image" &&
        !asset.composite &&
        (kind !== "avatar-photo" || asset.source === "人物库场景造型") &&
        ((kind !== "image" && kind !== "avatar-photo") ||
          !person ||
          asset.personId === person.id),
  );
  return (
    <StudioDialog title={title[kind]} onClose={onClose}>
      <p>只带入本次需要的素材，取消不会修改当前创作。</p>
      {kind === "reference" ? (
        <div className="studio-reference-picker-toolbar">
          <input
            aria-label="搜索素材文件名"
            placeholder="输入文件名搜索"
            type="search"
            value={referenceFileQuery}
            onChange={(event) => {
              setReferenceFileQuery(event.target.value);
              setCloudImagePage(1);
            }}
          />
          <fieldset
            className="studio-reference-picker-filters"
            aria-label="素材类型"
          >
            {[
              ["all", "全部"],
              ["image", "图片"],
              ["video", "视频"],
              ["audio", "声音"],
            ].map(([value, label]) => (
              <Button
                key={value}
                variant={referenceMediaFilter === value ? "primary" : "outline"}
                aria-pressed={referenceMediaFilter === value}
                onClick={() => {
                  setReferenceMediaFilter(
                    value as "all" | "image" | "video" | "audio",
                  );
                  setCloudImagePage(1);
                }}
              >
                {label}
              </Button>
            ))}
          </fieldset>
        </div>
      ) : null}
      <div
        className={
          kind === "reference"
            ? "studio-picker-grid studio-picker-grid--reference"
            : "studio-picker-grid"
        }
      >
        {kind === "person"
          ? data.people.map((item) => (
              <button
                type="button"
                key={item.id}
                onClick={() => select({ ipId: item.id })}
              >
                {item.portrait ? (
                  <img src={item.portrait} alt="" />
                ) : (
                  <Icon name="person" size={48} />
                )}
                <strong>{item.name}</strong>
                <small>{item.role}</small>
              </button>
            ))
          : kind === "voice"
            ? person?.voices
                .filter((voice) => voice.confirmed)
                .map((voice) => (
                  <button
                    type="button"
                    key={voice.id}
                    onClick={() => select({ voiceId: voice.id })}
                  >
                    <Icon name="audio" size={36} />
                    <strong>{voice.name}</strong>
                    <small>已确认</small>
                  </button>
                ))
            : kind === "avatar"
              ? person?.avatars
                  .filter(
                    (avatar) => avatar.ready && avatar.origin === "视频制作",
                  )
                  .map((avatar) => (
                    <button
                      type="button"
                      key={avatar.id}
                      onClick={() => select({ avatarId: avatar.id })}
                    >
                      <Media
                        asset={data.assets.find(
                          (asset) => asset.id === avatar.imageId,
                        )}
                        alt={avatar.name}
                      />
                      <strong>{avatar.name}</strong>
                      <small>{avatar.origin} · 可用于口播</small>
                    </button>
                  ))
              : assets.map((asset) => (
                  <button
                    type="button"
                    key={asset.id}
                    onClick={() => {
                      // REFERENCE-MATERIAL-PREVIEW：云素材选中即写回 data.assets，
                      // 不再要求「签名地址已就绪」——按页签发的地址是异步的，等它
                      // 就绪才回写会让用户在签发完成前选中的素材永远没有预览。
                      // 缺地址由参考页的批量解析补齐（mergeStudioAssets 保留已有 url）。
                      if (usesCloudImages) {
                        updateData((current) => ({
                          ...current,
                          assets: mergeStudioAssets(current.assets, [asset]),
                        }));
                      }
                      if (kind === "avatar-photo") {
                        select({ imageId: asset.id });
                        navigate("person-avatars", {
                          selectedPersonId: person?.id,
                          returnTo: state.page,
                        });
                        notify(
                          "已选择单张照片作为制作原料。照片尚不是口播分身，需完成制作后才能使用。",
                        );
                      } else if (kind === "reference") {
                        if (referenceValidation.issues.length > 0) {
                          notify("请先整理旧草稿中的无效参考素材。");
                          return;
                        }
                        if (
                          asset.kind !== "image" &&
                          asset.durationSeconds !== undefined &&
                          asset.durationSeconds > MAX_REFERENCE_MEDIA_SECONDS
                        ) {
                          notify(
                            asset.kind === "video"
                              ? "参考视频时长不能超过 15 秒，请裁剪后再选取。"
                              : "参考音频时长不能超过 15 秒，请裁剪后再选取。",
                          );
                          return;
                        }
                        const atKindLimit =
                          asset.kind === "image"
                            ? referenceValidation.imageCount >=
                              referenceValidation.imageLimit
                            : asset.kind === "video"
                              ? referenceValidation.videoCount >=
                                referenceValidation.videoLimit
                              : referenceValidation.audioCount >=
                                referenceValidation.audioLimit;
                        if (atKindLimit) {
                          notify(
                            asset.kind === "image"
                              ? `当前最多选择 ${referenceValidation.imageLimit} 张参考图。`
                              : asset.kind === "video"
                                ? `当前最多选择 ${referenceValidation.videoLimit} 个参考视频。`
                                : `当前最多选择 ${referenceValidation.audioLimit} 个参考音频。`,
                          );
                          return;
                        }
                        const nextReferences = validateReferences(
                          [...referenceValidation.referenceIds, asset.id],
                          [...referenceValidation.assets, asset],
                        );
                        if (nextReferences.issues.length > 0) {
                          notify(nextReferences.issues[0]);
                          return;
                        }
                        select({
                          referenceIds: [
                            ...referenceValidation.referenceIds,
                            asset.id,
                          ],
                        });
                      } else
                        select(
                          kind === "image"
                            ? { imageId: asset.id }
                            : kind === "original-frame"
                              ? {
                                  originalImageId: asset.id,
                                  frameConfirmed: false,
                                }
                              : kind === "first-frame"
                                ? { firstFrameId: asset.id }
                                : kind === "tail-frame"
                                  ? { tailFrameId: asset.id }
                                  : { audioId: asset.id, voiceId: undefined },
                        );
                    }}
                  >
                    {asset.kind === "audio" ? (
                      <Icon name="audio" size={40} />
                    ) : (
                      <Media asset={asset} alt={asset.name} />
                    )}
                    <strong>{asset.name}</strong>
                    <small>{asset.source}</small>
                  </button>
                ))}
      </div>
      {kind === "reference" && !assets.length ? (
        <Empty
          title="没有符合条件的素材"
          description="请更换素材类型或文件名关键词。"
        />
      ) : null}
      {usesCloudImages && cloudImageCandidates.length > cloudImagePageSize ? (
        <nav className="content-pagination" aria-label="图片素材分页">
          <Button
            aria-label="上一页素材"
            disabled={cloudImagePage === 1}
            variant="outline"
            onClick={() => setCloudImagePage((page) => page - 1)}
          >
            ‹
          </Button>
          <span>
            {cloudImagePage} / {cloudImagePages}
          </span>
          <Button
            aria-label="下一页素材"
            disabled={cloudImagePage === cloudImagePages}
            variant="outline"
            onClick={() => setCloudImagePage((page) => page + 1)}
          >
            ›
          </Button>
        </nav>
      ) : null}
      {((kind === "voice" &&
        !person?.voices.some((voice) => voice.confirmed)) ||
        (kind === "avatar" &&
          !person?.avatars.some(
            (avatar) => avatar.ready && avatar.origin === "视频制作",
          )) ||
        (kind === "person" && !data.people.length)) && (
        <Empty
          title="没有可选的已就绪资产"
          description="请先在人物库中准备该人物的素材"
          action={
            <Button
              onClick={() => {
                onClose();
                navigate(
                  kind === "voice"
                    ? "person-voices"
                    : kind === "avatar"
                      ? "person-avatars"
                      : "people",
                  { selectedPersonId: person?.id, returnTo: state.page },
                );
              }}
            >
              前往人物库
            </Button>
          }
        />
      )}
    </StudioDialog>
  );
}
