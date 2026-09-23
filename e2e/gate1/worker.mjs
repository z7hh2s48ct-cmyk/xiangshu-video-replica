import { spawn } from "node:child_process";
import { appendFile } from "node:fs/promises";
import path from "node:path";

import { requiredRunDir } from "./evidence.mjs";

export async function runWorkerOnce({
  label,
  maxTasks = 1,
  fakeH3Outcome = "ok",
  timeoutMs = 60_000,
}) {
  if (!Number.isInteger(maxTasks) || maxTasks < 1) {
    throw new Error("maxTasks must be a positive integer");
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1) {
    throw new Error("timeoutMs must be a positive integer");
  }
  const command = parseWorkerCommand();
  const runDir = requiredRunDir();
  const logPath = path.join(runDir, "logs", "worker.log");
  const args = [...command.slice(1), "--max-tasks", String(maxTasks)];
  const env = {
    ...process.env,
    VIDEO_REPLICA_FAKE_H3_OUTCOME: fakeH3Outcome,
  };
  const output = [];
  const controller = new AbortController();
  const child = spawn(command[0], args, {
    cwd: process.cwd(),
    env,
    stdio: ["ignore", "pipe", "pipe"],
    signal: controller.signal,
  });
  child.stdout.on("data", (chunk) => output.push(chunk));
  child.stderr.on("data", (chunk) => output.push(chunk));
  let timedOut = false;
  const result = await new Promise((resolve) => {
    let settled = false;
    const settle = (value) => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        resolve(value);
      }
    };
    const timeout = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    child.once("error", (error) => settle({ exitCode: 1, error }));
    child.once("close", (code) => settle({ exitCode: code ?? 1, error: null }));
  });
  const header = `\n[worker:${label}] outcome=${fakeH3Outcome} max_tasks=${maxTasks} exit=${result.exitCode} timed_out=${timedOut}\n`;
  await appendFile(logPath, Buffer.concat([Buffer.from(header), ...output]));
  if (timedOut) {
    throw new Error(`Worker ${label} timed out after ${timeoutMs}ms`);
  }
  if (result.error) {
    throw result.error;
  }
  if (result.exitCode !== 0) {
    throw new Error(`Worker ${label} failed with exit code ${result.exitCode}`);
  }
}

function parseWorkerCommand() {
  const raw = process.env.GATE1_WORKER_COMMAND?.trim();
  if (!raw) {
    throw new Error("GATE1_WORKER_COMMAND is required");
  }
  const command = JSON.parse(raw);
  if (
    !Array.isArray(command) ||
    command.length === 0 ||
    command.some((part) => typeof part !== "string" || !part)
  ) {
    throw new Error("GATE1_WORKER_COMMAND must be a JSON string array");
  }
  return command;
}
