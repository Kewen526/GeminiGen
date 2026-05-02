# -*- coding: utf-8 -*-
import os

# ── JWT ──────────────────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-use-long-random-string")
ALGORITHM  = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 天

# ── 数据库 ────────────────────────────────────────────────────
DB_CONFIG = {
    "host":            os.getenv("DB_HOST", "47.95.157.46"),
    "port":            int(os.getenv("DB_PORT", "3306")),
    "user":            os.getenv("DB_USER", "root"),
    "password":        os.getenv("DB_PASSWORD", "root@kunkun"),
    "database":        os.getenv("DB_NAME", "quote_iw"),
    "charset":         "utf8mb4",
    "connect_timeout": 10,
}

# ── 定价 (人民币/张) ───────────────────────────────────────────
MODEL_PRICES: dict[str, float] = {
    "nano-banana-2":   0.05,
    "nano-banana-pro": 0.06,
}
DEFAULT_MODEL = "nano-banana-2"

# ── GeminiGen 账号（worker 使用）──────────────────────────────
GEMINIGEN_USERNAME = os.getenv("GEMINIGEN_USERNAME", "")
GEMINIGEN_PASSWORD = os.getenv("GEMINIGEN_PASSWORD", "")

# ── Worker 并发数 ─────────────────────────────────────────────
WORKER_COUNT   = int(os.getenv("WORKER_COUNT", "3"))
WORKER_POLL_S  = 5    # 轮询间隔秒数

# ── 场景图根目录（与 main_loop.py 一致）───────────────────────
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # GeminiGen/
SCENE_ROOT = os.path.join(SCRIPT_DIR, "商家实拍图")
TEMP_DIR   = os.path.join(SCRIPT_DIR, "platform_temp")

# ── 服务地址 ──────────────────────────────────────────────────
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
