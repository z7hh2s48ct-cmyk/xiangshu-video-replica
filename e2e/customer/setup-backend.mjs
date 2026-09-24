import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..", "..");
const serverDir = path.join(repoRoot, "server");
// The venv layout differs per platform: uv creates Scripts/python.exe on
// Windows and bin/python on Linux/macOS (the CI Linux gate is the latter).
const python =
  process.platform === "win32"
    ? path.join(serverDir, ".venv", "Scripts", "python.exe")
    : path.join(serverDir, ".venv", "bin", "python");

// Honor the repository's PostgreSQL fixture override (TEST_POSTGRESQL_URL is
// what scripts/pg-fixture.sh and the CI postgres service both export); fall
// back to the local default only when it is unset.
const TEST_PG_URL =
  process.env.TEST_POSTGRESQL_URL ??
  "postgresql://testuser:testpass@localhost:5433/customer_v3_test";
const PG_BASE = TEST_PG_URL.slice(0, TEST_PG_URL.lastIndexOf("/"));
const E2E_DB = "customer_e2e";
const API_PORT = process.env.CUSTOMER_E2E_API_PORT ?? "8765";
const WEB_PORT = process.env.CUSTOMER_E2E_WEB_PORT ?? "5173";
export const API_URL = `http://127.0.0.1:${API_PORT}`;
export const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;

/** Plaintext codes the seed script inserts; the spec drives the activation UI
 * with these. Never real codes — throwaway per E2E run. */
const SEED_CODES = [
  "XS04-ABCDEFG-HJKLMNP-QRSTVWX-YZ23456",
  "XS04-2345678-9ABCDEF-GHJKLMN-PQRSTVW",
  "XS04-XYZ2345-6789ABC-DEFGHJK-MNPQRST",
  "XS04-1234567-89ABCDE-FGHJKMN-PQRSTVW",
];

/** Throwaway test keys — never a real secret. */
function testKeys() {
  return {
    ACTIVATION: randomBytes(36).toString("base64url"),
    FINGERPRINT: randomBytes(36).toString("base64url"),
    ADMIN_SESSION: randomBytes(36).toString("base64url"),
    API_KEY: randomBytes(36).toString("base64url"),
    IDEMPOTENCY_AEAD: randomBytes(32).toString("base64url").replace(/=+$/, ""),
  };
}

function runSync(cmd, args, env = {}, cwd = repoRoot) {
  const result = spawnSync(cmd, args, {
    cwd,
    env: { ...process.env, ...env },
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(
      `command failed: ${cmd} ${args.join(" ")}\n${result.stderr || result.stdout}`,
    );
  }
  return result.stdout;
}

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForHealth(url, timeoutMs, what) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(2000) });
      if (res.ok) return;
    } catch {
      // still starting
    }
    await delay(1000);
  }
  throw new Error(`${what} not healthy at ${url} after ${timeoutMs}ms`);
}

