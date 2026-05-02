#!/bin/bash
# ============================================================
#  GeminiGen — 通过 GitHub API 更新代码（服务器专用）
#  适用于无法直连 GitHub git 协议的 Alibaba Cloud 服务器
#
#  用法:
#    bash update.sh                          # 更新到默认分支
#    BRANCH=main bash update.sh             # 指定分支
#    GITHUB_TOKEN=xxx bash update.sh        # 私有仓库需要 token
# ============================================================
set -e

REPO="Kewen526/GeminiGen"
BRANCH="${BRANCH:-claude/fix-git-pull-connection-1E8wW}"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
TMP_ZIP="/tmp/geminigen_update_$$.zip"
TMP_DIR="/tmp/geminigen_extract_$$"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
die()   { echo -e "${RED}[ERR]${NC}  $1"; exit 1; }

# ── 1. 下载最新代码 zip ──────────────────────────────────────
info "从 GitHub API 下载分支: ${BRANCH}..."

CURL_OPTS=(-L --fail --silent --show-error --max-time 60)
if [ -n "$GITHUB_TOKEN" ]; then
    CURL_OPTS+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
fi
CURL_OPTS+=(-H "Accept: application/vnd.github+json")
CURL_OPTS+=(-o "$TMP_ZIP")
CURL_OPTS+=("https://api.github.com/repos/${REPO}/zipball/${BRANCH}")

if ! curl "${CURL_OPTS[@]}"; then
    die "GitHub API 下载失败，请检查网络或 GITHUB_TOKEN"
fi

SIZE=$(du -h "$TMP_ZIP" | cut -f1)
ok "下载完成 (${SIZE})"

# ── 2. 解压 ──────────────────────────────────────────────────
info "解压..."
rm -rf "$TMP_DIR" && mkdir -p "$TMP_DIR"
unzip -q "$TMP_ZIP" -d "$TMP_DIR" || die "解压失败（zip 可能损坏或是 HTML 错误页）"

EXTRACTED=$(ls "$TMP_DIR" | head -1)
[ -z "$EXTRACTED" ] && die "解压目录为空"
SRC="$TMP_DIR/$EXTRACTED"
ok "解压目录: $EXTRACTED"

# ── 3. 同步文件（保留运行时文件）────────────────────────────
info "同步代码到 ${DEPLOY_DIR}..."
rsync -a \
    --exclude='.env' \
    --exclude='.venv/' \
    --exclude='*.log' \
    --exclude='server.pid' \
    --exclude='.db_initialized' \
    --exclude='.deps_installed' \
    --exclude='platform_temp/' \
    --exclude='worker_temp/' \
    --exclude='playwright_profile_*/' \
    "$SRC/" "$DEPLOY_DIR/"
ok "代码同步完成"

# ── 4. 数据库迁移 ────────────────────────────────────────────
info "执行数据库迁移..."
if [ -f "${DEPLOY_DIR}/schema.sql" ]; then
    # 读取 DB 配置（从 .env 或默认值）
    DB_HOST="47.95.157.46"; DB_PORT="3306"
    DB_USER="root"; DB_PASSWORD="root@kunkun"; DB_NAME="quote_iw"
    if [ -f "${DEPLOY_DIR}/.env" ]; then
        eval "$(grep -E '^DB_(HOST|PORT|USER|PASSWORD|NAME)=' "${DEPLOY_DIR}/.env" | sed 's/^/export /')"
    fi
    if command -v mysql &>/dev/null; then
        mysql -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" -p"$DB_PASSWORD" "$DB_NAME" \
            < "${DEPLOY_DIR}/schema.sql" \
            && ok "数据库迁移完成" \
            || warn "数据库迁移失败（如已是最新结构可忽略）"
    else
        # 用 Python + pymysql 迁移
        PYTHON="${DEPLOY_DIR}/.venv/bin/python"
        [ -f "$PYTHON" ] || PYTHON=$(command -v python3 || true)
        if [ -n "$PYTHON" ]; then
            "$PYTHON" - <<PYEOF
import pymysql, re
sql = open("${DEPLOY_DIR}/schema.sql", encoding="utf-8").read()
sql = re.sub(r'^\s*USE\s+\S+\s*;\s*$', '', sql, flags=re.MULTILINE|re.IGNORECASE)
conn = pymysql.connect(host="${DB_HOST}", port=${DB_PORT},
    user="${DB_USER}", password="${DB_PASSWORD}", database="${DB_NAME}",
    charset="utf8mb4", connect_timeout=10)
stmts = [s.strip() for s in sql.split(";") if s.strip()]
try:
    with conn.cursor() as c:
        for s in stmts:
            try: c.execute(s)
            except Exception as e:
                if "already exists" not in str(e).lower() and "duplicate" not in str(e).lower():
                    print(f"[WARN] {e}")
    conn.commit()
    print("[OK]   数据库迁移完成")
finally:
    conn.close()
PYEOF
        fi
    fi
else
    warn "schema.sql 不存在，跳过迁移"
fi

# ── 5. 重启服务 ──────────────────────────────────────────────
info "重启 geminigen 服务..."
if systemctl is-active --quiet geminigen 2>/dev/null || systemctl list-units --type=service 2>/dev/null | grep -q geminigen; then
    systemctl restart geminigen
    sleep 4
    if systemctl is-active --quiet geminigen; then
        ok "服务已重启"
    else
        warn "服务重启后状态异常，请检查: journalctl -u geminigen -n 30"
    fi
else
    warn "未找到 systemd 服务，请手动重启"
fi

# ── 6. 健康检查 ──────────────────────────────────────────────
PORT=$(grep '^PORT=' "${DEPLOY_DIR}/.env" 2>/dev/null | cut -d= -f2 || echo "8000")
sleep 2
if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    ok "健康检查通过 ✅  http://localhost:${PORT}/health"
else
    warn "健康检查未通过，服务可能仍在启动中"
    echo "      请稍后运行: curl http://localhost:${PORT}/health"
fi

# ── 清理 ─────────────────────────────────────────────────────
rm -f "$TMP_ZIP"
rm -rf "$TMP_DIR"
ok "更新完成！"
