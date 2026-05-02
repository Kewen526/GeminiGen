# -*- coding: utf-8 -*-
"""
GeminiGen 平台 — 独立 Worker
==============================
在本地电脑运行，连接服务器数据库，自动领取并处理生成任务。

使用方式：
  Windows: 双击 start_worker.bat
  手动启动: python worker_standalone.py

依赖安装:
  pip install -r requirements_worker.txt
  playwright install chromium
"""

# ============================================================
# ★★★ 配置区 —— 根据实际情况修改 ★★★
# ============================================================

# ── 数据库（服务器上的 MySQL）────────────────────────────────
DB_HOST     = "47.95.157.46"
DB_PORT     = 3306
DB_USER     = "root"
DB_PASSWORD = "root@kunkun"
DB_NAME     = "quote_iw"

# ── GeminiGen 账号（用于生成图片）────────────────────────────
ACCOUNTS = [
    # 可配置多个账号，每个账号启动独立浏览器
    {"username": "your_account@gmail.com", "password": "your_password"},
    # {"username": "account2@gmail.com",    "password": "password2"},
]

# ── 并发设置 ──────────────────────────────────────────────────
WORKER_COUNT   = 3     # 每个账号启动几个 Worker 线程
POLL_INTERVAL  = 5     # 无任务时等待秒数
QUALITY_CHECK  = False # 是否开启质量检测（True 更稳定但每张多30s）

# ── 本地路径 ──────────────────────────────────────────────────
import os
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCENE_ROOT = os.path.join(SCRIPT_DIR, "商家实拍图")   # 仓库场景图目录
TEMP_DIR   = os.path.join(SCRIPT_DIR, "worker_temp")  # 临时文件目录
LOG_FILE   = os.path.join(SCRIPT_DIR, "worker.log")   # 日志文件

# ============================================================
# 以下无需修改
# ============================================================

import sys
import time
import random
import logging
import threading
import traceback
import urllib.request
from pathlib import Path

import pymysql
import pymysql.cursors

# 引入本目录的核心模块
import gemini_gen
from zhipu_classify import classify_image_sync
from cos_upload import upload_to_cos
if QUALITY_CHECK:
    from score_checker import quality_check


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

# ── 分类 → 场景图文件夹映射 ───────────────────────────────────
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

PROMPT_UNIFIED = (
    "图1（仓库参考图） + 图2（电商图）"
    " → 提取图1的场景风格 → 提取图2的商品本体（多个商品只取一件）"
    " →还原图2商品应该在货架刚拿下来的状态"
    " → 按照该商品实际分类还原，去除所有电商拍摄效果"
    "（例如商品内部液体、拼装效果、漂浮效果）"
    "按图1的陈列方式展示图2的商品"
)

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


def finish_task(task_id, result_url):
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE gen_tasks SET status = 'success', result_image_url = %s, "
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
    """将卡在 processing 状态超过 N 分钟的任务重置为 pending（启动时清理）"""
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
def get_scene_photo(category):
    folder = CATEGORY_FOLDER_MAP.get(category)
    if not folder:
        # 找不到分类就随机用一个
        folder = random.choice(list(CATEGORY_FOLDER_MAP.values()))

    folder_path = os.path.join(SCENE_ROOT, folder)
    if not os.path.isdir(folder_path):
        # fallback：遍历找第一个有图的文件夹
        for f in CATEGORY_FOLDER_MAP.values():
            p = os.path.join(SCENE_ROOT, f)
            if os.path.isdir(p):
                folder_path = p; break
        else:
            raise FileNotFoundError(f"场景图目录不存在: {SCENE_ROOT}")

    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    imgs = [str(p) for p in Path(folder_path).iterdir()
            if p.suffix.lower() in exts and p.is_file()]
    if not imgs:
        raise FileNotFoundError(f"场景图文件夹为空: {folder_path}")

    chosen = random.choice(imgs)
    logger.info(f"  场景图: {os.path.basename(chosen)} [{folder}]")
    return chosen