export async function setup() {
  const keys = testKeys();
  const dsn = `${PG_BASE}/${E2E_DB}`;
  const adminDsn = `${PG_BASE}/postgres`;

  // The seeded E2E database runs active_storage_provider="local" (see
  // seed_codes.py); this is the matching root for that adapter. Without it
  // every storage-touching route answers 503 STORAGE_PROVIDER_FORBIDDEN.
  const runDir =
    process.env.CUSTOMER_E2E_RUN_DIR ?? path.join(__dirname, "run");
  const storageRoot = path.join(runDir, "storage");
  mkdirSync(storageRoot, { recursive: true });

  // 1. Fresh database.
  runSync(python, [
    "-c",
    `
import psycopg
with psycopg.connect("${adminDsn}", autocommit=True) as c:
    c.execute('DROP DATABASE IF EXISTS "${E2E_DB}" WITH (FORCE)')
    c.execute('CREATE DATABASE "${E2E_DB}"')
`,
  ]);

  // 2. Migrate to head on the dedicated database.
  runSync(
    python,
    [
      "-m",
      "alembic",
      "-c",
      path.join(serverDir, "alembic.ini"),
      "upgrade",
      "head",
    ],
    { VIDEO_REPLICA_DATABASE_URL: dsn },
    serverDir,
  );

  // 3. Seed admin + activation codes + a throwaway ZPay merchant config (the
  // wallet recharge route reads it; seed_codes.py prints the Fernet key the
  // config was encrypted with, and the API process must use the same key to
  // decrypt it — mirrors test_customer_recharge.recharge_config_fixture).
  const seedOutput = runSync(python, [path.join(__dirname, "seed_codes.py")], {
    CUSTOMER_E2E_DATABASE_URL: dsn,
    VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY: keys.ACTIVATION,
    PYTHONPATH: serverDir,
  });
  const settingsKey = (seedOutput.match(
    /VIDEO_REPLICA_SETTINGS_KEY=(.+)\s*$/,
  ) ?? [])[1];
  if (!settingsKey) {
    throw new Error(
      `seed_codes.py did not print a settings key:\n${seedOutput}`,
    );
  }

  // 4. Start the API.
  const apiEnv = {
    ...process.env,
    VIDEO_REPLICA_DATABASE_URL: dsn,
    VIDEO_REPLICA_ACTIVATION_CODE_HMAC_KEY: keys.ACTIVATION,
    VIDEO_REPLICA_DEVICE_FINGERPRINT_HMAC_KEY: keys.FINGERPRINT,
    VIDEO_REPLICA_ADMIN_SESSION_HMAC_KEY: keys.ADMIN_SESSION,
    VIDEO_REPLICA_API_KEY_HMAC_KEY: keys.API_KEY,
    VIDEO_REPLICA_CUSTOMER_IDEMPOTENCY_AEAD_KEY: keys.IDEMPOTENCY_AEAD,
    VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_IP: "100000",
    VIDEO_REPLICA_RATE_LIMIT_ACTIVATE_CODE: "100000",
    VIDEO_REPLICA_RATE_LIMIT_WINDOW_SECONDS: "3600",
    // Recharge lane (PR #65 task #7): the customer wallet view needs the ZPay
    // config + deployment settings to create orders.
    VIDEO_REPLICA_SETTINGS_KEY: settingsKey,
    VIDEO_REPLICA_STORAGE_ROOT: storageRoot,
    PUBLIC_BASE_URL: "https://callback.example.com",
  };
  const api = spawn(
    python,
    [
      "-m",
      "uvicorn",
      "app.main:app",
      "--app-dir",
      serverDir,
      "--host",
      "127.0.0.1",
      "--port",
      API_PORT,
      "--no-proxy-headers",
    ],
    {
      env: apiEnv,
      stdio: ["ignore", "pipe", "pipe"],
      cwd: repoRoot,
    },
  );
  api.stdout?.on("data", () => {});
  api.stderr?.on("data", () => {});

  await waitForHealth(`${API_URL}/health`, 60_000, "API");

  // 5. Start the client dev server pointed at this API.
  const viteBin = path.join(repoRoot, "node_modules", "vite", "bin", "vite.js");
  const web = spawn(
    process.execPath,
    [viteBin, "--port", WEB_PORT, "--strictPort"],
    {
      cwd: path.join(repoRoot, "client"),
      // Browser sessions use same-origin HttpOnly cookies, as in deployment.
      // Route API requests through the existing Vite proxy instead of CORS.
      env: {
        ...process.env,
        VITE_API_BASE_URL: WEB_URL,
        VITE_DEV_API_PROXY_TARGET: API_URL,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  web.stdout?.on("data", () => {});
  web.stderr?.on("data", () => {});

  await waitForHealth(WEB_URL, 60_000, "vite");

  // Persist run state for the specs.
  writeFileSync(
    path.join(runDir, "run.json"),
    `${JSON.stringify(
      {
        apiUrl: API_URL,
        webUrl: WEB_URL,
        codes: SEED_CODES,
      },
      null,
      2,
    )}\n`,
  );

  globalThis.__customerE2E = { api, web };
  return { api, web, apiUrl: API_URL, webUrl: WEB_URL, codes: SEED_CODES };
}

export function teardown() {
  const handles = globalThis.__customerE2E;
  if (handles) {
    handles.api?.kill();
    handles.web?.kill();
  }
}
