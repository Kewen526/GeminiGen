# -*- coding: utf-8 -*-
"""
后台生成 Worker
- 轮询 gen_tasks 表，取 pending 任务
- 调用 gemini_gen.run_task() 生成图片
- 上传结果，回写数据库
"""

import os
import sys
import time
import random
import logging
import threading
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# 引用上层目录的核心模块
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BASE)

import gemini_gen
from zhipu_classify import classify_image_sync
from cos_upload import upload_to_cos

from .config import (
    WORKER_COUNT, WORKER_POLL_S, SCENE_ROOT, TEMP_DIR,
    GEMINIGEN_USERNAME, GEMINIGEN_PASSWORD,
    MODEL_PRICES,
)
from . import database as db

# ── 提示词（与 main_loop.py 一致）────────────────────────────
PROMPT_UNIFIED = (
    "图1（仓库参考图） + 图2（电商图）"
    " → 提取图1的场景风格 → 提取图2的商品本体（多个商品只取一件）"
    " →还原图2商品应该在货架刚拿下来的状态"
    " → 按照该商品实际分类还原，去除所有电商拍摄效果"
    "（例如商品内部液体、拼装效果、漂浮效果）"
    "按图1的陈列方式展示图2的商品"
)

CATEGORY_FOLDER_MAP = {
    "包":                  "包",
    "保健穿戴":            "保健穿戴",
    "电子":                "电子",
    "发饰饰品":            "发饰饰品",
    "服饰_裤子":           "服饰_裤子",
    "服饰_连衣裙":         "服饰_连衣裙",
    "服饰_上衣":           "服饰_上衣",
    "挂件":                "挂件",
    "化妆品":              "化妆品",
    "家居":                "家居",
    "戒指_项链_耳钉_手链": "戒指_项链_耳钉_手链",
    "帽子":                "帽子",
    "内衣":                "内衣",
    "手机壳":              "手机壳",
    "鞋子":                "鞋子",
    "眼镜":                "眼镜",
    "杂物":                "杂物",
}

_stop_event = threading.Event()


# ── 工具函数 ──────────────────────────────────────────────────
def _get_scene_photo(category: str) -> str:
    folder = CATEGORY_FOLDER_MAP.get(category, "杂物")
    path   = os.path.join(SCENE_ROOT, folder)
    if not os.path.isdir(path):
        # fallback：随机取任意分类
        for f in CATEGORY_FOLDER_MAP.values():
            p = os.path.join(SCENE_ROOT, f)
            if os.path.isdir(p):
                path = p; break

    exts = {".jpg", ".jpeg", ".png", ".webp"}
    imgs = [str(p) for p in Path(path).iterdir()
            if p.suffix.lower() in exts and p.is_file()]
    if not imgs:
        raise FileNotFoundError(f"场景图目录为空: {path}")
    return random.choice(imgs)


def _download_image(url_or_path: str, save_path: str) -> bool:
    if url_or_path.startswith("/") or url_or_path.startswith("\\"):
        # 本地路径（文件上传时的临时文件）
        if os.path.exists(url_or_path):
            import shutil
            shutil.copy2(url_or_path, save_path)
            return True
        return False
    try:
        opener = urllib.request.build_opener()
        opener.addheaders = [("User-Agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")]
        urllib.request.install_opener(opener)
        urllib.request.urlretrieve(url_or_path, save_path)
        return True
    except Exception as e:
        logger.error(f"下载图片失败 {url_or_path}: {e}")
        return False


