import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { formatDateTime as formatTimestamp } from "./admin/ui/vocabulary";
import {
  confirmGenerationTaskNotCharged,
  createGenerationResultPreviewUrl,
  createGenerationTaskPreviewUrl,
  customerVisibleErrorMessage,
  deleteGenerationBatch,
  downloadGenerationResult,
  downloadGenerationTaskResult,
  type GenerationBatch,
  type GenerationBatchListItem,
  type GenerationReconcileOperation,
  type GenerationTask,
  getGenerationBatch,
  getLatestGenerationReconcileOperation,
  listGenerationBatches,
  openVideoDownloadFolder,
  reconcileUncertainTask,
  regenerateGenerationBatch,
  regenerateGenerationTask,
  renameGenerationBatch,
  retryGenerationTask,
  type UserRole,
  VideoDownloadUnconfirmedError,
  waitForGenerationReconcileOperation,
} from "./api";
import { VideoPreview } from "./VideoPreview";
import {
  generationBatchDisplayStatus,
  hasGenerationResultSource,
  readSnapshotNumber,
  readSnapshotString,
  type VideoDownloadFeedback,
  VideoDownloadNotice,
  VideoResultStage,
} from "./VideoResultStage";

const BATCH_STORAGE_KEY = "generation.batchId";
const BATCH_STORAGE_KEY_PREFIX = `${BATCH_STORAGE_KEY}:`;
const POLL_INTERVAL_MS = 2_000;
const MAX_RETRY_DELAY_MS = 16_000;
const TERMINAL_BATCH_STATUSES = new Set([
  "SUCCEEDED",
  "COMPLETED_WITH_FAILURES",
  "FAILED",
  "CANCELLED",
  "NEEDS_ATTENTION",
]);

type TaskRecordsPanelProps = {
  currentUserId?: string;
  handoffBatch: GenerationBatch | null;
  onHandoffConsumed: () => void;
  userRole: UserRole;
};

type TaskViewMode = "stage" | "ops";

function previewSourceVersion(task: GenerationTask): string {
  return task.result_asset_id
    ? `asset:${task.result_asset_id}`
    : `direct:${task.id}:${task.attempt}`;
}

