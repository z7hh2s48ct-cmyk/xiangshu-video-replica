#!/usr/bin/env bash
set -euo pipefail

scan_paths=(
  "server/app"
  "server/migrations"
  "client/src"
  "client/src-tauri"
  "package.json"
  "client/package.json"
  "server/pyproject.toml"
  "packaging_tools"
  "scripts"
  "e2e"
  "docs"
  "deploy"
)

patterns=(
  # sk- 前必须是行首或非词字符（ERE 无后行断言，用捕获组锚定）：
  # 避免 task-warning--toolbar 一类 CSS 类名里的 “ta**sk-**warning…” 子串
  # 误报；真实密钥前必是引号/空白/行首，不会嵌在标识符中间。
  '(^|[^A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}'
  'AKIA[0-9A-Z]{16}'
  '-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'
  '(api[_-]?key|secret[_-]?access[_-]?key|authorization|bearer|token|password)[[:space:]]*[:=][[:space:]]*["'\''][^"'\'']{8,}["'\'']'
)

# Runtime bundles must not contain code-shaped activation credentials or
# already-signed cloud URLs. Tests may use generated/fake credentials, but a
# value in code shipped to customers is a release blocker even when it does
# not match a conventional API-key prefix.
runtime_secret_paths=(
  "server/app"
  "client/src"
  "client/src-tauri"
  "packaging_tools"
  "deploy"
  ":(exclude,glob)client/src/**/*.test.ts"
  ":(exclude,glob)client/src/**/*.test.tsx"
)

runtime_patterns=(
  'XS[0-9]{2}(-[0-9A-HJKMNP-TV-Z]{7}){4}'
  '(X-Amz-Signature|q-signature|Signature)=[A-Fa-f0-9%]{16,}'
)

for pattern in "${patterns[@]}"; do
  if git grep -n -I -E --untracked --no-exclude-standard \
    -e "$pattern" -- "${scan_paths[@]}"; then
    echo "Potential secret detected by pattern: $pattern" >&2
    exit 1
  elif [[ $? -gt 1 ]]; then
    echo "Secret scan failed while evaluating pattern: $pattern" >&2
    exit 2
  fi
done

for pattern in "${runtime_patterns[@]}"; do
  runtime_matches=""
  if runtime_matches="$(git grep -n -I -E --untracked --no-exclude-standard \
    -e "$pattern" -- "${runtime_secret_paths[@]}")"; then
    while IFS= read -r match; do
      # UI/docs may show the canonical all-X placeholder; it carries no
      # entropy and cannot redeem a code, so it is not credential material.
      candidate="$match"
      if [[ "$pattern" == 'XS[0-9]{2}(-[0-9A-HJKMNP-TV-Z]{7}){4}' ]]; then
        candidate="${candidate//XS04-XXXXXXX-XXXXXXX-XXXXXXX-XXXXXXX/}"
        if ! [[ "$candidate" =~ XS[0-9]{2}(-[0-9A-HJKMNP-TV-Z]{7}){4} ]]; then
          continue
        fi
      fi
      echo "$match"
      echo "Potential runtime credential or signed URL detected by pattern: $pattern" >&2
      exit 1
    done <<< "$runtime_matches"
  elif [[ $? -gt 1 ]]; then
    echo "Runtime credential scan failed while evaluating pattern: $pattern" >&2
    exit 2
  fi
done

deploy_token_matches=""
if deploy_token_matches="$(
  git grep -n -I -E --untracked --no-exclude-standard \
    -e 'proxy_set_header[[:space:]]+X-Control-Proxy-Token' -- deploy
)"; then
  while IFS= read -r match; do
    if [[ "$match" == *'proxy_set_header X-Control-Proxy-Token "";'* ]]; then
      continue
    fi
    if [[ "$match" == *.example:*'proxy_set_header X-Control-Proxy-Token "REPLACE_WITH_32_BYTE_RANDOM_TOKEN";'* ]]; then
      continue
    fi
    echo "Unexpected raw control proxy token in deployment file: $match" >&2
    exit 1
  done <<< "$deploy_token_matches"
else
  deploy_token_scan_status=$?
  if [[ $deploy_token_scan_status -ne 1 ]]; then
    echo "Deployment token scan failed." >&2
    exit 2
  fi
fi

echo "No hardcoded secrets detected in runtime contract surface."