def _process_task(task: dict, worker_id: int) -> None:
    task_id   = task["task_id"]
    user_id   = task["user_id"]
    model     = task["model"]
    prod_url  = task["product_image_url"]
    scene_url = task.get("scene_image_url") or ""
    prompt    = task.get("prompt_text") or ""

    logger.info(f"[W{worker_id}] 开始处理任务 {task_id} model={model}")

    os.makedirs(TEMP_DIR, exist_ok=True)
    product_local   = os.path.join(TEMP_DIR, f"prod_{task_id}.jpg")
    generated_local = os.path.join(TEMP_DIR, f"gen_{task_id}.png")

    try:
        # 1. 下载商品图
        if not _download_image(prod_url, product_local):
            db.fail_task(task_id, "商品图下载失败", refund=True)
            return

        # 2. 选场景图
        if scene_url:
            # 用户提供了自定义场景图，下载到本地
            scene_local = os.path.join(TEMP_DIR, f"scene_{task_id}.jpg")
            if not _download_image(scene_url, scene_local):
                scene_local = None
        else:
            scene_local = None

        if not scene_local:
            # 自动分类 → 选场景图
            try:
                category = classify_image_sync(prod_url, "")
            except Exception as e:
                logger.warning(f"[W{worker_id}] 分类失败({e})，使用杂物")
                category = "杂物"
            try:
                scene_local = _get_scene_photo(category)
            except Exception as e:
                db.fail_task(task_id, f"场景图获取失败: {e}", refund=True)
                return

        # 3. 提示词
        final_prompt = prompt if prompt else PROMPT_UNIFIED

        # 4. 生成（model 参数通过 gemini_gen.run_task 传入）
        logger.info(f"[W{worker_id}] 调用生成接口...")
        success, thumb_url, error_type = gemini_gen.run_task(
            scene_photo=scene_local,
            product_image=product_local,
            save_path=generated_local,
            prompt_text=final_prompt,
            model=model,
        )

        if error_type == "IMAGE_FORMAT_ERROR":
            db.fail_task(task_id, "图片格式/解析错误", refund=True)
            return

        if not success:
            db.fail_task(task_id, "生成失败", refund=True)
            return

        # 5. 上传结果
        cos_key    = f"platform_results/{task_id}.png"
        result_url = upload_to_cos(generated_local, cos_key)
        if not result_url:
            result_url = thumb_url  # fallback 用 geminigen 原始 URL

        if not result_url:
            db.fail_task(task_id, "结果上传失败", refund=True)
            return

        # 6. 回写成功
        db.finish_task(task_id, result_url)
        logger.info(f"[W{worker_id}] ✅ 任务完成 {task_id} → {result_url[:60]}")

    except Exception as e:
        logger.error(f"[W{worker_id}] 任务异常 {task_id}: {e}")
        import traceback; traceback.print_exc()
        db.fail_task(task_id, str(e)[:400], refund=True)
    finally:
        for p in [product_local, generated_local]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


# ── Worker 主循环 ─────────────────────────────────────────────
def _worker_loop(worker_id: int) -> None:
    logger.info(f"Worker-{worker_id} 已启动")
    while not _stop_event.is_set():
        try:
            task = db.claim_pending_task()
            if task:
                _process_task(task, worker_id)
            else:
                _stop_event.wait(timeout=WORKER_POLL_S)
        except Exception as e:
            logger.error(f"Worker-{worker_id} 循环异常: {e}")
            time.sleep(5)
    logger.info(f"Worker-{worker_id} 已停止")


# ── 启动 / 停止 ───────────────────────────────────────────────
_worker_threads: list[threading.Thread] = []


def start_workers() -> None:
    if not GEMINIGEN_USERNAME or not GEMINIGEN_PASSWORD:
        logger.warning("⚠ 未配置 GEMINIGEN_USERNAME/PASSWORD，Worker 不启动")
        return

    logger.info(f"初始化 GeminiGen 账号: {GEMINIGEN_USERNAME}")
    gemini_gen.set_account(GEMINIGEN_USERNAME, GEMINIGEN_PASSWORD, 0)
    if not gemini_gen.init_login():
        logger.error("❌ GeminiGen 登录失败，Worker 不启动")
        return

    os.makedirs(TEMP_DIR, exist_ok=True)
    _stop_event.clear()

    for i in range(1, WORKER_COUNT + 1):
        t = threading.Thread(
            target=_worker_loop, args=(i,),
            name=f"PlatformWorker-{i}", daemon=True,
        )
        t.start()
        _worker_threads.append(t)
        logger.info(f"Worker-{i} 已创建")

    logger.info(f"✅ {WORKER_COUNT} 个 Worker 已启动")


def stop_workers() -> None:
    _stop_event.set()
    gemini_gen.quit_driver()
    for t in _worker_threads:
        t.join(timeout=10)
    logger.info("所有 Worker 已停止")
