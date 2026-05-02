#!/bin/bash
# GeminiGen API 服务器启动脚本
# 服务器上运行，无浏览器/Playwright

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo " GeminiGen API Server"
echo "============================================================"

# 加载 .env（如果存在）
if [ -f ".env" ]; then
    echo "[INFO] 加载 .env 配置..."
    export $(grep -v '^#' .env | xargs)
fi

# 检查 Python
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] 未找到 python3"
    exit 1
fi

# 安装依赖
if [ ! -f ".deps_installed" ]; then
    echo "[INFO] 安装依赖..."
    pip3 install -r requirements_platform.txt
    touch .deps_installed
fi

# 初始化数据库表（首次运行）
if [ ! -f ".db_initialized" ]; then
    echo "[INFO] 初始化数据库..."
    if [ -n "$DB_HOST" ]; then
        mysql -h "$DB_HOST" -P "${DB_PORT:-3306}" -u "$DB_USER" -p"$DB_PASSWORD" < schema.sql \
            && touch .db_initialized \
            || echo "[WARN] 数据库初始化失败，请手动执行 schema.sql"
    else
        echo "[WARN] 未配置 DB_HOST，请手动执行 schema.sql"
    fi
fi

echo "[INFO] 启动 API 服务器..."
echo "[INFO] 访问地址: http://0.0.0.0:${PORT:-8000}"
echo "[INFO] 按 Ctrl+C 停止"
echo ""

# 使用 nohup 后台运行，或前台运行
if [ "$1" = "--daemon" ]; then
    nohup python3 -m app.main > server.log 2>&1 &
    echo "[INFO] 已在后台启动，PID=$!"
    echo "[INFO] 日志: tail -f server.log"
else
    python3 -m app.main
fi
