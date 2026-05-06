# -*- coding: utf-8 -*-
"""
后台生成 Worker
- 轮询 gen_tasks 表，取 pending 任务
- 调用 gemini_gen.run_task() 生成图片
- 上传结果，回写数据库
"""

import os
import sys
import json
import time
import random
import logging
import threading
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.append(_BASE)

import gemini_gen
from cos_upload import upload_to_cos

from .config import (
    WORKER_COUNT, WORKER_POLL_S, SCENE_ROOT, TEMP_DIR,
    GEMINIGEN_USERNAME, GEMINIGEN_PASSWORD,
    MODEL_PRICES, VIDEO_MODELS,
)
from . import database as db

PROMPT_UNIFIED = (
    "图1（参考图） + 图2（主体图）"
    " → 提取图1的场景风格 → 提取图2的主体内容"
    " → 按照图1的风格和构图方式展示图2的主体"
)

_stop_event = threading.Event()


# ── 随机选取场景图（不依赖分类）────────────────────────────
def _get_random_scene_photo() -> str:
    """从 SCENE_ROOT 下所有子目录中随机取一张图片"""
    exts = {".jpg", ".jpeg", ".png", ".webp"}
    all_imgs = []

    if os.path.isdir(SCENE_ROOT):
        for root, _dirs, files in os.walk(SCENE_ROOT):
            for f in files:
                if Path(f).suffix.lower() in exts:
                    all_imgs.append(os.path.join(root, f))

    if not all_imgs:
        raise FileNotFoundError(f"场景图目录为空或不存在: {SCENE_ROOT}")
    return random.choice(all_imgs)


def _parse_image_urls(raw: str) -> list:
    """解析 product_image_url 字段：可能是单个 URL，也可能是 JSON 数组"""
    if not raw:
        return []
    if raw.startswith("["):
        try:
            urls = json.loads(raw)
            return [u for u in urls if u]
        except Exception:
            pass
    return [raw]


def _download_image(url_or_path: str, save_path: str) -> bool:
    if url_or_path.startswith("/") or url_or_path.startswith("\\"):
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
    model     = task["model"]
    task_type = task.get("task_type") or "image"

    logger.info(f"[W{worker_id}] 开始处理任务 {task_id} model={model} type={task_type}")

    if task_type == "video" or model in VIDEO_MODELS:
        _process_video_task(task, worker_id)
        return

    _process_image_task(task, worker_id)


def _process_video_task(task: dict, worker_id: int) -> None:
    task_id      = task["task_id"]
    model        = task["model"]
    prompt       = task.get("prompt_text") or ""
    aspect_ratio = task.get("aspect_ratio") or "16:9"
    resolution   = task.get("resolution") or "1080p"
    duration     = task.get("video_duration") or 8
    mode_image   = task.get("video_mode_image") or "ingredient"
    ref_url      = task.get("product_image_url") or ""

    os.makedirs(TEMP_DIR, exist_ok=True)
    ref_local   = os.path.join(TEMP_DIR, f"ref_{task_id}.jpg") if ref_url else None
    video_local = os.path.join(TEMP_DIR, f"vid_{task_id}.mp4")

    try:
        # 下载参考图（如有）
        if ref_url:
            if not _download_image(ref_url, ref_local):
                ref_local = None

        logger.info(f"[W{worker_id}] 调用视频生成接口  model={model}  duration={duration}s")
        success, video_url, error_type = gemini_gen.run_video_task(
            save_path=video_local,
            prompt_text=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
            duration=duration,
            mode_image=mode_image,
            ref_image_path=ref_local,
        )

        if not success:
            db.fail_task(task_id, error_type or "视频生成失败", refund=True)
            return

        # 上传结果到 COS
        cos_key    = f"platform_results/video/{task_id}.mp4"
        result_url = upload_to_cos(video_local, cos_key)
        if not result_url:
            result_url = video_url  # 回退用原始 CDN 链接

        if not result_url:
            db.fail_task(task_id, "视频上传失败", refund=True)
            return

        db.finish_task(task_id, result_url, is_video=True)
        logger.info(f"[W{worker_id}] 视频任务完成 {task_id} -> {result_url[:60]}")

    except Exception as e:
        logger.error(f"[W{worker_id}] 视频任务异常 {task_id}: {e}")
        import traceback; traceback.print_exc()
        db.fail_task(task_id, str(e)[:400], refund=True)
    finally:
        for p in [ref_local, video_local]:
            try:
                if p and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass


