# -*- coding: utf-8 -*-
"""
GeminiGen 平台 — 本地 Worker
==============================
在本地电脑运行，连接服务器数据库，自动领取并处理生成任务。

启动方式：
  python start.py          （推荐，多账号自动管理）
  python worker_standalone.py --username xxx@gmail.com --password xxx

配置：在同目录 .env 文件中填写账号和数据库信息。
"""

from __future__ import annotations

import sys
import pathlib

# ── 修复 platform 模块冲突 ─────────────────────────────────────
# Python 把脚本所在目录插入 sys.path[0]，导致本地 platform/ 包
# 遮蔽 stdlib platform，引发 zstandard/urllib3 AttributeError。
# 解决：先移除项目根目录，导入所有外部包后再恢复。
_project_root = str(pathlib.Path(__file__).resolve().parent)
while _project_root in sys.path:
    sys.path.remove(_project_root)

import os          # noqa: E402
import argparse    # noqa: E402
import platform    # noqa: E402  — cache stdlib platform in sys.modules before project root is restored
import requests    # noqa: E402
import pymysql     # noqa: E402

sys.path.insert(0, _project_root)

# ── 加载 .env ──────────────────────────────────────────────────
_env_path = pathlib.Path(__file__).with_name(".env")
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── 命令行参数（由 start.py 传入，或手动运行时填写）──────────
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--username",      default="")
_parser.add_argument("--password",      default="")
_parser.add_argument("--instance",      type=int, default=0)
_parser.add_argument("--worker-count",  type=int, default=0)
_args, _ = _parser.parse_known_args()

# ── 数据库配置 ─────────────────────────────────────────────────
DB_HOST     = os.environ.get("DB_HOST",     "47.95.157.46")
DB_PORT     = int(os.environ.get("DB_PORT", "3306"))
DB_USER     = os.environ.get("DB_USER",     "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "root@kunkun")
DB_NAME     = os.environ.get("DB_NAME",     "geminigen_platform")

# ── GeminiGen 账号（命令行 > 环境变量）────────────────────────
_USERNAME = _args.username or os.environ.get("GEMINIGEN_USERNAME", "")
_PASSWORD = _args.password or os.environ.get("GEMINIGEN_PASSWORD", "")
_INSTANCE = _args.instance

# ── Worker 设置 ────────────────────────────────────────────────
WORKER_COUNT  = _args.worker_count or int(os.environ.get("WORKER_COUNT_LOCAL", "3"))
POLL_INTERVAL = 5
QUALITY_CHECK = False

# ── 本地路径 ──────────────────────────────────────────────────
SCRIPT_DIR = str(pathlib.Path(__file__).parent)
TEMP_DIR   = os.path.join(SCRIPT_DIR, "worker_temp")
LOG_FILE   = os.path.join(SCRIPT_DIR, f"worker_{_INSTANCE}.log")

# ============================================================
# 以下无需修改
# ============================================================

import time
import random
import logging
import threading
import traceback
import urllib.request
from pathlib import Path

import pymysql
import pymysql.cursors

import gemini_gen
from cos_upload import upload_to_cos

# ── 日志 ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

_stop_event = threading.Event()


# ============================================================
# 数据库操作
# ============================================================
def _get_conn():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=DB_PASSWORD, database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        autocommit=False,
    )


def claim_pending_task():
    """原子性取一条 pending 任务，标记为 processing（多 Worker 安全）"""
    conn = _get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM gen_tasks "
                "WHERE status = 'pending' "
                "ORDER BY created_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE gen_tasks SET status = 'processing', updated_at = NOW() "
                    "WHERE task_id = %s",
                    (row["task_id"],),
                )
                conn.commit()
            else:
                conn.rollback()
            return row
    except Exception as e:
        conn.rollback()
        logger.error(f"claim_pending_task 异常: {e}")
        return None
    finally:
        conn.close()


def finish_task(task_id, result_url, is_video=False):
    url_col = "result_video_url" if is_video else "result_image_url"
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE gen_tasks SET status = 'success', {url_col} = %s, "
                "updated_at = NOW() WHERE task_id = %s",
                (result_url, task_id),
            )
            cur.execute(
                "UPDATE platform_users pu "
                "JOIN gen_tasks gt ON pu.id = gt.user_id "
                "SET pu.total_tasks = pu.total_tasks + 1 "
                "WHERE gt.task_id = %s",
                (task_id,),
            )
        conn.commit()
    except Exception as e:
        logger.error(f"finish_task 异常: {e}")
        conn.rollback()
    finally:
        conn.close()


