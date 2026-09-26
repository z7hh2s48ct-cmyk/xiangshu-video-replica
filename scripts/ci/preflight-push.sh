#!/usr/bin/env bash
# Push 前预检：一键跑完《并行开发迁移Head与PR冲突处置手册》§5 清单的免-PG 部分。
#
# 用法：
#   bash scripts/ci/preflight-push.sh              # 全部检查（含改动文件 ruff）
#   bash scripts/ci/preflight-push.sh --skip-ruff  # 跳过 ruff（只跑 git + 迁移 + 分片）
#   bash scripts/ci/preflight-push.sh --help
#
# 本脚本只读、只报告——不自动 merge、不 push、不改任何文件，处置权在你。
# 落后于 origin/main 时只提示 `git merge origin/main`（手册 §3），绝不 rebase
# （AGENTS.md 禁 force push；squash merge 下 merge commit 不进 main，无副作用）。
#
# 不含全量 pytest / 客户 E2E / 构建（耗时长、需 PG）——那些是收尾门禁，用
# `npm run check:sharded`。本脚本只拦 push 前 30 秒能发现的问题（手册 §0 铁律 3：
# 预检不过不 push——CI 分片一轮 17 分钟，本地 30 秒能拦住的问题不要交给 CI）。
#
# Portable: bash 3.2 (macOS) safe — no mapfile/readarray, no associative arrays.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "${REPO_ROOT}"

# ---- 输出 helpers（终端支持时着色，重定向到文件时自动去色）----
if [ -t 1 ]; then
  RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'
  BLUE=$'\033[0;34m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; BLUE=""; BOLD=""; DIM=""; RESET=""
fi

log()  { printf '%s\n' "$*"; }
ok()   { printf '  %s[OK]%s   %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s[WARN]%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '  %s[FAIL]%s %s\n' "$RED" "$RESET" "$*"; }
step() { printf '\n%s%s==>%s %s\n' "$BLUE" "$BOLD" "$RESET" "$*"; }

usage() {
  cat <<'EOF'
Push 前预检：一键跑完《并行开发迁移Head与PR冲突处置手册》§5 清单的免-PG 部分。

用法：
  bash scripts/ci/preflight-push.sh              全部检查（含改动文件 ruff）
  bash scripts/ci/preflight-push.sh --skip-ruff  跳过 ruff（只跑 git + 迁移 + 分片）
  bash scripts/ci/preflight-push.sh --help       显示本帮助

检查项（对应手册 §5）：
  1. git fetch origin --prune
  2. 与 origin/main 的 behind/ahead；落后则提示 git merge origin/main（绝不 rebase）
  3. 迁移链守卫 migration_manifest.py --check（免 PG，单头 + 冻结常量）
  4. 分片清单覆盖 build-test-shards.py --check-coverage
  5. 本分支改动 server .py 文件的 ruff check + format --check

只读、只报告——不自动 merge、不 push、不改任何文件。
不含全量 pytest / E2E / 构建（收尾门禁用 npm run check:sharded）。
EOF
}

FAILURES=0
WARNINGS=0
SKIP_RUFF=0

for arg in "$@"; do
  case "$arg" in
    --skip-ruff) SKIP_RUFF=1 ;;
    -h|--help) usage; exit 0 ;;
    *) log "未知参数: ${arg}（--help 看用法）" >&2; exit 2 ;;
  esac
done

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  fail "不在 git 仓库内"; exit 2
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
log "${BOLD}Push 前预检${RESET}  分支: ${BOLD}${BRANCH}${RESET}  仓库: ${DIM}${REPO_ROOT}${RESET}"

if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "HEAD" ]; then
  warn "当前在 ${BRANCH}——AGENTS.md §1 禁止在 main 直接开发/推送，请切到任务分支"
  WARNINGS=$((WARNINGS+1))
fi

# ---- Step 1: fetch ----
step "Step 1/5  git fetch origin --prune"
if git fetch origin --prune >/dev/null 2>&1; then
  ok "fetch 成功"
else
  fail "fetch 失败（网络/认证？）——后续 behind/ahead 判断可能不准"
  FAILURES=$((FAILURES+1))
fi

# ---- Step 2: 与 origin/main 同步状态 ----
step "Step 2/5  与 origin/main 的同步状态"
if ! git rev-parse --verify origin/main >/dev/null 2>&1; then
  fail "origin/main 不存在（远程未配置？）"
  FAILURES=$((FAILURES+1))