def _process_image_task(task: dict, worker_id: int) -> None:
    task_id   = task["task_id"]
    model     = task["model"]
    prod_raw  = task["product_image_url"]
    scene_url = task.get("scene_image_url") or ""
    prompt    = task.get("prompt_text") or ""

    os.makedirs(TEMP_DIR, exist_ok=True)
    generated_local = os.path.join(TEMP_DIR, f"gen_{task_id}.png")
    prod_locals: list[str] = []
    scene_local = None

    try:
        # 1. 下载所有用户参考图（支持多图 JSON 数组或单 URL）
        prod_urls = _parse_image_urls(prod_raw)
        for i, url in enumerate(prod_urls[:5]):
            local_path = os.path.join(TEMP_DIR, f"prod_{task_id}_{i}.jpg")
            if _download_image(url, local_path):
                prod_locals.append(local_path)

        if not prod_locals:
            db.fail_task(task_id, "图片下载失败", refund=True)
            return

        # 2. 确定场景图：仅当用户只上传了1张图时补充场景图
        if len(prod_locals) == 1:
            if scene_url:
                scene_local = os.path.join(TEMP_DIR, f"scene_{task_id}.jpg")
                if not _download_image(scene_url, scene_local):
                    scene_local = None
            if not scene_local:
                try:
                    scene_local = _get_random_scene_photo()
                except Exception as e:
                    db.fail_task(task_id, f"场景图获取失败: {e}", refund=True)
                    return

        # 3. 提示词
        final_prompt = prompt if prompt else PROMPT_UNIFIED

        # 4. 生成：用户多图时直接使用，单图时追加场景图
        if scene_local and os.path.exists(scene_local):
            ref_imgs = prod_locals + [scene_local]
        else:
            ref_imgs = prod_locals

        logger.info(f"[W{worker_id}] 调用生成接口...  参考图={len(ref_imgs)}张")
        success, thumb_url, error_type = gemini_gen.run_task(
            save_path=generated_local,
            prompt_text=final_prompt,
            model=model,
            reference_images=ref_imgs if ref_imgs else None,
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
            result_url = thumb_url

        if not result_url:
            db.fail_task(task_id, "结果上传失败", refund=True)
            return

        # 6. 回写成功
        db.finish_task(task_id, result_url)
        logger.info(f"[W{worker_id}] 任务完成 {task_id} -> {result_url[:60]}")

    except Exception as e:
        logger.error(f"[W{worker_id}] 任务异常 {task_id}: {e}")
        import traceback; traceback.print_exc()
        db.fail_task(task_id, str(e)[:400], refund=True)
    finally:
        for p in prod_locals + ([scene_local] if scene_local else []) + [generated_local]:
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
        logger.warning("未配置 GEMINIGEN_USERNAME/PASSWORD，Worker 不启动")
        return

    logger.info(f"初始化 GeminiGen 账号: {GEMINIGEN_USERNAME}")
    gemini_gen.set_account(GEMINIGEN_USERNAME, GEMINIGEN_PASSWORD, 0)
    if not gemini_gen.init_login():
        logger.error("GeminiGen 登录失败，Worker 不启动")
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

    logger.info(f"{WORKER_COUNT} 个 Worker 已启动")


def stop_workers() -> None:
    _stop_event.set()
    gemini_gen.quit_driver()
    for t in _worker_threads:
        t.join(timeout=10)
    logger.info("所有 Worker 已停止")