export function TaskRecordsPanel({
  currentUserId,
  handoffBatch,
  onHandoffConsumed,
  userRole,
}: TaskRecordsPanelProps) {
  const storageKey = batchStorageKey(currentUserId);
  const restoredBatchId =
    handoffBatch?.id ?? readStoredBatchId(storageKey) ?? "";
  const [batchIdInput, setBatchIdInput] = useState(restoredBatchId);
  const [activeBatchId, setActiveBatchId] = useState(restoredBatchId);
  const activeBatchIdRef = useRef(restoredBatchId);
  const requeuedTaskIdsRef = useRef<Set<string>>(new Set());
  const [pollingRevision, setPollingRevision] = useState(0);
  const [batch, setBatch] = useState<GenerationBatch | null>(handoffBatch);
  const latestBatchRef = useRef<GenerationBatch | null>(handoffBatch);
  const [batchError, setBatchError] = useState("");
  const [isBatchLoading, setIsBatchLoading] = useState(false);
  // 客户默认只看结果舞台；对账/重试等低频能力由舞台底部的
  // “处理与诊断”入口进入，避免把内部运维信息放在主视图首层。
  const [viewMode, setViewMode] = useState<TaskViewMode>("stage");
  const [retryDelaySeconds, setRetryDelaySeconds] = useState<number | null>(
    null,
  );
  const [batchHistory, setBatchHistory] = useState<GenerationBatchListItem[]>(
    [],
  );
  const batchHistoryRef = useRef(batchHistory);
  batchHistoryRef.current = batchHistory;
  const accountRef = useRef(currentUserId);
  accountRef.current = currentUserId;
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState("");
  const [isHistoryLoading, setIsHistoryLoading] = useState(false);
  // 批次卡重命名/删除与列表加载共用侧栏，操作消息单独一条通道。
  const [editingBatchId, setEditingBatchId] = useState("");
  const [editingBatchName, setEditingBatchName] = useState("");
  const [renamingBatchId, setRenamingBatchId] = useState("");
  const [deletingBatchId, setDeletingBatchId] = useState("");
  const [batchActionError, setBatchActionError] = useState("");
  const historyRequestRef = useRef(0);
  // 在线播放：预签名 URL 由服务端管理过期，本地只缓存 task → url 映射。
  const [previewUrls, setPreviewUrls] = useState<Record<string, string>>({});
  const isMountedRef = useRef(true);
  const [resultErrors, setResultErrors] = useState<Record<string, string>>({});
  const [activeResultAction, setActiveResultAction] = useState("");
  const [downloadFeedback, setDownloadFeedback] = useState<
    Record<string, VideoDownloadFeedback>
  >({});
  const pendingDownloadsRef = useRef(new Set<string>());
  const downloadScope = JSON.stringify([currentUserId ?? "", activeBatchId]);
  const scopedDownloadFeedback: Record<string, VideoDownloadFeedback> = {};
  for (const task of batch?.tasks ?? []) {
    const feedback = downloadFeedback[`${downloadScope}:${task.id}`];
    if (feedback) scopedDownloadFeedback[task.id] = feedback;
  }
  const [activeTaskAction, setActiveTaskAction] = useState("");
  const [taskActionReasons, setTaskActionReasons] = useState<
    Record<string, string>
  >({});
  const [batchRegenerationReason, setBatchRegenerationReason] = useState("");
  const [batchPaymentConfirmed, setBatchPaymentConfirmed] = useState(false);
  const [taskPaymentConfirmations, setTaskPaymentConfirmations] = useState<
    Record<string, boolean>
  >({});
  const taskOperationKeysRef = useRef<Record<string, string>>({});
  const canOperate = userRole !== "auditor";

  const updateBatch = useCallback((nextBatch: GenerationBatch | null) => {
    const changed = new Set(
      (nextBatch?.tasks ?? [])
        .filter((task) => {
          const previous = latestBatchRef.current?.tasks.find(
            (item) => item.id === task.id,
          );
          return (
            previous &&
            previewSourceVersion(previous) !== previewSourceVersion(task)
          );
        })
        .map((task) => task.id),
    );
    if (changed.size) {
      setPreviewUrls((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([id]) => !changed.has(id)),
        ),
      );
      setResultErrors((current) =>
        Object.fromEntries(
          Object.entries(current).filter(([id]) => !changed.has(id)),
        ),
      );
    }
    latestBatchRef.current = nextBatch;
    setBatch(nextBatch);
    if (nextBatch) {
      setBatchHistory((current) =>
        current.map((item) => mergeBatchSnapshot(item, nextBatch)),
      );
    }
  }, []);

  // 切换/清空批次时只需丢弃本地 URL 缓存；切回后会重新自动签发。
  const releasePreviewUrls = useCallback(() => {
    setPreviewUrls({});
  }, []);

  const selectBatch = useCallback(
    (batchId: string, knownBatch?: GenerationBatch) => {
      if (
        activeBatchIdRef.current === batchId &&
        latestBatchRef.current &&
        !knownBatch
      ) {
        setPollingRevision((current) => current + 1);
        return;
      }
      releasePreviewUrls();
      activeBatchIdRef.current = batchId;
      setActiveBatchId(batchId);
      setBatchIdInput(batchId);
      updateBatch(knownBatch ?? null);
      setBatchError("");
      setRetryDelaySeconds(null);
      setBatchRegenerationReason("");
      setBatchPaymentConfirmed(false);
      setTaskActionReasons({});
      setTaskPaymentConfirmations({});
      requeuedTaskIdsRef.current.clear();
      storeBatchId(storageKey, batchId);
    },
    [releasePreviewUrls, storageKey, updateBatch],
  );

  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  const loadHistory = useCallback(
    async (cursor?: string, append = false) => {
      const requestId = historyRequestRef.current + 1;
      historyRequestRef.current = requestId;
      setIsHistoryLoading(true);
      try {
        const page = await listGenerationBatches({
          limit: 20,
          ...(cursor ? { cursor } : {}),
        });
        if (historyRequestRef.current !== requestId) {
          return;
        }
        // 列表响应可能比详情轮询更旧，保留当前批次刚收到的状态。
        const items = (Array.isArray(page.items) ? page.items : []).map(
          (item) =>
            latestBatchRef.current
              ? mergeBatchSnapshot(item, latestBatchRef.current)
              : item,
        );
        const pageCursor =
          typeof page.next_cursor === "string" ? page.next_cursor : null;
        setBatchHistory((current) =>
          append ? appendUniqueBatches(current, items) : items,
        );
        setNextCursor(pageCursor);
        setHistoryError("");
        if (!activeBatchIdRef.current && items[0]) {
          selectBatch(items[0].id);
        }
      } catch (error) {
        if (historyRequestRef.current === requestId) {
          setHistoryError(
            customerVisibleErrorMessage(
              error,
              "任务记录列表暂不可用，请检查网络连接后重试。",
            ),
          );
        }
      } finally {
        if (historyRequestRef.current === requestId) {
          setIsHistoryLoading(false);
        }
      }
    },
    [selectBatch],
  );

  useEffect(() => {
    void loadHistory();
    return () => {
      historyRequestRef.current += 1;
    };
  }, [loadHistory]);

  useEffect(() => {
    if (!handoffBatch) {
      return;
    }
    selectBatch(handoffBatch.id, handoffBatch);
    onHandoffConsumed();
  }, [handoffBatch, onHandoffConsumed, selectBatch]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: pollingRevision intentionally restarts a poller that stopped on a terminal batch.
  useEffect(() => {
    if (!activeBatchId) {
      return;
    }

    let isActive = true;
    let timeoutId: number | undefined;
    let nextRetryDelayMs = POLL_INTERVAL_MS;
    let requestInFlight = false;

    function scheduleLoad(delayMs: number) {
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
      timeoutId = window.setTimeout(loadBatch, delayMs);
    }

    async function loadBatch() {
      if (requestInFlight) {
        return;
      }
      requestInFlight = true;
      setIsBatchLoading(true);
      try {
        const nextBatch = await getGenerationBatch(activeBatchId);
        if (!isActive) {
          return;
        }
        updateBatch(nextBatch);
        setBatchError("");
        setRetryDelaySeconds(null);
        nextRetryDelayMs = POLL_INTERVAL_MS;
        storeBatchId(storageKey, activeBatchId);
        if (
          !isTerminalBatch(nextBatch) ||
          hasRequeuedTaskStillProcessing(nextBatch, requeuedTaskIdsRef.current)
        ) {
          scheduleLoad(POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (!isActive) {
          return;
        }
        const status = (error as { status?: number }).status;
        if (status === 404) {
          setBatchError("该任务记录不存在，已停止自动刷新。");
          setRetryDelaySeconds(null);
          clearStoredBatchId(storageKey);
          activeBatchIdRef.current = "";
          setActiveBatchId("");
          updateBatch(null);
          return;
        }
        nextRetryDelayMs = Math.min(nextRetryDelayMs * 2, MAX_RETRY_DELAY_MS);
        setBatchError("网络连接失败");
        setRetryDelaySeconds(nextRetryDelayMs / 1_000);
        scheduleLoad(nextRetryDelayMs);
      } finally {
        requestInFlight = false;
        if (isActive) {
          setIsBatchLoading(false);
        }
      }
    }

    function resumePolling() {
      nextRetryDelayMs = POLL_INTERVAL_MS;
      setRetryDelaySeconds(null);
      if (!requestInFlight) {
        void loadBatch();
      }
    }

    window.addEventListener("online", resumePolling);
    window.addEventListener("focus", resumePolling);
    void loadBatch();
    return () => {
      isActive = false;
      window.removeEventListener("online", resumePolling);
      window.removeEventListener("focus", resumePolling);
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [activeBatchId, pollingRevision, storageKey, updateBatch]);

  const reconcileRecoveryKey = (batch?.tasks ?? [])
    .filter((task) => task.status === "SUBMISSION_UNCERTAIN")
    .map((task) => task.id)
    .sort()
    .join(",");

  useEffect(() => {
    if (!activeBatchId || !reconcileRecoveryKey) {
      return;
    }
    let active = true;
    async function recoverReconciliations() {
      try {
        const taskIds = reconcileRecoveryKey.split(",");
        const operations = await Promise.all(
          taskIds.map((taskId) =>
            getLatestGenerationReconcileOperation(taskId),
          ),
        );
        const pending = operations.filter(
          (operation): operation is GenerationReconcileOperation =>
            operation?.status === "PENDING" || operation?.status === "RUNNING",
        );
        const failed = operations.find(
          (operation) => operation?.status === "FAILED",
        );
        if (failed && active) {
          setBatchError(failed.error_message || "任务对账失败，请重新提交。");
        }
        if (
          operations.some((operation) => operation?.status === "SUCCEEDED") &&
          active
        ) {
          setPollingRevision((current) => current + 1);
        }
        if (pending.length === 0) {
          return;
        }
        await Promise.allSettled(
          pending.map((operation) =>
            waitForGenerationReconcileOperation(operation.id),
          ),
        );
        if (active) {
          setPollingRevision((current) => current + 1);
        }
      } catch {
        if (active) {
          setBatchError("读取后台对账进度失败，请稍后刷新。");
        }
      }
    }
    void recoverReconciliations();
    return () => {
      active = false;
    };
  }, [activeBatchId, reconcileRecoveryKey]);

  function handleBatchSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const nextBatchId = batchIdInput.trim();
    if (nextBatchId) {
      selectBatch(nextBatchId);
    }
  }

  function batchDisplayName(item: GenerationBatchListItem): string {
    return item.display_name ?? item.project_name;
  }

  function startBatchRename(item: GenerationBatchListItem) {
    setEditingBatchId(item.id);
    setEditingBatchName(batchDisplayName(item));
    setBatchActionError("");
  }

  function cancelBatchRename() {
    setEditingBatchId("");
    setEditingBatchName("");
    setRenamingBatchId("");
  }

  async function saveBatchRename(item: GenerationBatchListItem) {
    const displayName = editingBatchName.trim();
    if (!displayName) {
      setBatchActionError("批次名称不能为空。");
      return;
    }
    if (displayName === batchDisplayName(item)) {
      cancelBatchRename();
      return;
    }
    setRenamingBatchId(item.id);
    setBatchActionError("");
    try {
      const updated = await renameGenerationBatch(item.id, displayName);
      setBatchHistory((current) =>
        current.map((entry) =>
          entry.id === updated.id
            ? { ...entry, display_name: updated.display_name ?? null }
            : entry,
        ),
      );
      if (activeBatchIdRef.current === updated.id) {
        const current = latestBatchRef.current;
        if (current?.id === updated.id) {
          updateBatch({ ...current, display_name: updated.display_name });
        }
      }
      cancelBatchRename();
    } catch (error) {
      setBatchActionError(
        error instanceof Error ? error.message : "修改批次名称失败，请重试。",
      );
    } finally {
      setRenamingBatchId("");
    }
  }

  // 服务端仅隐藏当前账号的列表记录，不能取消任务或删除后台财务证据。
  async function handleDeleteBatch(item: GenerationBatchListItem) {
    if (!canOperate) return;
    const accountAtStart = currentUserId;
    const displayName = batchDisplayName(item);
    const confirmed = window.confirm(
      `将「${displayName}」仅从本账号任务列表移除，后台生成和费用记录保留，不会取消正在进行的任务。确定删除？`,
    );
    if (!confirmed) {
      return;
    }
    setDeletingBatchId(item.id);
    setBatchActionError("");
    try {
      await deleteGenerationBatch(item.id);
      if (!isMountedRef.current || accountRef.current !== accountAtStart)
        return;
      // 丢弃删除前发出的列表响应，避免旧快照把已隐藏记录重新插回。
      historyRequestRef.current += 1;
      setIsHistoryLoading(false);
      const remaining = batchHistoryRef.current.filter(
        (entry) => entry.id !== item.id,
      );
      batchHistoryRef.current = remaining;
      setBatchHistory(remaining);
      if (activeBatchIdRef.current === item.id) {
        if (remaining[0]) {
          selectBatch(remaining[0].id);
        } else {
          releasePreviewUrls();
          activeBatchIdRef.current = "";
          setActiveBatchId("");
          setBatchIdInput("");
          updateBatch(null);
          setBatchError("");
          clearStoredBatchId(storageKey);
        }
      }
    } catch (error) {
      if (isMountedRef.current && accountRef.current === accountAtStart) {
        setBatchActionError(
          customerVisibleErrorMessage(error, "移除任务记录失败，请重试。"),
        );
      }
    } finally {
      if (isMountedRef.current && accountRef.current === accountAtStart) {
        setDeletingBatchId((current) => (current === item.id ? "" : current));
      }
    }
  }

  async function handleReconcile(taskId: string) {
    if (!canOperate || !activeBatchId) {
      return;
    }
    const batchIdAtStart = activeBatchId;
    const actionKey = `${taskId}:reconcile`;
    const operationKey = operationIdempotencyKey(
      taskOperationKeysRef.current,
      actionKey,
    );
    setActiveTaskAction(actionKey);
    try {
      const operation = await reconcileUncertainTask(taskId, {
        idempotency_key: operationKey,
      });
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      setActiveTaskAction((current) => (current === actionKey ? "" : current));
      setBatchError("任务已进入后台对账，可离开本页继续其他操作。");
      await waitForGenerationReconcileOperation(operation.id);
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      const nextBatch = await getGenerationBatch(batchIdAtStart);
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      updateBatch(nextBatch);
      setBatchError("");
      delete taskOperationKeysRef.current[actionKey];
    } catch (error) {
      if (activeBatchIdRef.current === batchIdAtStart) {
        setBatchError(
          error instanceof Error ? error.message : "任务对账失败，请重试。",
        );
      }
      delete taskOperationKeysRef.current[actionKey];
    } finally {
      setActiveTaskAction((current) => (current === actionKey ? "" : current));
    }
  }

  async function handleRetry(task: GenerationTask) {
    const reason = taskActionReasons[task.id]?.trim();
    if (!canOperate || !activeBatchId || !reason) {
      return;
    }
    const batchIdAtStart = activeBatchId;
    const actionKey = `${task.id}:retry:${reason}`;
    const operationKey = operationIdempotencyKey(
      taskOperationKeysRef.current,
      actionKey,
    );
    setActiveTaskAction(actionKey);
    let retryAccepted = false;
    try {
      await retryGenerationTask(task.id, {
        idempotency_key: operationKey,
        retry_reason: reason,
      });
      retryAccepted = true;
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      const nextBatch = await getGenerationBatch(batchIdAtStart);
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      updateBatch(nextBatch);
      setBatchError("");
      delete taskOperationKeysRef.current[actionKey];
    } catch {
      if (activeBatchIdRef.current === batchIdAtStart) {
        setBatchError("任务安全重试失败，请检查当前状态后再试。");
      }
    } finally {
      if (retryAccepted && activeBatchIdRef.current === batchIdAtStart) {
        requeuedTaskIdsRef.current.add(task.id);
        setPollingRevision((current) => current + 1);
      }
      setActiveTaskAction((current) => (current === actionKey ? "" : current));
    }
  }

  async function handleConfirmNotCharged(task: GenerationTask) {
    const reason = taskActionReasons[task.id]?.trim();
    if (userRole !== "admin" || !activeBatchId || !reason) {
      return;
    }
    const batchIdAtStart = activeBatchId;
    const actionKey = `${task.id}:confirm-not-charged:${reason}`;
    const operationKey = operationIdempotencyKey(
      taskOperationKeysRef.current,
      actionKey,
    );
    setActiveTaskAction(actionKey);
    let confirmationAccepted = false;
    try {
      await confirmGenerationTaskNotCharged(task.id, {
        idempotency_key: operationKey,
        reason,
      });
      confirmationAccepted = true;
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      const nextBatch = await getGenerationBatch(batchIdAtStart);
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      updateBatch(nextBatch);
      setBatchError("");
      delete taskOperationKeysRef.current[actionKey];
    } catch {
      if (activeBatchIdRef.current === batchIdAtStart) {
        setBatchError("确认未计费失败，任务未重新入队。");
      }
    } finally {
      if (confirmationAccepted && activeBatchIdRef.current === batchIdAtStart) {
        requeuedTaskIdsRef.current.add(task.id);
        setPollingRevision((current) => current + 1);
      }
      setActiveTaskAction((current) => (current === actionKey ? "" : current));
    }
  }

  async function handleRegenerateBatch() {
    const reason = batchRegenerationReason.trim();
    if (
      !canOperate ||
      !batch ||
      !activeBatchId ||
      !reason ||
      !batchPaymentConfirmed
    ) {
      return;
    }
    const batchIdAtStart = activeBatchId;
    const estimatedCost = estimatedBatchCost(batch);
    const actionKey = `${batch.id}:paid-regenerate:${reason}:${estimatedCost ?? "unknown"}`;
    const operationKey = operationIdempotencyKey(
      taskOperationKeysRef.current,
      actionKey,
    );
    setActiveTaskAction(actionKey);
    try {
      const replacement = await regenerateGenerationBatch(batch.id, {
        idempotency_key: operationKey,
        estimated_cost_snapshot: estimatedCost,
        generation_reason: reason,
      });
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      delete taskOperationKeysRef.current[actionKey];
      selectBatch(replacement.id, replacement);
      void loadHistory();
    } catch {
      if (activeBatchIdRef.current === batchIdAtStart) {
        setBatchError("整批付费再次生成失败，已保留本次请求供重试。");
      }
    } finally {
      setActiveTaskAction((current) => (current === actionKey ? "" : current));
    }
  }

  // 付费再次生成（单任务）：舞台轻量确认卡与运维详情表单共用此路径，
  // reason 由调用方 UI 组装（付费确认门控在各自按钮上）。
  async function handleRegenerateTask(task: GenerationTask, reason: string) {
    const trimmedReason = reason.trim();
    if (!canOperate || !activeBatchId || !trimmedReason) {
      return;
    }
    const batchIdAtStart = activeBatchId;
    const estimatedCost = task.estimated_cost ?? null;
    const actionKey = `${task.id}:paid-regenerate:${trimmedReason}:${estimatedCost ?? "unknown"}`;
    const operationKey = operationIdempotencyKey(
      taskOperationKeysRef.current,
      actionKey,
    );
    setActiveTaskAction(actionKey);
    try {
      const replacement = await regenerateGenerationTask(task.id, {
        idempotency_key: operationKey,
        estimated_cost_snapshot: estimatedCost,
        generation_reason: trimmedReason,
      });
      if (activeBatchIdRef.current !== batchIdAtStart) {
        return;
      }
      delete taskOperationKeysRef.current[actionKey];
      selectBatch(replacement.id, replacement);
      void loadHistory();
    } catch {
      if (activeBatchIdRef.current === batchIdAtStart) {
        setBatchError("付费重新生成失败，已保留本次请求供重试。");
      }
    } finally {
      setActiveTaskAction((current) => (current === actionKey ? "" : current));
    }
  }

  // 直连结果和归档资产都先经过服务端的项目归属校验，再将播放地址交给客户端。
  const handlePreview = useCallback(
    async (task: GenerationTask) => {
      if (!canOperate) {
        return;
      }
      if (!hasGenerationResultSource(task)) {
        return;
      }
      const batchIdAtStart = activeBatchIdRef.current;
      const actionKey = `${task.id}:preview`;
      const sourceVersion = previewSourceVersion(task);
      const sourceIsCurrent = () => {
        const current = latestBatchRef.current?.tasks.find(
          (item) => item.id === task.id,
        );
        return (
          isMountedRef.current &&
          activeBatchIdRef.current === batchIdAtStart &&
          current !== undefined &&
          previewSourceVersion(current) === sourceVersion
        );
      };
      setActiveResultAction(actionKey);
      setResultErrors((current) => ({ ...current, [task.id]: "" }));
      try {
        let previewUrl: string;
        if (task.direct_result_available) {
          previewUrl = await createGenerationTaskPreviewUrl(task.id);
        } else if (task.result_asset_id) {
          previewUrl = await createGenerationResultPreviewUrl(
            task.result_asset_id,
          );
        } else {
          return;
        }
        if (!sourceIsCurrent()) {
          return;
        }
        setPreviewUrls((current) => ({ ...current, [task.id]: previewUrl }));
      } catch {
        if (sourceIsCurrent()) {
          setResultErrors((current) => ({
            ...current,
            [task.id]: "预览链接获取失败，请重试。",
          }));
        }
      } finally {
        if (isMountedRef.current) {
          setActiveResultAction((current) =>
            current === actionKey ? "" : current,
          );
        }
      }
    },
    [canOperate],
  );

  // 归档预览地址失效时清除黑屏播放器并展示可恢复动作。
  const handlePreviewSourceError = useCallback((task: GenerationTask) => {
    const currentTask = latestBatchRef.current?.tasks.find(
      (item) => item.id === task.id,
    );
    if (
      !currentTask ||
      previewSourceVersion(currentTask) !== previewSourceVersion(task)
    )
      return;
    setPreviewUrls((current) => {
      const next = { ...current };
      delete next[task.id];
      return next;
    });
    setResultErrors((current) => ({
      ...current,
      [task.id]: "视频已生成，但播放地址暂时不可用。",
    }));
  }, []);

  async function handleDownload(task: GenerationTask) {
    if (
      !canOperate ||
      !hasGenerationResultSource(task) ||
      (!task.direct_result_available && !task.result_asset_id)
    ) {
      return;
    }
    const actionKey = `${downloadScope}:${task.id}`;
    if (pendingDownloadsRef.current.has(actionKey)) return;
    pendingDownloadsRef.current.add(actionKey);
    setDownloadFeedback((current) => ({
      ...current,
      [actionKey]: { status: "pending" },
    }));
    try {
      const result = task.direct_result_available
        ? await downloadGenerationTaskResult(task.id, `${task.id}.mp4`)
        : await downloadGenerationResult(
            task.result_asset_id ?? "",
            `${task.id}.mp4`,
          );
      if (isMountedRef.current) {
        setDownloadFeedback((current) => ({ ...current, [actionKey]: result }));
      }
    } catch (error) {
      if (isMountedRef.current) {
        setDownloadFeedback((current) => ({
          ...current,
          [actionKey]: {
            status: "error",
            message:
              error instanceof VideoDownloadUnconfirmedError
                ? "尚未确认保存结果，请先检查所选文件夹，避免重复下载。"
                : "下载失败，请检查网络和保存位置后重试。",
          },
        }));
      }
    } finally {
      pendingDownloadsRef.current.delete(actionKey);
    }
  }

  async function handleOpenDownloadFolder(
    task: GenerationTask,
    downloadId: string,
  ) {
    if (!canOperate) return;
    const actionKey = `${downloadScope}:${task.id}`;
    try {
      await openVideoDownloadFolder(downloadId);
    } catch {
      if (isMountedRef.current) {
        setDownloadFeedback((current) => {
          const saved = current[actionKey];
          if (saved?.status !== "saved" || saved.downloadId !== downloadId)
            return current;
          return {
            ...current,
            [actionKey]: {
              ...saved,
              folderError: "无法打开文件夹，请按上方路径查看视频。",
            },
          };
        });
      }
    }
  }

  return (
    <section className="task-records" aria-label="任务记录">
      <div className="task-records-layout">
        <aside className="batch-history-panel" aria-label="批次历史">
          <div className="batch-history-heading">
            <div>
              <h3>批次列表</h3>
            </div>
            <button
              className="secondary-button"
              disabled={isHistoryLoading}
              onClick={() => {
                void loadHistory();
                setPollingRevision((current) => current + 1);
              }}
              type="button"
            >
              刷新
            </button>
          </div>
          {historyError ? (
            <p className="status-note" role="status">
              {historyError}
            </p>
          ) : null}
          {batchActionError ? (
            <p className="status-note status-note--error" role="status">
              {batchActionError}
            </p>
          ) : null}
          {isHistoryLoading && batchHistory.length === 0 ? (
            <p className="muted">正在加载项目任务记录…</p>
          ) : null}
          <ul className="batch-history-list">
            {batchHistory.map((item) => {
              const displayName = batchDisplayName(item);
              const isEditing = editingBatchId === item.id;
              const isRenaming = renamingBatchId === item.id;
              const isDeleting = deletingBatchId === item.id;
              return (
                <li key={item.id}>
                  {isEditing ? (
                    <div className="batch-history-edit">
                      <input
                        aria-label="修改批次名称"
                        onChange={(event) =>
                          setEditingBatchName(event.target.value)
                        }
                        type="text"
                        value={editingBatchName}
                      />
                      <button
                        disabled={isRenaming}
                        onClick={() => void saveBatchRename(item)}
                        type="button"
                      >
                        {isRenaming ? "正在保存…" : "保存"}
                      </button>
                      <button
                        className="secondary-button"
                        disabled={isRenaming}
                        onClick={cancelBatchRename}
                        type="button"
                      >
                        取消
                      </button>
                    </div>
                  ) : (
                    <div
                      className={
                        item.id === activeBatchId
                          ? "batch-history-card batch-history-card--active"
                          : "batch-history-card"
                      }
                    >
                      <button
                        aria-label={`打开批次 ${item.id}`}
                        aria-pressed={item.id === activeBatchId}
                        className="batch-history-card__main"
                        onClick={() => selectBatch(item.id)}
                        type="button"
                      >
                        <span className="batch-history-card__title">
                          <strong>{displayName}</strong>
                          <span
                            className={`batch-status batch-status--${generationBatchDisplayStatus(item).toLowerCase()}`}
                          >
                            {formatStatus(generationBatchDisplayStatus(item))}
                          </span>
                        </span>
                        <span className="batch-history-card__meta">
                          {formatTimestamp(item.created_at)} · 已结束{" "}
                          {item.progress.terminal_count} /{" "}
                          {item.progress.total_count}
                          {item.progress.counts.failed > 0
                            ? ` · 失败 ${item.progress.counts.failed}`
                            : ""}
                          {item.progress.counts.cancelled > 0
                            ? ` · 已取消 ${item.progress.counts.cancelled}`
                            : ""}
                        </span>
                      </button>
                      {canOperate ? (
                        <div className="batch-history-card__actions">
                          <button
                            className="secondary-button"
                            onClick={() => startBatchRename(item)}
                            type="button"
                          >
                            重命名
                          </button>
                          <button
                            aria-label={`删除批次 ${displayName}`}
                            className="secondary-button batch-delete-button"
                            disabled={isDeleting}
                            onClick={() => void handleDeleteBatch(item)}
                            type="button"
                          >
                            {isDeleting ? "正在删除…" : "删除"}
                          </button>
                        </div>
                      ) : null}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
          {nextCursor ? (
            <button
              className="load-more-button"
              disabled={isHistoryLoading}
              onClick={() => void loadHistory(nextCursor, true)}
              type="button"
            >
              加载更多任务记录
            </button>
          ) : null}
        </aside>

        <div className="batch-detail-column">
          <BatchStatusMessage
            error={batchError}
            isLoading={isBatchLoading && !batch}
            onRetry={() => setPollingRevision((current) => current + 1)}
            retryDelaySeconds={retryDelaySeconds}
          />
          {batch ? (
            viewMode === "stage" ? (
              <VideoResultStage
                activeResultAction={activeResultAction}
                activeTaskAction={activeTaskAction}
                batch={batch}
                batchTitle={
                  batch.display_name ??
                  batchHistory.find((item) => item.id === batch.id)
                    ?.project_name ??
                  "视频生成批次"
                }
                canOperate={canOperate}
                isCustomerView={userRole === "customer"}
                onDownload={handleDownload}
                downloadFeedback={scopedDownloadFeedback}
                onOpenDownloadFolder={handleOpenDownloadFolder}
                onOpenOpsDetail={() => setViewMode("ops")}
                onPreviewSourceError={handlePreviewSourceError}
                onRegenerate={handleRegenerateTask}
                onRequestPreview={handlePreview}
                previewUrls={previewUrls}
                resultErrors={resultErrors}
              />
            ) : (
              <>
                <section
                  aria-label="运维视图导航"
                  className="task-view-switch task-view-switch--ops"
                >
                  <div>
                    <strong>处理与诊断</strong>
                    <p>仅在任务异常、对账或重试时使用。</p>
                  </div>
                  <button
                    className="secondary-button"
                    onClick={() => setViewMode("stage")}
                    type="button"
                  >
                    返回生成结果
                  </button>
                </section>
                <BatchPanel
                  downloadFeedback={scopedDownloadFeedback}
                  onOpenDownloadFolder={handleOpenDownloadFolder}
                  activeResultAction={activeResultAction}
                  activeTaskAction={activeTaskAction}
                  batch={batch}
                  batchPaymentConfirmed={batchPaymentConfirmed}
                  batchRegenerationReason={batchRegenerationReason}
                  canOperate={canOperate}
                  onConfirmNotCharged={handleConfirmNotCharged}
                  onDownload={handleDownload}
                  onPreview={handlePreview}
                  onPreviewSourceError={handlePreviewSourceError}
                  onReconcile={handleReconcile}
                  onRegenerateBatch={handleRegenerateBatch}
                  onRegenerateTask={(task) =>
                    handleRegenerateTask(task, taskActionReasons[task.id] ?? "")
                  }
                  onRetry={handleRetry}
                  onBatchPaymentConfirmationChange={setBatchPaymentConfirmed}
                  onBatchRegenerationReasonChange={setBatchRegenerationReason}
                  onTaskActionReasonChange={(taskId, reason) =>
                    setTaskActionReasons((current) => ({
                      ...current,
                      [taskId]: reason,
                    }))
                  }
                  onTaskPaymentConfirmationChange={(taskId, confirmed) =>
                    setTaskPaymentConfirmations((current) => ({
                      ...current,
                      [taskId]: confirmed,
                    }))
                  }
                  previewUrls={previewUrls}
                  resultErrors={resultErrors}
                  taskActionReasons={taskActionReasons}
                  taskPaymentConfirmations={taskPaymentConfirmations}
                  userRole={userRole}
                />
              </>
            )
          ) : (
            <EmptyBatchState hasHistory={batchHistory.length > 0} />
          )}

          {userRole !== "customer" ? (
            <details className="batch-compatibility-query">
              <summary>兼容查询：通过 Batch ID 查找历史记录</summary>
              <form className="batch-form" onSubmit={handleBatchSubmit}>
                <label htmlFor="batch-id">Batch ID</label>
                <div className="batch-input-row">
                  <input
                    id="batch-id"
                    value={batchIdInput}
                    placeholder="粘贴 generation batch id"
                    onChange={(event) => setBatchIdInput(event.target.value)}
                  />
                  <button type="submit" disabled={!batchIdInput.trim()}>
                    查询任务记录
                  </button>
                </div>
              </form>
            </details>
          ) : null}
        </div>
      </div>
    </section>
  );
}

function BatchStatusMessage({
  error,
  isLoading,
  onRetry,
  retryDelaySeconds,
}: {
  error: string;
  isLoading: boolean;
  onRetry: () => void;
  retryDelaySeconds: number | null;
}) {
  if (error) {
    return (
      <div className="status-note" role="status">
        <span>
          {retryDelaySeconds
            ? `${error}，${retryDelaySeconds} 秒后重试`
            : error}
        </span>
        <button onClick={onRetry} type="button">
          立即刷新
        </button>
      </div>
    );
  }
  return isLoading ? (
    <p className="status-note" role="status">
      正在刷新任务记录
    </p>
  ) : null;
}

function BatchPanel({
  downloadFeedback,
  onOpenDownloadFolder,
  activeResultAction,
  activeTaskAction,
  batch,
  batchPaymentConfirmed,
  batchRegenerationReason,
  canOperate,
  onBatchPaymentConfirmationChange,
  onBatchRegenerationReasonChange,
  onConfirmNotCharged,
  onDownload,
  onPreview,
  onPreviewSourceError,
  onReconcile,
  onRegenerateBatch,
  onRegenerateTask,
  onRetry,
  onTaskActionReasonChange,
  onTaskPaymentConfirmationChange,
  previewUrls,
  resultErrors,
  taskActionReasons,
  taskPaymentConfirmations,
  userRole,
}: {
  downloadFeedback: Record<string, VideoDownloadFeedback>;
  onOpenDownloadFolder: (task: GenerationTask, downloadId: string) => void;
  activeResultAction: string;
  activeTaskAction: string;
  batch: GenerationBatch;
  batchPaymentConfirmed: boolean;
  batchRegenerationReason: string;
  canOperate: boolean;
  onBatchPaymentConfirmationChange: (confirmed: boolean) => void;
  onBatchRegenerationReasonChange: (reason: string) => void;
  onConfirmNotCharged: (task: GenerationTask) => void;
  onDownload: (task: GenerationTask) => void;
  onPreview: (task: GenerationTask) => void;
  onPreviewSourceError: (task: GenerationTask) => void;
  onReconcile: (taskId: string) => void;
  onRegenerateBatch: () => void;
  onRegenerateTask: (task: GenerationTask) => void;
  onRetry: (task: GenerationTask) => void;
  onTaskActionReasonChange: (taskId: string, reason: string) => void;
  onTaskPaymentConfirmationChange: (taskId: string, confirmed: boolean) => void;
  previewUrls: Record<string, string>;
  resultErrors: Record<string, string>;
  taskActionReasons: Record<string, string>;
  taskPaymentConfirmations: Record<string, boolean>;
  userRole: UserRole;
}) {
  const counts = {
    ...batch.progress.counts,
    needs_attention: batch.tasks.filter(taskNeedsAttention).length,
  };
  const historicalCounts = batch.progress.historical_counts ?? {};
  const hasHistoricalFailures =
    (historicalCounts.failed ?? 0) > 0 ||
    (historicalCounts.archive_failed ?? 0) > 0;
  const batchActionBusy = Boolean(activeTaskAction);
  const batchReason = batchRegenerationReason.trim();
  // 状态摘要只保留非零项：全零时说明批次尚未产生状态变化，不再铺满 8 个空格子。
  const countItems = statusCountItems(counts).filter(([, value]) => value > 0);
  // 异常与来源信息合并进“需要关注”面板，形成摘要之后的第二段，避免四条横幅平铺。
  const hasAttentionItems =
    Boolean(batch.source_batch_id) ||
    hasHistoricalFailures ||
    Boolean(batch.stale) ||
    counts.needs_attention > 0;
  return (
    <div className="batch-panel">
      <div className="batch-overview">
        <span
          className={`batch-status batch-status--${generationBatchDisplayStatus(batch).toLowerCase()}`}
        >
          {formatStatus(generationBatchDisplayStatus(batch))}
        </span>
        <span className="batch-overview__progress">
          {batch.progress.progress_percent}%
        </span>
        <span className="batch-overview__count">
          任务已结束 {batch.progress.terminal_count} /{" "}
          {batch.progress.total_count}
        </span>
        {countItems.length > 0 ? (
          <span className="batch-overview__chips">
            {countItems.map(([label, value]) => (
              <span key={label} className="batch-overview__chip">
                {label} {value}
              </span>
            ))}
          </span>
        ) : null}
      </div>
      <div
        aria-label="批次进度"
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={batch.progress.progress_percent}
        className="progress-track"
        role="progressbar"
      >
        <span style={{ width: `${batch.progress.progress_percent}%` }} />
      </div>
      {hasAttentionItems ? (
        <section aria-label="需要关注" className="batch-attention-panel">
          {batch.source_batch_id ? (
            <div className="batch-lineage" role="status">
              <strong>冻结输入重生成</strong>
              <span>来源批次 {batch.source_batch_id}</span>
              {batch.source_task_id ? (
                <span>来源任务 {batch.source_task_id}</span>
              ) : null}
              {batch.generation_reason ? (
                <span>{batch.generation_reason}</span>
              ) : null}
            </div>
          ) : null}
          {hasHistoricalFailures ? (
            <p className="historical-failure-summary">
              历史事实：失败 {historicalCounts.failed ?? 0} · 归档失败{" "}
              {historicalCounts.archive_failed ?? 0} · 已替代{" "}
              {historicalCounts.superseded ?? 0}
            </p>
          ) : null}
          {batch.stale ? (
            <p className="stale-banner" role="status">
              该批次的上游版本已更新；结果仍可查看，但不能作为当前版本的交付依据。
            </p>
          ) : null}
          {counts.needs_attention ? (
            <p className="attention-banner">
              需要处理 {counts.needs_attention}
            </p>
          ) : null}
        </section>
      ) : null}
      {canOperate ? (
        <details
          className="paid-regeneration-controls"
          aria-label="整批付费再次生成"
        >
          <summary>整批再次生成（付费）</summary>
          <div className="paid-regeneration-body">
            <p>
              仅复用本批次的冻结请求与 Prompt，将新建 {batch.quantity} 个付费
              任务。 金额快照：{formatCost(estimatedBatchCost(batch))}
            </p>
            <label>
              <span>整批重生成原因</span>
              <input
                aria-label="整批重生成原因"
                disabled={batchActionBusy}
                maxLength={500}
                onChange={(event) =>
                  onBatchRegenerationReasonChange(event.target.value)
                }
                placeholder="说明为什么需要再生成整批视频"
                value={batchRegenerationReason}
              />
            </label>
            <label className="paid-confirmation-check">
              <input
                aria-label={`确认新建 ${batch.quantity} 个付费任务`}
                checked={batchPaymentConfirmed}
                disabled={batchActionBusy}
                onChange={(event) =>
                  onBatchPaymentConfirmationChange(event.target.checked)
                }
                type="checkbox"
              />
              <span>我已确认本次会新增 {batch.quantity} 次付费视频生成</span>
            </label>
            <button
              disabled={
                !batchReason || !batchPaymentConfirmed || batchActionBusy
              }
              onClick={onRegenerateBatch}
              type="button"
            >
              整批付费再次生成
            </button>
          </div>
        </details>
      ) : null}
      <ul className="task-list">
        {batch.tasks.map((task) => (
          <TaskItem
            downloadFeedback={downloadFeedback[task.id]}
            onOpenDownloadFolder={onOpenDownloadFolder}
            activeResultAction={activeResultAction}
            activeTaskAction={activeTaskAction}
            canOperate={canOperate}
            key={task.id}
            onConfirmNotCharged={onConfirmNotCharged}
            onDownload={onDownload}
            onPreview={onPreview}
            onPreviewSourceError={onPreviewSourceError}
            onReconcile={onReconcile}
            onRegenerate={onRegenerateTask}
            onRetry={onRetry}
            onTaskActionReasonChange={onTaskActionReasonChange}
            onTaskPaymentConfirmationChange={onTaskPaymentConfirmationChange}
            previewUrl={previewUrls[task.id]}
            resultError={resultErrors[task.id]}
            task={task}
            taskActionReason={taskActionReasons[task.id] ?? ""}
            taskPaymentConfirmed={taskPaymentConfirmations[task.id] ?? false}
            userRole={userRole}
          />
        ))}
      </ul>
    </div>
  );
}

function TaskItem({
  downloadFeedback,
  onOpenDownloadFolder,
  activeResultAction,
  activeTaskAction,
  canOperate,
  onConfirmNotCharged,
  onDownload,
  onPreview,
  onPreviewSourceError,
  onReconcile,
  onRegenerate,
  onRetry,
  onTaskActionReasonChange,
  onTaskPaymentConfirmationChange,
  previewUrl,
  resultError,
  task,
  taskActionReason,
  taskPaymentConfirmed,
  userRole,
}: {
  downloadFeedback?: VideoDownloadFeedback;
  onOpenDownloadFolder: (task: GenerationTask, downloadId: string) => void;
  activeResultAction: string;
  activeTaskAction: string;
  canOperate: boolean;
  onConfirmNotCharged: (task: GenerationTask) => void;
  onDownload: (task: GenerationTask) => void;
  onPreview: (task: GenerationTask) => void;
  onPreviewSourceError: (task: GenerationTask) => void;
  onReconcile: (taskId: string) => void;
  onRegenerate: (task: GenerationTask) => void;
  onRetry: (task: GenerationTask) => void;
  onTaskActionReasonChange: (taskId: string, reason: string) => void;
  onTaskPaymentConfirmationChange: (taskId: string, confirmed: boolean) => void;
  previewUrl?: string;
  resultError?: string;
  task: GenerationTask;
  taskActionReason: string;
  taskPaymentConfirmed: boolean;
  userRole: UserRole;
}) {
  const attentionNeeded = taskNeedsAttention(task);
  const hasResult = hasGenerationResultSource(task);
  const previewAction = `${task.id}:preview`;
  const availableActions = task.available_actions ?? [];
  const canRetry = canOperate && availableActions.includes("RETRY");
  const canReconcile = canOperate && availableActions.includes("RECONCILE");
  const requiresAdminConfirmation = availableActions.includes(
    "CONFIRM_NOT_CHARGED",
  );
  const canConfirmNotCharged =
    userRole === "admin" && requiresAdminConfirmation;
  const canRegenerate = canOperate && availableActions.includes("REGENERATE");
  const actionReason = taskActionReason.trim();
  const taskActionBusy = Boolean(activeTaskAction);

  const resolution = readSnapshotString(task, "resolution");
  const outputDuration = readSnapshotNumber(task, "output_duration_seconds");

  return (
    <li className="task-item task-result-card">
      <div className="task-detail-grid">
        {/* 左栏：结果事实。预览、质检与 dl 事实表按阅读顺序纵向排布。 */}
        <div className="task-detail-main">
          <div className="task-result-heading">
            <div>
              <strong className="task-id-mono">{task.id}</strong>
              <span>阶段：{taskStage(task)}</span>
            </div>
            {attentionNeeded ? (
              <span className="attention-tag">需要处理</span>
            ) : null}
          </div>

          {task.superseded_by_task_id ? (
            <p className="task-resolution-note">
              已由任务 {task.superseded_by_task_id} 替代；本记录仅保留历史事实。
            </p>
          ) : null}

          {task.error_message_redacted &&
          (task.status !== "SUCCEEDED" ||
            task.archive_status === "ARCHIVE_FAILED" ||
            !hasResult) ? (
            <p className="task-error-summary">
              {customerVisibleErrorMessage(
                task.error_message_redacted,
                "视频生成失败，请稍后重试。",
              )}
            </p>
          ) : null}

          {hasResult ? (
            <div className="task-result-actions">
              <span className="muted">
                {previewUrl
                  ? "已获取播放地址"
                  : resultError
                    ? "结果地址暂不可用"
                    : "有结果记录，待确认播放地址"}
              </span>
              {canOperate ? (
                <>
                  <button
                    disabled={activeResultAction === previewAction}
                    onClick={() => void onPreview(task)}
                    type="button"
                  >
                    {previewUrl ? `刷新预览 ${task.id}` : `加载预览 ${task.id}`}
                  </button>
                  <button
                    disabled={downloadFeedback?.status === "pending"}
                    onClick={() => void onDownload(task)}
                    type="button"
                  >
                    {downloadFeedback?.status === "pending"
                      ? "正在下载…"
                      : `下载 MP4 ${task.id}`}
                  </button>
                </>
              ) : (
                <span className="muted">审计只读，不可预览或下载结果</span>
              )}
            </div>
          ) : (
            <span className="muted">
              {task.archive_status === "ARCHIVE_FAILED"
                ? "结果交付失败，暂无可用播放地址"
                : "等待成片返回"}
            </span>
          )}
          {hasResult && canOperate ? (
            <section
              aria-label={`结果播放器 ${task.id}`}
              className="task-result-preview"
            >
              {previewUrl ? (
                <VideoPreview
                  aria-label={`结果预览 ${task.id}`}
                  className="task-result-video"
                  controls
                  onError={() => onPreviewSourceError(task)}
                  preload="auto"
                  src={previewUrl}
                />
              ) : (
                <span className="muted">
                  点击加载预览，系统将签发新的短期链接。
                </span>
              )}
            </section>
          ) : null}
          {resultError ? (
            <p className="task-error-summary" role="status">
              {resultError}
            </p>
          ) : null}
          <VideoDownloadNotice
            feedback={downloadFeedback}
            onOpenFolder={(downloadId) =>
              onOpenDownloadFolder(task, downloadId)
            }
          />

          <dl className="task-facts">
            <div>
              <dt>生成能力</dt>
              <dd>视频生成</dd>
            </div>
            {resolution ? (
              <div>
                <dt>分辨率</dt>
                <dd>{resolution}</dd>
              </div>
            ) : null}
            {outputDuration !== null ? (
              <div>
                <dt>成片时长</dt>
                <dd>{outputDuration} 秒</dd>
              </div>
            ) : null}
            <div>
              <dt>生成耗时</dt>
              <dd>{formatDuration(task.duration_seconds)}</dd>
            </div>
            <div>
              <dt>费用</dt>
              <dd>{formatCost(task.actual_cost ?? task.estimated_cost)}</dd>
            </div>
            <div>
              <dt>尝试</dt>
              <dd>{task.attempt ?? 0} 次</dd>
            </div>
            <div>
              <dt>任务参考号</dt>
              <dd>
                {task.provider_task_id_tail
                  ? task.provider_task_id_tail
                  : "未公开"}
              </dd>
            </div>
            {task.submitted_at ? (
              <div>
                <dt>提交时间</dt>
                <dd>{formatTimestamp(task.submitted_at)}</dd>
              </div>
            ) : null}
          </dl>
        </div>

        {/* 右栏：处理操作折叠区。默认收起，避免表单噪音淹没结果事实。 */}
        {canOperate ? (
          <aside className="task-detail-ops" aria-label={`任务操作 ${task.id}`}>
            {canReconcile || canRetry || canConfirmNotCharged ? (
              <details className="task-resolution-controls">
                <summary>处理此任务</summary>
                <div className="task-detail-ops__body">
                  {canReconcile ? (
                    <button
                      aria-label={`对账 ${task.id}`}
                      disabled={taskActionBusy}
                      type="button"
                      onClick={() => onReconcile(task.id)}
                    >
                      对账
                    </button>
                  ) : null}
                  {canRetry || canConfirmNotCharged ? (
                    <label>
                      <span>处理原因</span>
                      <input
                        aria-label={`处理原因 ${task.id}`}
                        disabled={taskActionBusy}
                        maxLength={500}
                        onChange={(event) =>
                          onTaskActionReasonChange(task.id, event.target.value)
                        }
                        placeholder="填写本次处理依据"
                        value={taskActionReason}
                      />
                    </label>
                  ) : null}
                  {canRetry ? (
                    <button
                      aria-label={`${task.archive_status === "ARCHIVE_FAILED" ? "重试归档" : "安全重试"} ${task.id}`}
                      disabled={!actionReason || taskActionBusy}
                      onClick={() => onRetry(task)}
                      type="button"
                    >
                      {task.archive_status === "ARCHIVE_FAILED"
                        ? "重试归档"
                        : "安全重试"}
                    </button>
                  ) : null}
                  {canConfirmNotCharged ? (
                    <button
                      aria-label={`确认未计费 ${task.id}`}
                      disabled={!actionReason || taskActionBusy}
                      onClick={() => onConfirmNotCharged(task)}
                      type="button"
                    >
                      确认未计费并重新入队
                    </button>
                  ) : null}
                  {requiresAdminConfirmation && userRole !== "admin" ? (
                    <p className="task-resolution-note">
                      需管理员核对账单并确认未计费后才能重提。
                    </p>
                  ) : null}
                </div>
              </details>
            ) : null}
            {canRegenerate ? (
              <details className="task-paid-regeneration">
                <summary>付费重新生成</summary>
                <div className="task-detail-ops__body">
                  <p>
                    只复用该任务的冻结 Prompt，新建一次付费视频生成；
                    原记录保留。金额快照：
                    {formatCost(task.estimated_cost)}
                  </p>
                  <label>
                    <span>重新生成原因</span>
                    <input
                      aria-label={`重新生成原因 ${task.id}`}
                      disabled={taskActionBusy}
                      maxLength={500}
                      onChange={(event) =>
                        onTaskActionReasonChange(task.id, event.target.value)
                      }
                      placeholder="填写本次新增付费生成的原因"
                      value={taskActionReason}
                    />
                  </label>
                  <label className="paid-confirmation-check">
                    <input
                      aria-label={`确认为任务 ${task.id} 新增一次付费生成`}
                      checked={taskPaymentConfirmed}
                      disabled={taskActionBusy}
                      onChange={(event) =>
                        onTaskPaymentConfirmationChange(
                          task.id,
                          event.target.checked,
                        )
                      }
                      type="checkbox"
                    />
                    <span>我已确认本次将产生一次新的付费视频生成</span>
                  </label>
                  <button
                    aria-label={`付费重新生成 ${task.id}`}
                    disabled={
                      !actionReason || !taskPaymentConfirmed || taskActionBusy
                    }
                    onClick={() => onRegenerate(task)}
                    type="button"
                  >
                    付费重新生成
                  </button>
                </div>
              </details>
            ) : null}
          </aside>
        ) : null}
      </div>
    </li>
  );
}

function EmptyBatchState({ hasHistory }: { hasHistory: boolean }) {
  return (
    <div className="empty-state">
      <h2>{hasHistory ? "请选择一个任务批次" : "还没有任务记录"}</h2>
      <p>
        {hasHistory
          ? "从左侧项目批次中选择记录查看详情。"
          : "生成的视频会显示在这里。"}
      </p>
    </div>
  );
}

function appendUniqueBatches(
  current: GenerationBatchListItem[],
  incoming: GenerationBatchListItem[],
): GenerationBatchListItem[] {
  const existingIds = new Set(current.map((item) => item.id));
  return [...current, ...incoming.filter((item) => !existingIds.has(item.id))];
}

function mergeBatchSnapshot(
  item: GenerationBatchListItem,
  detail: GenerationBatch,
): GenerationBatchListItem {
  if (item.id !== detail.id) {
    return item;
  }
  return {
    ...item,
    status: detail.status,
    progress: detail.progress,
    tasks: detail.tasks,
    needs_attention_count: detail.progress.counts.needs_attention,
    has_results: detail.tasks.some(hasGenerationResultSource),
  };
}

function operationIdempotencyKey(
  keys: Record<string, string>,
  actionKey: string,
): string {
  const existing = keys[actionKey];
  if (existing) {
    return existing;
  }
  const randomPart =
    typeof globalThis.crypto?.randomUUID === "function"
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const created = `generation-task:${randomPart}`;
  keys[actionKey] = created;
  return created;
}

function isTerminalBatch(batch: GenerationBatch) {
  return (
    TERMINAL_BATCH_STATUSES.has(batch.status) ||
    (batch.progress.total_count > 0 &&
      batch.progress.terminal_count === batch.progress.total_count)
  );
}

function hasRequeuedTaskStillProcessing(
  batch: GenerationBatch,
  requeuedTaskIds: Set<string>,
) {
  for (const taskId of requeuedTaskIds) {
    const task = batch.tasks.find((candidate) => candidate.id === taskId);
    if (!task || isRequeuedTaskSettled(task)) {
      requeuedTaskIds.delete(taskId);
    }
  }
  return requeuedTaskIds.size > 0;
}

function isRequeuedTaskSettled(task: GenerationTask) {
  return (
    task.archive_status === "ARCHIVED" ||
    task.archive_status === "DIRECT" ||
    task.status === "FAILED" ||
    task.status === "CANCELLED" ||
    task.status === "SUBMISSION_UNCERTAIN"
  );
}

function statusCountItems(counts: Record<string, number>) {
  return [
    ["成功", counts.succeeded ?? 0],
    ["运行中", counts.running ?? 0],
    ["排队", counts.queued ?? 0],
    ["提交中", counts.submitting ?? 0],
    ["归档中", counts.archiving ?? 0],
    ["失败", counts.failed ?? 0],
    ["取消", counts.cancelled ?? 0],
    ["需要处理", counts.needs_attention ?? 0],
  ] as const;
}

function taskNeedsAttention(task: GenerationTask) {
  if (task.superseded_by_task_id) {
    return false;
  }
  return (
    task.status === "SUBMISSION_UNCERTAIN" ||
    task.archive_status === "ARCHIVE_FAILED"
  );
}

function taskStage(task: GenerationTask) {
  if (
    task.status === "SUCCEEDED" &&
    ["DIRECT", "ARCHIVED"].includes(task.archive_status) &&
    hasGenerationResultSource(task)
  ) {
    return "已完成";
  }
  if (task.stage) {
    return formatStatus(task.stage);
  }
  if (task.archive_status === "ARCHIVE_FAILED") {
    return "归档失败";
  }
  if (task.status === "SUCCEEDED" && task.archive_status === "ARCHIVED") {
    return "已归档";
  }
  return formatStatus(task.status);
}

function formatStatus(status: string) {
  const labels: Record<string, string> = {
    ARCHIVED: "已归档",
    ARCHIVING: "归档中",
    ARCHIVE_FAILED: "归档失败",
    CANCELLED: "已取消",
    COMPLETED: "已归档",
    COMPLETED_WITH_FAILURES: "部分失败",
    FAILED: "失败",
    NEEDS_ATTENTION: "需要处理",
    PENDING: "等待中",
    QUALITY_FAILED: "结果暂不可用",
    QUEUED: "排队中",
    RUNNING: "生成中",
    SUBMISSION_UNCERTAIN: "提交结果待确认",
    SUBMITTING: "提交中",
    SUCCEEDED: "已完成",
  };
  return labels[status] ?? status;
}

function formatDuration(value: number | null | undefined) {
  return value === null || value === undefined ? "—" : `${value.toFixed(1)} 秒`;
}

function estimatedBatchCost(batch: GenerationBatch): number | null {
  if (
    batch.tasks.length === 0 ||
    batch.tasks.some(
      (task) =>
        task.estimated_cost === null || task.estimated_cost === undefined,
    )
  ) {
    return null;
  }
  return Number(
    batch.tasks
      .reduce((total, task) => total + (task.estimated_cost ?? 0), 0)
      .toFixed(6),
  );
}

function formatCost(value: number | null | undefined) {
  return value === null || value === undefined
    ? "待回填"
    : `¥${value.toFixed(2)}`;
}

function batchStorageKey(currentUserId?: string): string {
  return currentUserId
    ? `${BATCH_STORAGE_KEY_PREFIX}${encodeURIComponent(currentUserId)}`
    : BATCH_STORAGE_KEY;
}

function readStoredBatchId(storageKey: string): string | null {
  try {
    return window.localStorage.getItem(storageKey);
  } catch {
    return null;
  }
}

function storeBatchId(storageKey: string, batchId: string): void {
  try {
    window.localStorage.setItem(storageKey, batchId);
  } catch {
    // The active batch remains available in memory when browser storage is blocked.
  }
}

function clearStoredBatchId(storageKey: string): void {
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // A blocked storage backend must not interrupt task navigation or polling.
  }
}
