#!/bin/bash
# GeminiGen 服务器一键更新并部署（容错版）
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "[INFO] 当前目录: $REPO_DIR"

# 目标分支（默认 main）
REQ_BRANCH="${1:-main}"

if ! git remote get-url origin >/dev/null 2>&1; then
  echo "[ERR] 当前目录不是有效的 Git 仓库或缺少 origin"
  exit 1
fi

echo "[INFO] 拉取远端分支列表..."
git fetch --all --prune

if git show-ref --verify --quiet "refs/remotes/origin/${REQ_BRANCH}"; then
  BRANCH="$REQ_BRANCH"
else
  echo "[WARN] origin/${REQ_BRANCH} 不存在，自动回退到 origin/main"
  BRANCH="main"
  if ! git show-ref --verify --quiet "refs/remotes/origin/main"; then
    echo "[ERR] origin/main 也不存在，请检查远端仓库"
    exit 1
  fi
fi

if [ -n "$(git status --porcelain)" ]; then
  STASH_NAME="autostash-$(date +%Y%m%d-%H%M%S)"
  echo "[WARN] 检测到本地改动，自动暂存: $STASH_NAME"
  git stash push -u -m "$STASH_NAME" >/dev/null
fi

echo "[INFO] 强制同步到 origin/${BRANCH}"
git checkout -B "$BRANCH" "origin/${BRANCH}"

echo "[INFO] 开始部署（会自动创建/修复 .venv 和 systemd）..."
bash deploy.sh

echo "[OK] 更新部署完成"
echo "[INFO] 检查状态: systemctl status geminigen --no-pager"
