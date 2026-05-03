#!/bin/bash
# ============================================================
#  GeminiGen 平台 — 服务器一键部署脚本
#  用法: bash deploy.sh
# ============================================================
set -e

# ── 颜色输出 ──────────────────────────────────────────────────
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
# 1. 检查并安装 Python 3.10+
# ============================================================
info "检查 Python..."
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(echo $PY_VER | cut -d. -f1)
    PY_MINOR=$(echo $PY_VER | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        success "Python $PY_VER"
    else
        warn "Python $PY_VER 版本过低，需要 3.10+，尝试安装新版本..."
        apt-get update -qq && apt-get install -y python3.11 python3.11-pip 2>/dev/null \
            || yum install -y python311 python311-pip 2>/dev/null \
            || error "请手动安装 Python 3.10+"
    fi
else
    info "未找到 Python3，正在安装..."
    apt-get update -qq && apt-get install -y python3 python3-pip python3-venv \
        || yum install -y python3 python3-pip \
        || error "Python 安装失败，请手动安装"
fi

BASE_PYTHON=$(command -v python3.11 || command -v python3.10 || command -v python3)
success "使用 Python: $BASE_PYTHON"

# ============================================================
# 2. 创建/更新 virtualenv，隔离依赖不污染系统环境
# ============================================================
info "安装系统工具..."
yum install -y git curl mysql 2>/dev/null || apt-get install -y git curl mysql-client 2>/dev/null || true

VENV_DIR="${SCRIPT_DIR}/.venv"

# ── 坑①：旧 venv 可能由系统默认的 Python 3.6 创建，3.6 不满足依赖要求
# 若 venv 内的 Python 主版本 < 3.8，直接删除重建
if [ -f "${VENV_DIR}/bin/python" ]; then
    VENV_PY_MINOR=$("${VENV_DIR}/bin/python" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
    VENV_PY_MAJOR=$("${VENV_DIR}/bin/python" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo 0)
    if [ "$VENV_PY_MAJOR" -lt 3 ] || [ "$VENV_PY_MINOR" -lt 8 ]; then
        warn "venv Python ${VENV_PY_MAJOR}.${VENV_PY_MINOR} 太旧（需 3.8+），删除并用 ${BASE_PYTHON} 重建..."
        rm -rf "$VENV_DIR"
    fi
fi

info "配置虚拟环境 (${VENV_DIR})..."
if [ ! -f "${VENV_DIR}/bin/activate" ]; then
    $BASE_PYTHON -m venv "$VENV_DIR" || error "创建 venv 失败，请检查 python3-venv 是否已安装"
    success "虚拟环境已创建 ($(${VENV_DIR}/bin/python --version))"
else
    success "虚拟环境已存在 ($(${VENV_DIR}/bin/python --version))，跳过创建"
fi

# 后续全部用 venv 内的 Python/pip
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

# ── 坑②：在中国大陆服务器上，PyPI 官方源下载不稳定，改用阿里云镜像
PIP_MIRROR="https://mirrors.aliyun.com/pypi/simple/"
PIP_OPTS="-i ${PIP_MIRROR} --trusted-host mirrors.aliyun.com"

info "安装 Python 依赖包（使用阿里云镜像，约 1-2 分钟）..."
$PIP install --quiet --upgrade pip $PIP_OPTS

# ── 坑③：fastapi 0.136+ / pydantic 2.13+ 组合存在 BaseModel 导入异常，
#    固定到已验证稳定的版本区间
$PIP install --quiet $PIP_OPTS \
    "fastapi>=0.115.2,<0.120" \
    "uvicorn[standard]>=0.29.0" \
    "python-multipart>=0.0.9" \
    "python-jose[cryptography]>=3.3.0" \
    "passlib[bcrypt]>=1.7.4" \
    "bcrypt<5" \
    "pymysql>=1.1.0" \
    "pydantic[email]>=2.9.0,<2.12" \
    "requests" \
    "aiohttp" \
    "cos-python-sdk-v5>=1.9.0"

# 再装完整 requirements（某些可选包不影响核心服务）
$PIP install --quiet $PIP_OPTS -r requirements_platform.txt || warn "部分可选依赖安装失败，核心 API 仍可运行"
success "依赖安装完成"

# ============================================================
# 3. 生成 .env（如果不存在则创建，已有则跳过）
# ============================================================
if [ -f ".env" ]; then
    warn ".env 已存在，跳过生成（如需重置请删除 .env 后重新运行）"
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
DB_NAME=geminigen_platform

HOST=0.0.0.0
PORT=8000

# 生成任务由本地电脑的 worker_standalone.py 处理，服务器不启动 Worker
WORKER_COUNT=0
GEMINIGEN_USERNAME=
GEMINIGEN_PASSWORD=
EOF
    success ".env 已生成"
fi

# ============================================================
# 4. 初始化数据库表
# ============================================================
info "初始化数据库表..."
if command -v mysql &>/dev/null; then
    mysql -h 47.95.157.46 -P 3306 -u root -p'root@kunkun' < schema.sql \
        && success "数据库表初始化完成" \
        || warn "数据库初始化失败，请手动执行: mysql < schema.sql"
else
    $PYTHON - <<'PYEOF'
import pymysql, re, sys

sql_file = "schema.sql"
with open(sql_file, "r", encoding="utf-8") as f:
    content = f.read()

content = re.sub(r'^\s*USE\s+\S+\s*;\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)

conn = pymysql.connect(
    host="47.95.157.46", port=3306,
    user="root", password="root@kunkun",
    database="geminigen_platform", charset="utf8mb4",
    connect_timeout=10,
)
statements = [s.strip() for s in content.split(";") if s.strip()]
try:
    with conn.cursor() as cur:
        for stmt in statements:
            if stmt:
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
# 5. 创建 systemd 服务（用 venv 内的 Python，彻底隔离依赖）
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
ExecStart=/usr/bin/env bash ${SCRIPT_DIR}/run_server.sh
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
        warn "服务启动异常，查看详情: journalctl -u geminigen -n 50"
    fi
else
    warn "无 systemd 权限，使用 nohup 启动..."
    # ── 坑④：不能用 `python -m platform.main`，本地 platform/ 包会遮蔽
    #    stdlib 的 platform 模块导致 pydantic 启动失败，改用 run_api.py
    pkill -f "run_api.py" 2>/dev/null || true
    nohup $PYTHON "${SCRIPT_DIR}/run_api.py" >> server.log 2>&1 &
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
# 6. 验证服务
# ============================================================
info "验证服务..."
sleep 2
PORT=$(grep '^PORT=' .env | cut -d= -f2 || echo 8000)
if curl -sf "http://localhost:${PORT}/health" >/dev/null 2>&1; then
    success "服务运行正常 ✅"
else
    warn "health check 失败，可能还在启动中，稍后手动验证: curl http://localhost:${PORT}/health"
fi

# ============================================================
# 完成
# ============================================================
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP')
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
