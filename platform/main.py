# -*- coding: utf-8 -*-
"""GeminiGen 对外平台 —— FastAPI 入口"""

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from .routers import auth, generate, balance, apikeys
from .config import HOST, PORT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化 Worker
    try:
        from .worker import start_workers
        start_workers()
    except Exception as e:
        logger.error(f"Worker 启动失败: {e}")
    yield
    # 停止时清理
    try:
        from .worker import stop_workers
        stop_workers()
    except Exception:
        pass


app = FastAPI(
    title="GeminiGen API",
    description="AI 电商图片生成中转平台",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# API 路由
app.include_router(auth.router)
app.include_router(generate.router)
app.include_router(balance.router)
app.include_router(apikeys.router)

# 静态文件（前端页面）
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


@app.get("/docs-page", include_in_schema=False)
def docs_page():
    return FileResponse(os.path.join(STATIC_DIR, "docs.html"))


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("platform.main:app", host=HOST, port=PORT, reload=False)
