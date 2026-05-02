#!/bin/bash
# ============================================================
#  GeminiGen 平台 — 服务器一键部署脚本
#  用法: bash deploy.sh
# ============================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[OK]${NC}   $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERR]${NC}  $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "============================================================"
echo "  GeminiGen 平台 — 一键部署"
echo "============================================================"
echo ""

# ============================================================
# 1. 检查 Python（优先 python3.11，避免读到系统旧版本）
# ============================================================
info "检查 Python..."
BASE_PYTHON=$(command -v python3.11 || command -v python3.10 || command -v python3 || true)
[ -z "$BASE_PYTHON" ] && error "未找到 Python 3，请先安装 python3.11"

PY_VER=$($BASE_PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    error "Python $PY_VER 版本过低（需要 3.10+），请安装 python3.11 后重试"
fi
success "Python: $BASE_PYTHON ($PY_VER)"

# ============================================================
# 2. 安装系统工具
# ============================================================
info "安装系统工具..."
yum install -y git curl mysql 2>/dev/null || apt-get install -y git curl mysql-client 2>/dev/null || true

# ============================================================
# 3. 创建 virtualenv（隔离依赖，不污染系统 Python 环境）
# ============================================================
VENV_DIR="${SCRIPT_DIR}/.venv"
info "配置虚拟环境..."
if [ ! -f "${VENV_DIR}/bin/activate" ]; then
    $BASE_PYTHON -m venv "$VENV_DIR" || error "创建 venv 失败，请确认 python3-venv 已安装"
    success "虚拟环境已创建: ${VENV_DIR}"
else
    success "虚拟环境已存在，跳过创建"
fi

PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

info "安装 Python 依赖包（约 1-2 分钟）..."
$PIP install --quiet --upgrade pip
$PIP install --quiet -r requirements_platform.txt
success "依赖安装完成"

# ============================================================
# 4. 生成 .env（已存在则跳过）
# ============================================================
if [ -f ".env" ]; then
    warn ".env 已存在，跳过（如需重建请先删除 .env）"
else
    info "生成 .env 配置..."
    SECRET_KEY=$($PYTHON -c "import secrets; print(secrets.token_hex(32))")
    cat > .env <<EOF
# GeminiGen API Server 配置（自动生成）
SECRET_KEY=${SECRET_KEY}

DB_HOST=47.95.157.46
DB_PORT=3306
DB_USER=root
DB_PASSWORD=root@kunkun
DB_NAME=quote_iw

HOST=0.0.0.0
PORT=8000

WORKER_COUNT=0
GEMINIGEN_USERNAME=
GEMINIGEN_PASSWORD=
EOF
    success ".env 已生成"
fi

# ============================================================
# 5. 初始化数据库表
# ============================================================
info "初始化数据库表..."
if command -v mysql &>/dev/null; then
    mysql -h 47.95.157.46 -P 3306 -u root -p'root@kunkun' < schema.sql \
        && success "数据库表初始化完成" \
        || warn "数据库初始化失败，请手动执行: mysql < schema.sql"
else
    $PYTHON - <<'PYEOF'
import pymysql, re
sql_file = "schema.sql"
with open(sql_file, "r", encoding="utf-8") as f:
    content = f.read()
content = re.sub(r'^\s*USE\s+\S+\s*;\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)
conn = pymysql.connect(
    host="47.95.157.46", port=3306, user="root", password="root@kunkun",
    database="quote_iw", charset="utf8mb4", connect_timeout=10,
)
statements = [s.strip() for s in content.split(";") if s.strip()]
try:
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
    conn.commit()
    print("[OK]   数据库表初始化完成")
except Exception as e:
    print(f"[WARN] 数据库初始化: {e}")
finally:
    conn.close()
PYEOF
fi

# ============================================================
# 6. 配置 systemd 服务（使用 venv 内的 Python 运行 app.main）
# ============================================================
info "配置 systemd 服务..."
SERVICE_FILE="/etc/systemd/system/geminigen.service"

if [ -w "/etc/systemd/system" ]; then
    cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=GeminiGen API Server
After=network.target

[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${PYTHON} -m app.main
Restart=always
RestartSec=5
StandardOutput=append:${SCRIPT_DIR}/server.log
StandardError=append:${SCRIPT_DIR}/server.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable geminigen
    systemctl restart geminigen
    sleep 2
    if systemctl is-active --quiet geminigen; then
        success "systemd 服务已启动并设为开机自启"
    else
        warn "服务启动异常，查看详情: tail -20 ${SCRIPT_DIR}/server.log"
    fi
else
    warn "无 systemd 权限，使用 nohup 启动..."
    pkill -f "app.main" 2>/dev/null || true
    nohup $PYTHON -m app.main >> server.log 2>&1 &
    SERVER_PID=$!
    sleep 3
    if kill -0 $SERVER_PID 2>/dev/null; then
        echo $SERVER_PID > server.pid
        success "服务器已后台启动 (PID=$SERVER_PID)"
    else
        error "启动失败，请查看 server.log"
    fi
fi

# ============================================================
# 7. 验证服务
# ============================================================
info "验证服务..."
sleep 2
PORT=$(grep '^PORT=' .env | cut -d= -f2 || echo 8000)
if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    success "服务运行正常 ✅"
else
    warn "health check 暂未通过，服务可能仍在启动中"
fi

# ============================================================
# 完成
# ============================================================
SERVER_IP=$(curl -s --connect-timeout 3 ifconfig.me 2>/dev/null || echo 'YOUR_IP')
echo ""
echo "============================================================"
echo -e "${GREEN}  ✅ 部署完成！${NC}"
echo "============================================================"
echo ""
echo "  🌐 网站首页:    http://${SERVER_IP}:${PORT}"
echo "  📋 API 文档:    http://${SERVER_IP}:${PORT}/api/docs"
echo "  🖥  控制台:     http://${SERVER_IP}:${PORT}/dashboard"
echo "  📁 日志:        tail -f ${SCRIPT_DIR}/server.log"
echo ""
echo "  服务管理:"
echo "    systemctl status  geminigen"
echo "    systemctl restart geminigen"
echo "    systemctl stop    geminigen"
echo ""
echo "  下一步: 在本地电脑修改 worker_standalone.py 账号后双击 start_worker.bat"
echo ""
