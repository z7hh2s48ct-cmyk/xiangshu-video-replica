const inMemoryKeys = new Map<string, string>();

function importStorageKey(accountId: string, actionKey: string) {
  return `studio:viral-import:${accountId}:${actionKey}`;
}

export class ViralImportPollingTimeoutError extends Error {}

export function viralImportIdempotencyKey(
  accountId: string,
  actionKey: string,
): string {
  const storageKey = importStorageKey(accountId, actionKey);
  try {
    const stored = window.sessionStorage.getItem(storageKey);
    if (stored) return stored;
    const created = crypto.randomUUID();
    window.sessionStorage.setItem(storageKey, created);
    return created;
  } catch {
    const stored = inMemoryKeys.get(storageKey);
    if (stored) return stored;
    const created = crypto.randomUUID();
    inMemoryKeys.set(storageKey, created);
    return created;
  }
}

export function clearViralImportIdempotencyKey(
  accountId: string,
  actionKey: string,
  expectedKey: string,
) {
  const storageKey = importStorageKey(accountId, actionKey);
  try {
    if (window.sessionStorage.getItem(storageKey) === expectedKey) {
      window.sessionStorage.removeItem(storageKey);
    }
  } catch {
    // The in-memory fallback below still keeps this tab correct.
  }
  if (inMemoryKeys.get(storageKey) === expectedKey) {
    inMemoryKeys.delete(storageKey);
  }
}

export function shouldClearViralImportIdempotencyKey(error: unknown) {
  if (error instanceof ViralImportPollingTimeoutError) return true;
  if (!error || typeof error !== "object") return false;
  if ("retryable" in error && error.retryable === false) return true;
  if (!("status" in error) || typeof error.status !== "number") return false;
  return error.status >= 400 && error.status < 500;
}
