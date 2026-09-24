# Self-hosted runner on macOS (OrbStack / Lima VM, arm64)

Run the repo's **Linux quality gate** on your own Mac so a PR's CI executes on a
warm, local machine instead of a cold GitHub-hosted runner. The Mac host is
arm64, so we give GitHub Actions a **Linux** environment via a lightweight VM
(OrbStack recommended; Lima also works). `ci.yml`'s `select-runner` job dispatches
the `quality-linux` gate here whenever this runner is **online**, and falls back
to hosted `ubuntu-24.04` when it is not.

> Why a VM and not Docker Desktop directly? The gate needs a full Linux userland
> (apt system libs for Tauri/webkit, systemd-less but real `bash`, `cargo`,
> `ffmpeg`) plus Docker-in-it for the PostgreSQL fixtures. A Linux VM gives the
> exact `ubuntu-24.04` target the hosted runner uses, so results match CI.

## 1. Create the VM

**OrbStack (simplest):**
```bash
brew install orbstack            # or download from orbstack.dev
orb create ubuntu:24.04 video-replica-runner
orb shell video-replica-runner   # drops you into the VM
```

**Lima (alternative):**
```bash
brew install lima
limactl start --name=video-replica-runner template://ubuntu
lima --name=video-replica-runner  # shell into the VM
```

The VM is arm64 Linux. That is fine: the runner registers its own `ARM64` label,
and `select-runner` matches on the custom `video-replica` label only (arch is
never pinned), so arm64 Mac and x64 Windows/WSL2 runners are interchangeable.

## 2. Install Docker inside the VM

OrbStack VMs can share the host Docker; if `docker info` fails inside the VM,
`setup-runner.sh` installs Docker Engine for you (next step). Verify:
```bash
docker info >/dev/null 2>&1 && echo "docker OK" || echo "will be installed by setup-runner.sh"
```

## 3. Clone the repo INSIDE the VM

Clone into the VM's own filesystem (not a mounted host path) for correct
permissions and speed:
```bash
git clone https://github.com/z7hh2s48ct-cmyk/xiangshu-video-replica.git ~/repo
cd ~/repo
```

## 4. Provision the toolchain

```bash
bash scripts/ci/self-hosted-runner/setup-runner.sh
```
Installs Node 24, Python 3.12, uv 0.12.0, Rust stable + rustfmt, ffmpeg, the
Tauri/webkit apt libs, and Docker — the same set the `quality-linux` job needs.
Idempotent; re-run any time.

## 5. Register the runner (interactive token — never stored in the repo)

1. In the repo on GitHub: **Settings → Actions → Runners → New self-hosted runner**.
2. Copy the short-lived registration token.
3. In the VM:
```bash
bash scripts/ci/self-hosted-runner/register-runner.sh
# paste the token when prompted (input is hidden), or one-shot:
#   RUNNER_TOKEN=AXXX... bash scripts/ci/self-hosted-runner/register-runner.sh
```
The runner registers with labels `self-hosted, linux, ARM64, video-replica`.
The token is read from stdin/env, consumed by `config.sh`, and never written to
the repo, logs, or shell history by these scripts.

## 6. Start the runner

```bash
bash scripts/ci/self-hosted-runner/runner-service.sh start
bash scripts/ci/self-hosted-runner/runner-service.sh status   # -> ONLINE
```
Keep the VM (and Mac) awake while you expect PR jobs. For boot-persistent
service (OrbStack VMs have systemd):
```bash
bash scripts/ci/self-hosted-runner/runner-service.sh install-systemd
```

## 7. Use it

- Open/refresh a PR. `select-runner` sees the online `video-replica` runner and
  sets `quality-linux`'s `runs-on` to it. The Linux gate runs locally, warm.
- First job populates npm/uv/cargo/playwright caches; later jobs start near-warm
  (cold 6-8 min → ~30 s).
- Because the gate now runs on your machine, the AGENTS.md pre-PR local full run
  is redundant while the runner is online — **the PR CI *is* the local run**.

### Enable auto-dispatch (one-time read-only PAT secret)

`select-runner` lists the repo's self-hosted runners to find an online
`video-replica` one. That REST call needs an **Administration (read)** scope the
workflow `GITHUB_TOKEN` does **not** have (and `administration` is not a valid
`permissions:` key), so without a token it 403s and the job cleanly falls back to
GitHub-hosted `ubuntu-24.04` — your runner is simply never used. To let PR CI
actually dispatch here, add a read-only PAT **once**:

1. Create a **fine-grained PAT**: Repository access → *only this repo*;
   Permissions → **Administration: Read-only** (nothing else).
2. Repo → Settings → Secrets and variables → Actions → *New repository secret*:
   name `SELF_HOSTED_RUNNER_READ_PAT`, value = the PAT.

`ci.yml` uses `${{ secrets.SELF_HOSTED_RUNNER_READ_PAT || github.token }}`: with
the secret set, online-runner auto-dispatch works; without it, every run stays on
hosted runners (still correct, just not local). The token is read-only,
repo-scoped, and never printed, logged, or committed.

## Escape hatches

- **Stop the runner** (`runner-service.sh stop`) → new PRs auto-fall back to
  hosted `ubuntu-24.04`. No workflow edit needed.
- **Force hosted** without stopping the runner: set repo variable
  `CI_FORCE_HOSTED=true` (Settings → Secrets and variables → Actions → Variables).
- **Tune shard count** for the VM's cores: repo variable `CI_PYTEST_SHARDS`
  (e.g. `6`). The shard script regenerates balanced manifests for that N from
  `scripts/ci/test-durations.json`.

## Security

`ci.yml` keeps the same-repo guard
(`github.event.pull_request.head.repo.full_name == github.repository`) on every
job, so a fork PR can never dispatch untrusted code to this runner. The repo is
private; the runner runs jobs only from it.