def download_image(url_or_path, save_path):
    # 本地路径（服务器上传的临时文件，Worker 在同机器时直接复制）
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
            logger.info(f"  ✅ 图片已下载 {size_kb:.0f}KB → {os.path.basename(save_path)}")
            return True
        except Exception as e:
            logger.warning(f"  下载第{attempt+1}次失败: {e}")
            if attempt < 2:
                time.sleep(3)

    logger.error(f"  ❌ 图片下载失败（3次）: {url_or_path[:80]}")
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
def process_task(task, worker_id):
    task_id   = task["task_id"]
    model     = task["model"]
    prod_url  = task["product_image_url"]
    scene_url = task.get("scene_image_url") or ""
    prompt    = task.get("prompt_text") or ""

    logger.info(f"▶ [W{worker_id}] 任务开始  task_id={task_id}  model={model}")

    os.makedirs(TEMP_DIR, exist_ok=True)
    product_local   = os.path.join(TEMP_DIR, f"prod_{task_id}.jpg")
    scene_local_tmp = os.path.join(TEMP_DIR, f"scene_{task_id}.jpg")
    generated_local = os.path.join(TEMP_DIR, f"gen_{task_id}.png")
    temp_files = [product_local, scene_local_tmp, generated_local]

    try:
        # 1. 下载商品图
        logger.info(f"  [W{worker_id}] 下载商品图...")
        if not download_image(prod_url, product_local):
            fail_task(task_id, "商品图下载失败")
            return

        # 2. 选场景图
        if scene_url:
            if download_image(scene_url, scene_local_tmp):
                scene_local = scene_local_tmp
            else:
                logger.warning("  用户场景图下载失败，改用自动选择")
                scene_local = None
        else:
            scene_local = None

        if not scene_local:
            logger.info(f"  [W{worker_id}] AI 分类中...")
            try:
                category = classify_image_sync(prod_url, "")
                logger.info(f"  分类结果: {category}")
            except Exception as e:
                logger.warning(f"  分类失败({e})，使用杂物")
                category = "杂物"
            try:
                scene_local = get_scene_photo(category)
            except Exception as e:
                fail_task(task_id, f"场景图获取失败: {e}")
                cleanup(*temp_files)
                return

        # 3. 生成（最多 3 次）
        final_url   = None
        MAX_RETRIES = 3

        for attempt in range(1, MAX_RETRIES + 1):
            logger.info(f"  [W{worker_id}] 生成第 {attempt}/{MAX_RETRIES} 次...")

            if attempt > 1:
                # 重新选场景图
                try:
                    category = classify_image_sync(prod_url, "")
                    scene_local = get_scene_photo(category)
                except Exception:
                    pass

            success, thumb_url, error_type = gemini_gen.run_task(
                scene_photo=scene_local,
                product_image=product_local,
                save_path=generated_local,
                prompt_text=prompt or PROMPT_UNIFIED,
                model=model,
            )

            if error_type == "IMAGE_FORMAT_ERROR":
                fail_task(task_id, "图片格式/解析错误，无法处理")
                cleanup(*temp_files)
                return

            if not success:
                logger.warning(f"  [W{worker_id}] 第{attempt}次生成失败")
                if attempt < MAX_RETRIES:
                    time.sleep(20)
                continue

            # 4. 质量检测（可选）
            if QUALITY_CHECK and thumb_url:
                logger.info(f"  [W{worker_id}] 质量检测中...")
                passed, reason = quality_check(scene_local, prod_url, thumb_url)
                if not passed:
                    logger.warning(f"  质量检测不合格: {reason}（第{attempt}次）")
                    if attempt < MAX_RETRIES:
                        time.sleep(10)
                    continue
                logger.info(f"  ✅ 质量检测合格")

            final_url = thumb_url
            break

        if not final_url:
            fail_task(task_id, f"{MAX_RETRIES}次生成均失败")
            cleanup(*temp_files)
            return

        # 5. 上传结果
        cos_key    = f"platform_results/{task_id}.png"
        result_url = upload_to_cos(generated_local, cos_key)
        if not result_url:
            result_url = final_url  # fallback：用 geminigen 直链

        # 6. 回写成功
        finish_task(task_id, result_url)
        logger.info(f"  ✅ [W{worker_id}] 任务完成！{result_url[:60]}")

    except Exception as e:
        logger.error(f"  ❌ [W{worker_id}] 任务异常 {task_id}: {e}")
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
# 启动验证
# ============================================================
def check_config():
    errors = []
    if not ACCOUNTS or not ACCOUNTS[0].get("username"):
        errors.append("❌ 未填写 GeminiGen 账号（修改文件顶部的 ACCOUNTS 配置）")
    if not os.path.isdir(SCENE_ROOT):
        errors.append(f"❌ 场景图目录不存在: {SCENE_ROOT}")
    # 验证数据库连接
    try:
        conn = _get_conn()
        conn.close()
    except Exception as e:
        errors.append(f"❌ 数据库连接失败: {e}")
    return errors


# ============================================================
# 主入口
# ============================================================
def main():
    logger.info("=" * 60)
    logger.info("GeminiGen 平台 Worker 启动中...")
    logger.info(f"脚本目录: {SCRIPT_DIR}")
    logger.info(f"场景图目录: {SCENE_ROOT}")
    logger.info(f"并发数: {WORKER_COUNT}  质量检测: {QUALITY_CHECK}")
    logger.info("=" * 60)

    # 启动前校验
    errors = check_config()
    if errors:
        for e in errors:
            logger.error(e)
        logger.error("配置有误，请修改 worker_standalone.py 顶部的配置区后重试")
        input("\n按 Enter 键退出...")
        sys.exit(1)

    # 重置卡住的任务
    reset_stuck_tasks(timeout_minutes=30)

    # 初始化 GeminiGen（支持多账号，每账号独立浏览器）
    account = ACCOUNTS[0]
    logger.info(f"初始化账号: {account['username']}")
    gemini_gen.set_account(account["username"], account["password"], 0)
    if not gemini_gen.init_login():
        logger.error("❌ GeminiGen 登录失败，请检查账号密码")
        input("\n按 Enter 键退出...")
        sys.exit(1)

    logger.info(f"✅ 登录成功，启动 {WORKER_COUNT} 个 Worker 线程...")
    os.makedirs(TEMP_DIR, exist_ok=True)

    # 启动 Worker 线程
    threads = []
    for i in range(1, WORKER_COUNT + 1):
        t = threading.Thread(
            target=worker_loop,
            args=(i,),
            name=f"W{i}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        if i < WORKER_COUNT:
            time.sleep(2)

    logger.info(f"✅ 全部 {WORKER_COUNT} 个 Worker 已就绪，等待任务...")
    logger.info("（按 Ctrl+C 停止）")

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
