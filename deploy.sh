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
# 优先找 python3.11 / python3.10，再降级到 python3
BASE_PYTHON=$(command -v python3.11 || command -v python3.10 || command -v python3)
if [ -z "$BASE_PYTHON" ]; then
    error "未找到 Python，请手动安装 Python 3.10+"
fi
PY_VER=$("$BASE_PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo $PY_VER | cut -d. -f1)
PY_MINOR=$(echo $PY_VER | cut -d. -f2)
if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
    success "Python $PY_VER ($BASE_PYTHON)"
else
    warn "python3 版本过低($PY_VER)，尝试安装 python3.11..."
    yum install -y python3.11 2>/dev/null || apt-get install -y python3.11 2>/dev/null || true
    BASE_PYTHON=$(command -v python3.11 || command -v python3.10)
    [ -z "$BASE_PYTHON" ] && error "请手动安装 Python 3.10+"
    success "找到 $BASE_PYTHON"
fi
fi

# ============================================================
# 2. 创建/更新 virtualenv，隔离依赖不污染系统环境
# ============================================================
info "安装系统工具..."
yum install -y git curl mysql 2>/dev/null || apt-get install -y git curl mysql-client 2>/dev/null || true

VENV_DIR="${SCRIPT_DIR}/.venv"
info "配置虚拟环境 (${VENV_DIR})..."
if [ ! -f "${VENV_DIR}/bin/activate" ]; then
    $BASE_PYTHON -m venv "$VENV_DIR" || error "创建 venv 失败，请检查 python3-venv 是否已安装"
    success "虚拟环境已创建"
else
    success "虚拟环境已存在，跳过创建"
fi

# 后续全部用 venv 内的 Python/pip
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

info "安装 Python 依赖包（可能需要1-2分钟）..."
$PIP install --quiet --upgrade pip
# 先安装关键依赖，确保 API 可启动
$PIP install --quiet fastapi uvicorn[standard] python-multipart python-jose[cryptography] passlib[bcrypt] "bcrypt<5" pymysql pydantic[email] email-validator dnspython requests cos-python-sdk-v5
# 再安装完整依赖（某些可选包在部分镜像源可能不存在，不阻塞核心服务）
$PIP install --quiet -r requirements_platform.txt || warn "部分可选依赖安装失败，核心 API 仍可运行"
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

DB_HOST=127.0.0.1
DB_PORT=3306
DB_USER=root
DB_PASSWORD=
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
# 4. 初始化数据库表 + 迁移新字段
# ============================================================
info "初始化数据库表..."
$PYTHON - <<'PYEOF'
import pymysql, re

sql_file = "schema.sql"
with open(sql_file, "r", encoding="utf-8") as f:
    content = f.read()

# 移除 USE 语句，database 已在连接参数中指定
content = re.sub(r'^\s*USE\s+\S+\s*;\s*$', '', content, flags=re.MULTILINE | re.IGNORECASE)

conn = pymysql.connect(
    host=os.getenv("DB_HOST", "127.0.0.1"), port=int(os.getenv("DB_PORT", "3306")),
    user=os.getenv("DB_USER", "root"), password=os.getenv("DB_PASSWORD", ""),
    database=os.getenv("DB_NAME", "geminigen_platform"), charset="utf8mb4",
    connect_timeout=10,
)
try:
    with conn.cursor() as cur:
        # 执行建表 DDL（CREATE TABLE IF NOT EXISTS 幂等）
        for stmt in [s.strip() for s in content.split(";") if s.strip()]:
            try:
                cur.execute(stmt)
            except Exception as e:
                print(f"[WARN] DDL: {e}")
        conn.commit()

        # ── 迁移：幂等 ALTER TABLE（列已存在则跳过）──────────
        migrations = [
            "ALTER TABLE gen_tasks ADD COLUMN aspect_ratio  VARCHAR(10) NOT NULL DEFAULT '1:1'  AFTER prompt_text",
            "ALTER TABLE gen_tasks ADD COLUMN resolution    VARCHAR(10) NOT NULL DEFAULT '1K'   AFTER aspect_ratio",
            "ALTER TABLE gen_tasks ADD COLUMN output_format VARCHAR(10) NOT NULL DEFAULT 'PNG'  AFTER resolution",
            "ALTER TABLE platform_users ADD COLUMN google_id VARCHAR(100) DEFAULT NULL",
            "ALTER TABLE platform_users ADD COLUMN avatar_url VARCHAR(500) DEFAULT NULL",
            "ALTER TABLE platform_users ADD COLUMN email_verified TINYINT NOT NULL DEFAULT 0",
            "ALTER TABLE platform_users MODIFY COLUMN password_hash VARCHAR(255) DEFAULT NULL",
            # 视频生成支持
            "ALTER TABLE gen_tasks ADD COLUMN task_type VARCHAR(10) NOT NULL DEFAULT 'image' AFTER model",
            "ALTER TABLE gen_tasks ADD COLUMN result_video_url VARCHAR(1000) AFTER result_image_url",
            "ALTER TABLE gen_tasks ADD COLUMN video_duration INT DEFAULT NULL AFTER result_video_url",
            "ALTER TABLE gen_tasks ADD COLUMN video_mode_image VARCHAR(20) DEFAULT NULL AFTER video_duration",
        ]
        for sql in migrations:
            try:
                cur.execute(sql)
                conn.commit()
                col = sql.split("ADD COLUMN")[1].split()[0]
                print(f"[OK]   迁移列: {col}")
            except pymysql.err.OperationalError as e:
                if e.args[0] == 1060:  # Duplicate column — 已存在，跳过
                    pass
                else:
                    print(f"[WARN] 迁移失败: {e}")

    print("[OK]   数据库初始化完成")
except Exception as e:
    print(f"[WARN] 数据库初始化异常: {e}")
finally:
    conn.close()
PYEOF

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
    pkill -f "platform.main" 2>/dev/null || true
    nohup $PYTHON -m platform.main >> server.log 2>&1 &
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
