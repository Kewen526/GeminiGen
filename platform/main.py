# -*- coding: utf-8 -*-
"""GeminiGen 对外平台 —— FastAPI 入口"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .routers import auth, generate, balance, apikeys
from .config import HOST, PORT
from . import database as db

CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",") if os.getenv("CORS_ORIGINS") else ["*"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


# ── 定时清理（每 24h 删除 7 天前的任务记录）─────────────────
async def _cleanup_loop():
    await asyncio.sleep(3600)  # 启动 1h 后首次运行
    while True:
        try:
            n = db.delete_old_tasks(days=7)
            if n:
                logger.info(f"定时清理：已删除 {n} 条 7 天前的任务记录")
        except Exception as e:
            logger.error(f"定时清理失败: {e}")
        # 清理内存中的 IP 限流计数，防止无限增长
        now = time.time()
        for ip in list(_ip_requests):
            _ip_requests[ip] = [t for t in _ip_requests[ip] if now - t < 60]
            if not _ip_requests[ip]:
                del _ip_requests[ip]
        await asyncio.sleep(24 * 3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_task = asyncio.create_task(_cleanup_loop())

    worker_count = os.getenv("WORKER_COUNT", "0")
    if str(worker_count).isdigit() and int(worker_count) > 0:
        try:
            from .worker import start_workers
            start_workers()
        except Exception as e:
            logger.error(f"Worker 启动失败: {e}")
    else:
        logger.info("WORKER_COUNT=0，跳过 Worker 启动")

    yield

    cleanup_task.cancel()
    if str(worker_count).isdigit() and int(worker_count) > 0:
        try:
            from .worker import stop_workers
            stop_workers()
        except Exception:
            pass


app = FastAPI(
    title="Kewen AI API",
    description="AI 图像生成平台",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=CORS_ORIGINS != ["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── IP 限流中间件（针对 /auth/ 接口）────────────────────────
_ip_requests: dict[str, list] = {}
_ip_lock_time: dict[str, float] = {}

@app.middleware("http")
async def auth_rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/auth/"):
        forwarded = request.headers.get("X-Forwarded-For")
        ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host or "unknown")
        now = time.time()
        reqs = _ip_requests.get(ip, [])
        reqs = [t for t in reqs if now - t < 60]
        if len(reqs) >= 30:  # 每分钟最多 30 次 auth 请求/IP
            return JSONResponse({"detail": "请求过于频繁，请 1 分钟后再试"}, status_code=429)
        reqs.append(now)
        _ip_requests[ip] = reqs
    return await call_next(request)


# API 路由
app.include_router(auth.router)
app.include_router(generate.router)
app.include_router(balance.router)
app.include_router(apikeys.router)

# 静态文件
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ── 前端页面路由 ──────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(os.path.join(STATIC_DIR, "dashboard.html"))


@app.get("/generate", include_in_schema=False)
def generate_page():
    return FileResponse(os.path.join(STATIC_DIR, "generate.html"))


@app.get("/app/imagen", include_in_schema=False)
def app_imagen():
    return FileResponse(os.path.join(STATIC_DIR, "generate.html"))


@app.get("/docs-page", include_in_schema=False)
def docs_page():
    return FileResponse(os.path.join(STATIC_DIR, "docs.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("platform.main:app", host=HOST, port=PORT, reload=False)
