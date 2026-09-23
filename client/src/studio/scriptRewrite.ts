import type { StudioDraft } from "./types";

export function resolvePendingRewrite(
  scopeKey: string,
  cloud: StudioDraft["pendingRewrite"],
  local: StudioDraft["pendingRewrite"],
): StudioDraft["pendingRewrite"] {
  const matching = [cloud, local].filter(
    (value): value is NonNullable<typeof value> => value?.scopeKey === scopeKey,
  );
  return matching.sort((a, b) => {
    // A distinct newer request must not be replaced by an older accepted task.
    // For the same request, acceptance is monotonic even if the cloud save lags.
    if (!a.requestKey || a.requestKey !== b.requestKey) {
      const order = (b.startedAt ?? 0) - (a.startedAt ?? 0);
      if (order) return order;
    }
    return Number(Boolean(b.taskId)) - Number(Boolean(a.taskId));
  })[0];
}

export type ScriptRewriteScope = {
  accountId: string;
  projectId: string;
  sourceAssetId: string;
  identityId: string;
  scriptId: string;
  scriptVersion: number;
  text: string;
  instructions?: string;
  resultText?: string;
  profileFingerprint?: string;
};

type InMemoryKeyState = {
  activeFingerprint?: string;
  keys: Map<string, string>;
};

const inMemoryKeys = new Map<string, InMemoryKeyState>();
const ACTIVE_FINGERPRINT = "$active";

function accountStorageKey(accountId: string) {
  return `studio:script-rewrite:${accountId}`;
}

function scopeFingerprint(scope: ScriptRewriteScope) {
  return JSON.stringify([
    scope.projectId,
    scope.sourceAssetId,
    scope.identityId,
    scope.scriptId,
    scope.scriptVersion,
    scope.text,
    scope.instructions ?? "",
    scope.profileFingerprint ?? "",
  ]);
}

function memoryKeys(accountId: string) {
  const storageKey = accountStorageKey(accountId);
  let state = inMemoryKeys.get(storageKey);
  if (!state) {
    state = { keys: new Map() };
    inMemoryKeys.set(storageKey, state);
  }
  return state;
}

function readStoredKeys(accountId: string): Record<string, string> {
  try {
    const decoded = JSON.parse(
      window.sessionStorage.getItem(accountStorageKey(accountId)) ?? "{}",
    );
    return decoded && typeof decoded === "object"
      ? (decoded as Record<string, string>)
      : {};
  } catch {
    return {};
  }
}

function writeStoredKeys(accountId: string, keys: Record<string, string>) {
  try {
    window.sessionStorage.setItem(
      accountStorageKey(accountId),
      JSON.stringify(keys),
    );
  } catch {
    // The in-memory copy still preserves retries for this mount.
  }
}

export function scriptRewriteIdempotencyKey(scope: ScriptRewriteScope): string {
  const fingerprint = scopeFingerprint(scope);
  const stored = readStoredKeys(scope.accountId);
  const memory = memoryKeys(scope.accountId);
  const previousFingerprint =
    stored[ACTIVE_FINGERPRINT] ?? memory.activeFingerprint;
  if (previousFingerprint && previousFingerprint !== fingerprint) {
    delete stored[previousFingerprint];
    memory.keys.delete(previousFingerprint);
  }
  stored[ACTIVE_FINGERPRINT] = fingerprint;
  memory.activeFingerprint = fingerprint;
  if (typeof stored[fingerprint] === "string" && stored[fingerprint]) {
    memory.keys.set(fingerprint, stored[fingerprint]);
    writeStoredKeys(scope.accountId, stored);
    return stored[fingerprint];
  }
  const existing = memory.keys.get(fingerprint);
  if (existing && previousFingerprint === fingerprint) {
    writeStoredKeys(scope.accountId, stored);
    return existing;
  }
  const created = crypto.randomUUID();
  memory.keys.set(fingerprint, created);
  stored[fingerprint] = created;
  writeStoredKeys(scope.accountId, stored);
  return created;
}

export function clearScriptRewriteIdempotencyKey(
  scope: ScriptRewriteScope,
  expectedKey: string,
) {
  const fingerprint = scopeFingerprint(scope);
  const stored = readStoredKeys(scope.accountId);
  if (stored[fingerprint] === expectedKey) {
    delete stored[fingerprint];
    writeStoredKeys(scope.accountId, stored);
  }
  const memory = memoryKeys(scope.accountId);
  if (memory.keys.get(fingerprint) === expectedKey)
    memory.keys.delete(fingerprint);
}

export function shouldClearScriptRewriteIdempotencyKey(error: unknown) {
  if (!error || typeof error !== "object") return false;
  const candidate = error as {
    code?: string;
    retryable?: boolean;
    status?: number;
  };
  if (candidate.code === "SCRIPT_REWRITE_SUBMISSION_UNCERTAIN") return false;
  if (candidate.retryable === true) return false;
  if (candidate.retryable === false) return true;
  return (
    candidate.status !== undefined &&
    candidate.status >= 400 &&
    candidate.status < 500
  );
}
