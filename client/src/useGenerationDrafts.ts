import type { GenerationBatchInput } from "./api";

/**
 * 生成批次幂等记录（上线前检查 P2-1 收敛）。
 *
 * 本文件原名「useGenerationDrafts」，曾是生成草稿的巨型 Hook——那部分自
 * studio 模块化后已无调用方（#160/#179 修复后的残留副本，且仍带着
 * 「时长折叠 4/15 两档」的旧门禁，一旦被复用会带回「8 秒发成 4 秒」的
 * bug），2026-09-23 整体删除。这里只保留 live.ts 仍在用的批次幂等三函数
 * 与其依赖；文件名不改，避免无谓的 import churn。
 */

export type IdempotencyRecord = {
  fingerprint: string;
  key: string;
  request: GenerationBatchInput;
};

const sessionIdempotencyRecords = new Map<string, IdempotencyRecord>();

function requestFingerprint(
  request: Omit<GenerationBatchInput, "idempotency_key">,
): string {
  return JSON.stringify(request);
}

export function restoreIdempotencyRecord(
  storageKey: string,
): IdempotencyRecord | null {
  const memoryRecord = sessionIdempotencyRecords.get(storageKey);
  if (memoryRecord) {
    return memoryRecord;
  }
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (!saved) {
      return null;
    }
    const parsed: unknown = JSON.parse(saved);
    if (isIdempotencyRecord(parsed)) {
      sessionIdempotencyRecords.set(storageKey, parsed);
      return parsed;
    }
  } catch {
    // Recovery also works from the session map when browser storage is blocked.
  }
  return null;
}

export function restoreOrCreateIdempotencyRecord(
  storageKey: string,
  request: Omit<GenerationBatchInput, "idempotency_key">,
  memoryRecord: IdempotencyRecord | null,
  preferredKey?: string,
): IdempotencyRecord | null {
  const fingerprint = requestFingerprint(request);
  if (memoryRecord) {
    return memoryRecord.fingerprint === fingerprint ? memoryRecord : null;
  }
  const savedRecord = restoreIdempotencyRecord(storageKey);
  if (savedRecord) {
    return savedRecord.fingerprint === fingerprint ? savedRecord : null;
  }
  const key = preferredKey ?? createIdempotencyKey();
  const record = {
    fingerprint,
    key,
    request: { ...request, idempotency_key: key },
  };
  sessionIdempotencyRecords.set(storageKey, record);
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(record));
  } catch {
    // Keep the in-memory record so an offline retry still reuses the key.
  }
  return record;
}

export function clearIdempotencyRecord(
  storageKey: string,
  record: IdempotencyRecord,
) {
  if (sessionIdempotencyRecords.get(storageKey)?.key === record.key) {
    sessionIdempotencyRecords.delete(storageKey);
  }
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (!saved) {
      return;
    }
    const parsed: unknown = JSON.parse(saved);
    if (isIdempotencyRecord(parsed) && parsed.key === record.key) {
      window.localStorage.removeItem(storageKey);
    }
  } catch {
    // The remote batch is already visible; cleanup must not hide the result.
  }
}

// 测试专用：清空模块级会话幂等记录，使「重开页面」场景真实模拟刷新
// （模块重载、内存 Map 归零），迫使恢复链走 localStorage 读取→校验→
// 还原路径。运行时代码不应调用。
export function __resetSessionIdempotencyRecordsForTests() {
  sessionIdempotencyRecords.clear();
}

function isIdempotencyRecord(value: unknown): value is IdempotencyRecord {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const record = value as Partial<IdempotencyRecord>;
  const request = record.request as Partial<GenerationBatchInput> | undefined;
  if (
    typeof record.fingerprint !== "string" ||
    typeof record.key !== "string" ||
    !request ||
    request.idempotency_key !== record.key ||
    typeof request.quantity !== "number" ||
    (typeof request.prompt_version_id === "string") ===
      (typeof request.prompt_text === "string") ||
    typeof request.first_frame_asset_id !== "string" ||
    typeof request.output_duration_seconds !== "number" ||
    (request.resolution !== "768P" && request.resolution !== "2K") ||
    (request.provider !== "fake_h3" && request.provider !== "metaso") ||
    (request.fake_audio_quality !== "ok" &&
      request.fake_audio_quality !== "missing")
  ) {
    return false;
  }
  const { idempotency_key: _key, ...requestWithoutKey } = request;
  return (
    record.fingerprint ===
    requestFingerprint(
      requestWithoutKey as Omit<GenerationBatchInput, "idempotency_key">,
    )
  );
}

function createIdempotencyKey(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `batch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
