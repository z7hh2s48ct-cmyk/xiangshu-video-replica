import { randomUUID } from "node:crypto";
import {
  lstat,
  open,
  readFile,
  rename,
  unlink,
  writeFile,
} from "node:fs/promises";
import path from "node:path";

export async function restartApi({ timeoutMs = 30_000 } = {}) {
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1) {
    throw new Error("timeoutMs must be a positive integer");
  }
  const requestPath = requiredEnvironmentPath("GATE1_API_RESTART_REQUEST_PATH");
  const completionPath = requiredEnvironmentPath(
    "GATE1_API_RESTART_COMPLETION_PATH",
  );
  const requestId = randomUUID();
  const temporaryPath = `${requestPath}.${requestId}.tmp`;
  await writeFile(
    temporaryPath,
    `${JSON.stringify({ request_id: requestId })}\n`,
    "utf8",
  );
  await rename(temporaryPath, requestPath);

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const completion = await readCompletion(completionPath);
    if (completion?.request_id === requestId) {
      if (completion.status !== "ready") {
        throw new Error(`API restart failed for request ${requestId}`);
      }
      await requireApiHealth();
      return;
    }
    await delay(100);
  }
  throw new Error(`API restart timed out after ${timeoutMs}ms`);
}

export async function withBrokenGenerationArchive(operation) {
  if (typeof operation !== "function") {
    throw new Error("operation must be a function");
  }
  const storageRoot = path.resolve(
    requiredEnvironmentPath("GATE1_STORAGE_ROOT"),
  );
  const archiveDirectory = path.join(storageRoot, "generation-results");
  if (path.relative(storageRoot, archiveDirectory) !== "generation-results") {
    throw new Error("Generation archive path escapes the Gate 1 storage root");
  }
  const storageRootStats = await lstat(storageRoot);
  const archiveStats = await lstat(archiveDirectory);
  if (!storageRootStats.isDirectory() || !archiveStats.isDirectory()) {
    throw new Error(
      "Gate 1 storage and generation archive must be directories",
    );
  }

  const backupDirectory = path.join(
    storageRoot,
    `generation-results.gate1-backup-${randomUUID()}`,
  );
  let archiveMoved = false;
  let blockingFileCreated = false;
  try {
    await rename(archiveDirectory, backupDirectory);
    archiveMoved = true;
    const blockingFile = await open(archiveDirectory, "wx");
    blockingFileCreated = true;
    try {
      await blockingFile.writeFile(
        "Gate 1 intentionally blocks generation result archival.\n",
        "utf8",
      );
    } finally {
      await blockingFile.close();
    }
    return await operation();
  } finally {
    if (blockingFileCreated) {
      await unlink(archiveDirectory);
    }
    if (archiveMoved) {
      await rename(backupDirectory, archiveDirectory);
    }
  }
}

async function readCompletion(completionPath) {
  try {
    return JSON.parse(await readFile(completionPath, "utf8"));
  } catch (error) {
    if (error?.code === "ENOENT" || error instanceof SyntaxError) {
      return null;
    }
    throw error;
  }
}

async function requireApiHealth() {
  const apiUrl = requiredEnvironmentPath("GATE1_API_URL");
  const response = await fetch(`${apiUrl}/health`);
  if (!response.ok) {
    throw new Error(`Restarted API health check failed (${response.status})`);
  }
}

function requiredEnvironmentPath(name) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
