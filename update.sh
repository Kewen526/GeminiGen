#!/bin/bash
# GeminiGen 服务器一键更新 —— 通过 GitHub API 下载 zip，无需 git 网络访问
# 用法:
#   bash update.sh                         # 使用公开仓库，无 token
#   GITHUB_TOKEN=xxx bash update.sh        # 私有仓库或提高速率限制
#   bash update.sh main                    # 指定分支（默认 main）
set -euo pipefail

REPO_OWNER="Kewen526"
REPO_NAME="GeminiGen"
BRANCH="${1:-main}"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP_ZIP="/tmp/geminigen_deploy_$$.zip"
TMP_DIR="/tmp/geminigen_extract_$$"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERR]${NC}  $1"; exit 1; }

cleanup() { rm -rf "$TMP_ZIP" "$TMP_DIR" 2>/dev/null || true; }
trap cleanup EXIT

echo ""
echo "============================================================"
echo "  GeminiGen — GitHub API 一键部署"
echo "  仓库: ${REPO_OWNER}/${REPO_NAME}  分支: ${BRANCH}"
echo "============================================================"
echo ""

# ── 构造请求头（有 token 则带上，提升速率限制 5000/h vs 60/h）
CURL_OPTS=(-L --silent --show-error --fail --max-time 120)
if [ -n "${GITHUB_TOKEN:-}" ]; then
    CURL_OPTS+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
    info "使用 GITHUB_TOKEN 认证"
else
    warn "未设置 GITHUB_TOKEN，使用匿名访问（限速 60次/h）"
fi

# ── 1. 获取最新 commit SHA（顺便验证 API 可达 + 分支存在）
info "查询 ${BRANCH} 分支最新提交..."
API_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/commits/${BRANCH}"
COMMIT_INFO=$(curl "${CURL_OPTS[@]}" \
    -H "Accept: application/vnd.github.v3+json" \
    "$API_URL" 2>&1) || error "GitHub API 无法访问: $COMMIT_INFO"

COMMIT_SHA=$(echo "$COMMIT_INFO" | grep '"sha"' | head -1 | sed 's/.*"sha": *"\([^"]*\)".*/\1/')
COMMIT_MSG=$(echo "$COMMIT_INFO" | grep '"message"' | head -1 | sed 's/.*"message": *"\([^"]*\)".*/\1/' | cut -c1-60)
info "最新提交: ${COMMIT_SHA:0:8}  ${COMMIT_MSG}"

# ── 2. 下载 zip 包
ZIP_URL="https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/zipball/${BRANCH}"
info "下载代码包..."
curl "${CURL_OPTS[@]}" \
    -H "Accept: application/vnd.github.v3+json" \
    -o "$TMP_ZIP" \
    "$ZIP_URL" || error "下载失败，请检查网络或 GITHUB_TOKEN"

ZIP_SIZE=$(du -sh "$TMP_ZIP" | cut -f1)
success "下载完成（$ZIP_SIZE）"

# ── 3. 解压
info "解压代码包..."
mkdir -p "$TMP_DIR"
unzip -q "$TMP_ZIP" -d "$TMP_DIR" || error "解压失败"

# GitHub zip 解压后目录名为 Owner-Repo-SHA 格式
EXTRACTED=$(ls "$TMP_DIR" | head -1)
SRC_DIR="${TMP_DIR}/${EXTRACTED}"
[ -d "$SRC_DIR" ] || error "解压目录结构异常: $TMP_DIR"
success "解压到 ${SRC_DIR}"

# ── 4. 同步文件（保留 .env / .venv / 日志 / 临时文件）
info "同步代码到 ${DEPLOY_DIR}..."
rsync -a --checksum \
    --exclude='.env' \
    --exclude='.venv/' \
    --exclude='*.log' \
    --exclude='*.pid' \
    --exclude='platform_temp/' \
    --exclude='playwright_profile_*' \
    --exclude='.deps_installed' \
    --exclude='.db_initialized' \
    --exclude='商家实拍图/' \
    "${SRC_DIR}/" "${DEPLOY_DIR}/" || error "rsync 失败，请安装: yum install -y rsync"

success "代码同步完成"

# ── 5. 执行 deploy.sh（安装依赖 + 数据库迁移 + 重启服务）
info "执行部署流程..."
cd "$DEPLOY_DIR"
bash deploy.sh

echo ""
success "✅ 部署完成！当前版本: ${COMMIT_SHA:0:8}"
echo ""
