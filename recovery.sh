#!/bin/bash
# 修复错误 upstream 配置 + 强制同步 main + 重新部署
set -euo pipefail
cd "$(dirname "$0")"

git fetch origin --prune

# 修复 main 的 upstream 指向
if git show-ref --verify --quiet refs/heads/main; then
  git checkout main
else
  git checkout -b main origin/main
fi

git branch --unset-upstream 2>/dev/null || true
git branch --set-upstream-to=origin/main main

# 可选：暂存本地改动避免冲突
if [ -n "$(git status --porcelain)" ]; then
  git stash push -u -m "recovery-autostash-$(date +%Y%m%d-%H%M%S)" >/dev/null
fi

# 强制对齐远端 main
git reset --hard origin/main

bash deploy.sh