def fail_task(task_id, error_msg, refund=True):
    conn = _get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id, cost FROM gen_tasks WHERE task_id = %s",
                (task_id,),
            )
            row = cur.fetchone()
            cur.execute(
                "UPDATE gen_tasks SET status = 'failed', error_msg = %s, "
                "updated_at = NOW() WHERE task_id = %s",
                (error_msg[:500], task_id),
            )
            if refund and row and row.get("cost"):
                cur.execute(
                    "UPDATE platform_users SET balance = balance + %s WHERE id = %s",
                    (row["cost"], row["user_id"]),
                )
                cur.execute(
                    "INSERT INTO balance_transactions "
                    "(user_id, amount, type, task_id, note) VALUES (%s, %s, 'refund', %s, %s)",
                    (row["user_id"], row["cost"], task_id, "任务失败自动退款"),
                )
        conn.commit()
    except Exception as e:
        logger.error(f"fail_task 异常: {e}")
        conn.rollback()
    finally:
        conn.close()


def reset_stuck_tasks(timeout_minutes=30):
    """将卡在 processing 状态超过 N 分钟的任务重置为 pending"""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            affected = cur.execute(
                "UPDATE gen_tasks SET status = 'pending', updated_at = NOW() "
                "WHERE status = 'processing' "
                "AND updated_at < NOW() - INTERVAL %s MINUTE",
                (timeout_minutes,),
            )
        conn.commit()
        if affected:
            logger.info(f"已重置 {affected} 条卡住的 processing 任务")
    except Exception as e:
        logger.error(f"reset_stuck_tasks 异常: {e}")
    finally:
        conn.close()


# ============================================================
# 工具函数
# ============================================================
def download_image(url_or_path, save_path):
    if not url_or_path.startswith("http"):
        if os.path.exists(url_or_path):
            import shutil
            shutil.copy2(url_or_path, save_path)
            return True
        logger.error(f"  本地路径不存在: {url_or_path}")
        return False

    for attempt in range(3):
        try:
            opener = urllib.request.build_opener()
            opener.addheaders = [
                ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
            ]
            urllib.request.install_opener(opener)
            urllib.request.urlretrieve(url_or_path, save_path)
            size_kb = os.path.getsize(save_path) / 1024
            logger.info(f"  图片已下载 {size_kb:.0f}KB -> {os.path.basename(save_path)}")
            return True
        except Exception as e:
            logger.warning(f"  下载第{attempt+1}次失败: {e}")
            if attempt < 2:
                time.sleep(3)

    logger.error(f"  图片下载失败（3次）: {url_or_path[:80]}")
    return False


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


# ============================================================
# 核心：处理单个任务
# ============================================================
VIDEO_MODELS = {"grok-video", "veo-3-fast"}


def process_task(task, worker_id):
    task_id   = task["task_id"]
    model     = task["model"]
    task_type = task.get("task_type") or "image"

    logger.info(f"[W{worker_id}] 任务开始  task_id={task_id}  model={model}  type={task_type}")

    if task_type == "video" or model in VIDEO_MODELS:
        _process_video(task, worker_id)
    else:
        _process_image(task, worker_id)


def _process_video(task, worker_id):
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
    temp_files  = [f for f in [ref_local, video_local] if f]

    try:
        if ref_url and ref_local:
            if not download_image(ref_url, ref_local):
                ref_local = None

        logger.info(f"  [W{worker_id}] 调用视频生成  model={model}  duration={duration}s")
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
            fail_task(task_id, error_type or "视频生成失败")
            return

        cos_key    = f"platform_results/video/{task_id}.mp4"
        result_url = upload_to_cos(video_local, cos_key)
        if not result_url:
            result_url = video_url

        if not result_url:
            fail_task(task_id, "视频上传失败")
            return

        finish_task(task_id, result_url, is_video=True)
        logger.info(f"  [W{worker_id}] 视频任务完成: {result_url[:60]}")

    except Exception as e:
        logger.error(f"  [W{worker_id}] 视频任务异常 {task_id}: {e}")
        traceback.print_exc()
        fail_task(task_id, str(e)[:400])
    finally:
        cleanup(*temp_files)