else
  BEHIND="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo '?')"
  AHEAD="$(git rev-list --count origin/main..HEAD 2>/dev/null || echo '?')"
  log "  领先 origin/main: ${BOLD}${AHEAD}${RESET} 个提交   落后: ${BOLD}${BEHIND}${RESET} 个提交"
  if [ "$BEHIND" != "0" ] && [ "$BEHIND" != "?" ]; then
    warn "落后 origin/main ${BEHIND} 个提交——push 前先: ${BOLD}git merge origin/main${RESET}（手册 §3）"
    warn "  绝不 rebase（AGENTS.md 禁 force push）；merge 后冲突按手册 §2 表逐家族解决"
    WARNINGS=$((WARNINGS+1))
  else
    ok "未落后于 origin/main（behind=0）"
  fi
  log "  本分支提交（origin/main..HEAD，须${BOLD}只含本任务${RESET}，AGENTS.md §3）："
  if [ "$AHEAD" = "0" ]; then
    log "    (无独有提交——分支与 origin/main 同点)"
  else
    git log origin/main..HEAD --oneline 2>/dev/null | sed 's/^/    /'
  fi
fi

# ---- Step 3: 迁移链守卫 ----
step "Step 3/5  迁移链守卫  migration_manifest.py --check"
if [ -f scripts/ci/migration_manifest.py ]; then
  if python3 scripts/ci/migration_manifest.py --check 2>&1 | sed 's/^/  /'; then
    ok "迁移守卫通过（单头 + 冻结常量无漂移）"
  else
    fail "迁移守卫失败——若分支加了迁移：重挂 down_revision / --record / 重探针（手册 §3-§4）"
    FAILURES=$((FAILURES+1))
  fi
else
  warn "scripts/ci/migration_manifest.py 不存在，跳过"
fi

# ---- Step 4: 分片清单覆盖 ----
step "Step 4/5  分片清单覆盖  build-test-shards.py --check-coverage"
if [ -f scripts/ci/build-test-shards.py ]; then
  if python3 scripts/ci/build-test-shards.py --check-coverage 2>&1 | sed 's/^/  /'; then
    ok "分片覆盖通过（每个测试文件都在某个 shard）"
  else
    fail "分片覆盖失败——新增了测试文件？跑 build-test-shards.py --shards 4 再生（手册 §2 #6）"
    FAILURES=$((FAILURES+1))
  fi
else
  warn "scripts/ci/build-test-shards.py 不存在，跳过"
fi

# ---- Step 5: ruff（改动 server .py 文件）----
step "Step 5/5  ruff（本分支改动的 server .py 文件）"
if [ "$SKIP_RUFF" = "1" ]; then
  warn "已跳过 ruff（--skip-ruff）——未做改动文件的静态检查"
  WARNINGS=$((WARNINGS+1))
else
  MERGE_BASE="$(git merge-base origin/main HEAD 2>/dev/null || echo '')"
  if [ -z "$MERGE_BASE" ]; then
    warn "无法求 merge-base，跳过 ruff"
  else
    CHANGED_PY="$(git diff --name-only --diff-filter=ACMR "$MERGE_BASE" HEAD -- server/ 2>/dev/null | grep '\.py$' || true)"
    if [ -z "$CHANGED_PY" ]; then
      ok "本分支无 server .py 改动，跳过 ruff"
    else
      CNT="$(printf '%s\n' "$CHANGED_PY" | grep -c . || echo 0)"
      log "  改动 .py 文件 ${CNT} 个，跑 ruff check + format --check ..."
      # shellcheck disable=SC2086  # 故意不加引号：按行 word-split 成多个文件参数
      if uv --cache-dir .uv-cache run --project server --locked ruff check $CHANGED_PY 2>&1 | sed 's/^/  /' && \
         uv --cache-dir .uv-cache run --project server --locked ruff format --check $CHANGED_PY 2>&1 | sed 's/^/  /'; then
        ok "ruff 通过"
      else
        fail "ruff 失败——CI Linux 门第一道就是它，先修（ruff check --fix / ruff format）"
        FAILURES=$((FAILURES+1))
      fi
    fi
  fi
fi

# ---- 汇总 ----
step "预检汇总"
log "  失败: ${BOLD}${FAILURES}${RESET}   警告: ${BOLD}${WARNINGS}${RESET}"
if [ "$FAILURES" -eq 0 ]; then
  if [ "$WARNINGS" -eq 0 ]; then
    ok "全部通过——可以 push"
    log "  ${DIM}push 后再核对 PR 的 Commits 列表（AGENTS.md §3）；收尾全量门禁用 npm run check:sharded${RESET}"
  else
    warn "无失败，但有 ${WARNINGS} 项警告——处理后 push 更稳妥"
  fi
  exit 0
else
  fail "有 ${FAILURES} 项失败——修复后再 push（手册 §0 铁律 3：预检不过不 push）"
  exit 1
fi
