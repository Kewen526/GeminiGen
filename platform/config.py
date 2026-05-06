# -*- coding: utf-8 -*-
import os

# 优先读取同目录下的 .env 文件
_env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(_env_file):
    for _line in open(_env_file, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── JWT ──────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-use-long-random-string-in-production")
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 天

# ── 数据库 ────────────────────────────────────────────────────
DB_CONFIG = {
    "host":            os.getenv("DB_HOST", "127.0.0.1"),
    "port":            int(os.getenv("DB_PORT", "3306")),
    "user":            os.getenv("DB_USER", "root"),
    "password":        os.getenv("DB_PASSWORD", ""),
    "database":        os.getenv("DB_NAME", "geminigen_platform"),
    "charset":         "utf8mb4",
    "connect_timeout": 10,
}

# ── 定价（人民币 / 次）────────────────────────────────────────
MODEL_PRICES: dict[str, float] = {
    "nano-banana-2":   0.05,
    "nano-banana-pro": 0.06,
    # 视频生成
    "grok-video":      1.00,
    "veo-3-fast":      2.00,
}
DEFAULT_MODEL = "nano-banana-2"

IMAGE_MODELS = {"nano-banana-2", "nano-banana-pro"}
VIDEO_MODELS  = {"grok-video", "veo-3-fast"}

# ── GeminiGen 账号（仅服务器内置 Worker 使用，本地 Worker 不用）
GEMINIGEN_USERNAME = os.getenv("GEMINIGEN_USERNAME", "")
GEMINIGEN_PASSWORD = os.getenv("GEMINIGEN_PASSWORD", "")

# ── Worker 配置 ───────────────────────────────────────────────
# 服务器部署时建议设为 0，生成任务交给本地 worker_standalone.py 处理
WORKER_COUNT  = int(os.getenv("WORKER_COUNT", "0"))
WORKER_POLL_S = 5

# ── 路径 ──────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENE_ROOT = os.path.join(SCRIPT_DIR, "商家实拍图")
TEMP_DIR   = os.path.join(SCRIPT_DIR, "platform_temp")

# ── 服务地址 ──────────────────────────────────────────────────
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ── 邮件（注册验证码）────────────────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST", "")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", SMTP_USER)
SMTP_ENABLED  = bool(SMTP_HOST and SMTP_USER)

# ── Google OAuth（预留）──────────────────────────────────────
GOOGLE_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")

# ── 积分制 ────────────────────────────────────────────────────
POINTS_PER_YUAN = 100   # 1元 = 100积分

# ── 安全限流 ──────────────────────────────────────────────────
AUTH_RATE_LIMIT     = 10   # 每 IP 每分钟最多 10 次 auth 请求
GENERATE_RATE_LIMIT = 10   # 每用户每分钟最多 10 次生成请求
MAX_LOGIN_ATTEMPTS  = 5    # 连续失败 5 次锁定
LOCKOUT_MINUTES     = 15   # 锁定时长（分钟）

# ── 敏感词 ────────────────────────────────────────────────────
SENSITIVE_WORDS: list[str] = [
    "色情", "裸体", "性爱", "淫秽", "毒品", "制毒", "炸弹", "枪支",
    "赌博", "诈骗", "政治", "暴恐", "恐怖主义",
]