def _process_image(task, worker_id):
    task_id      = task["task_id"]
    model        = task["model"]
    prompt       = task.get("prompt_text") or ""
    aspect_ratio = task.get("aspect_ratio") or "1:1"
    resolution   = task.get("resolution") or "1K"
    prod_url     = task.get("product_image_url") or ""
    scene_url    = task.get("scene_image_url") or ""

    os.makedirs(TEMP_DIR, exist_ok=True)
    generated_local = os.path.join(TEMP_DIR, f"gen_{task_id}.png")
    temp_files = [generated_local]

    reference_images = []
    for idx, url in enumerate([u for u in [prod_url, scene_url] if u]):
        local_path = os.path.join(TEMP_DIR, f"ref_{task_id}_{idx}.jpg")
        if download_image(url, local_path):
            reference_images.append(local_path)
            temp_files.append(local_path)
        else:
            logger.warning(f"  [W{worker_id}] 参考图下载失败（跳过）: {url[:80]}")

    try:
        final_url   = None
        MAX_RETRIES = 3

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"  [W{worker_id}] 生成第 {attempt}/{MAX_RETRIES} 次...  参考图={len(reference_images)}张")
            success, thumb_url, error_type = gemini_gen.run_task(
                save_path=generated_local,
                prompt_text=prompt,
                model=model,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                reference_images=reference_images if reference_images else None,
            )
            if not success:
                logger.warning(f"  [W{worker_id}] 第{attempt}次生成失败  error={error_type}")
                if attempt < MAX_RETRIES:
                    time.sleep(20)
                continue
            final_url = thumb_url
            break

        if not final_url:
            fail_task(task_id, f"{MAX_RETRIES}次生成均失败")
            cleanup(*temp_files)
            return

        cos_key    = f"platform_results/{task_id}.png"
        result_url = upload_to_cos(generated_local, cos_key)
        if not result_url:
            result_url = final_url

        finish_task(task_id, result_url)
        logger.info(f"  [W{worker_id}] 图片任务完成: {result_url[:60]}")

    except Exception as e:
        logger.error(f"  [W{worker_id}] 图片任务异常 {task_id}: {e}")
        traceback.print_exc()
        fail_task(task_id, str(e)[:400])
    finally:
        cleanup(*temp_files)


# ============================================================
# Worker 主循环
# ============================================================
def worker_loop(worker_id):
    logger.info(f"Worker-{worker_id} 启动")
    while not _stop_event.is_set():
        try:
            task = claim_pending_task()
            if task:
                process_task(task, worker_id)
            else:
                _stop_event.wait(timeout=POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Worker-{worker_id} 循环异常: {e}")
            time.sleep(5)
    logger.info(f"Worker-{worker_id} 已停止")


# ============================================================
# 主入口
# ============================================================
def main():
    logger.info("=" * 60)
    logger.info(f"GeminiGen Worker 启动中  账号: {_USERNAME}  实例: {_INSTANCE}")
    logger.info(f"数据库: {DB_HOST}/{DB_NAME}  Worker线程数: {WORKER_COUNT}")
    logger.info("=" * 60)

    if not _USERNAME or not _PASSWORD:
        logger.error("未配置账号，请在 .env 中设置 GEMINIGEN_USERNAME/PASSWORD")
        logger.error("或通过命令行传入: --username xxx@gmail.com --password xxx")
        input("\n按 Enter 键退出...")
        sys.exit(1)

    # 验证数据库连接
    try:
        conn = _get_conn()
        conn.close()
        logger.info("数据库连接正常")
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        input("\n按 Enter 键退出...")
        sys.exit(1)

    # 重置卡住的任务
    reset_stuck_tasks(timeout_minutes=30)

    # 登录 GeminiGen
    logger.info(f"正在登录 GeminiGen: {_USERNAME}")
    gemini_gen.set_account(_USERNAME, _PASSWORD, _INSTANCE)
    if not gemini_gen.init_login():
        logger.error("GeminiGen 登录失败，请检查账号密码")
        input("\n按 Enter 键退出...")
        sys.exit(1)

    logger.info(f"登录成功，启动 {WORKER_COUNT} 个 Worker 线程...")
    os.makedirs(TEMP_DIR, exist_ok=True)

    threads = []
    for i in range(1, WORKER_COUNT + 1):
        t = threading.Thread(
            target=worker_loop,
            args=(i,),
            name=f"W{_INSTANCE}-{i}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        if i < WORKER_COUNT:
            time.sleep(2)

    logger.info(f"全部 {WORKER_COUNT} 个 Worker 已就绪，等待任务...（Ctrl+C 停止）")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\n收到停止信号，正在退出...")
        _stop_event.set()
        gemini_gen.quit_driver()
        for t in threads:
            t.join(timeout=10)
        logger.info("Worker 已安全停止")


if __name__ == "__main__":
    main()
