# -*- coding: utf-8 -*-
"""
Kewen AI Pipeline - 5路并发版（多进程部署）
==============================================
取任务 → 分类(3选2投票) → 选场景图 → 生成(按分类选提示词) → 质量检测(3图5维度3投票) → 上传 → 回写

启动方式：
  由 start.py 自动启动，不要直接运行此文件。
  若需手动测试：python main_loop.py --idx 0 --username xxx@gmail.com --password xxx
"""

import os
import sys
import time
import random
import logging
import argparse
import threading
import urllib.request
from pathlib import Path

# ============================================================
# 命令行参数（由 start.py 传入）
# ============================================================
_parser = argparse.ArgumentParser()
_parser.add_argument("--idx",      type=int,   default=0,  help="实例序号（0, 1, 2...）")
_parser.add_argument("--username", type=str,   default="", help="geminigen账号")
_parser.add_argument("--password", type=str,   default="", help="geminigen密码")
_args, _ = _parser.parse_known_args()

INSTANCE_IDX = _args.idx
INSTANCE_USERNAME = _args.username
INSTANCE_PASSWORD = _args.password

# ============================================================
# 配置项
# ============================================================
# 场景图目录：自动查找代码所在目录下的"商家实拍图"文件夹，无需手动配置路径
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
SCENE_ROOT   = os.path.join(SCRIPT_DIR, "商家实拍图")

WORKER_COUNT          = 5
NO_TASK_WAIT          = 30
MAX_GENERATE_ATTEMPTS = 3
# ============================================================

# ============================================================
# 提示词
# ============================================================
PROMPT_UNIFIED = """图1（仓库参考图） + 图2（电商图） → 提取图1的场景风格 → 提取图2的商品本体（多个商品只取一件） →还原图2商品应该在货架刚拿下来的状态→ 按照该商品实际分类还原，去除所有电商拍摄效果（例如商品内部液体、拼装效果、漂浮效果）按图1的陈列方式展示图2的商品"""

# ============================================================
# 日志（每个实例独立日志文件）
# ============================================================
LOG_FILE = os.path.join(SCRIPT_DIR, f"pipeline_{INSTANCE_IDX}.log")

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [%(levelname)s] [实例{INSTANCE_IDX}][%(threadName)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================
# 导入
# ============================================================
import gemini_gen
from db import (
    fetch_and_claim_task, update_ai_image, mark_task_skipped, update_task_status,
    fetch_retry_task, mark_retry_failed,
)
from zhipu_classify import classify_image_sync
from cos_upload import upload_to_cos
from score_checker import quality_check

# ============================================================
# 17个分类 → 场景图子文件夹名
# ============================================================
CATEGORY_FOLDER_MAP = {
    "包":                     "包",
    "保健穿戴":               "保健穿戴",
    "电子":                   "电子",
    "发饰饰品":               "发饰饰品",
    "服饰_裤子":              "服饰_裤子",
    "服饰_连衣裙":            "服饰_连衣裙",
    "服饰_上衣":              "服饰_上衣",
    "挂件":                   "挂件",
    "化妆品":                 "化妆品",
    "家居":                   "家居",
    "戒指_项链_耳钉_手链":    "戒指_项链_耳钉_手链",
    "帽子":                   "帽子",
    "内衣":                   "内衣",
    "手机壳":                 "手机壳",
    "鞋子":                   "鞋子",
    "眼镜":                   "眼镜",
    "杂物":                   "杂物",
}

# 每个实例使用独立的临时目录，避免多进程文件名冲突
DESKTOP_PATH = os.path.join(os.path.expanduser("~"), "Desktop")
TEMP_DIR     = os.path.join(DESKTOP_PATH, f"kewen_ai_temp_{INSTANCE_IDX}")

_task_counter_lock = threading.Lock()
_task_counter = 0


def _inc_counter():
    global _task_counter
    with _task_counter_lock:
        _task_counter += 1
        return _task_counter


# ============================================================
# 工具函数
# ============================================================
def get_scene_photo(category: str) -> str:
    """随机获取1张场景参考图"""
    folder_name = CATEGORY_FOLDER_MAP.get(category)
    if not folder_name:
        raise ValueError(f"未知分类: {category}")

    folder_path = os.path.join(SCENE_ROOT, folder_name)
    if not os.path.isdir(folder_path):
        raise FileNotFoundError(f"场景图文件夹不存在: {folder_path}")

    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    all_photos = [
        str(p) for p in Path(folder_path).iterdir()
        if p.suffix.lower() in exts and p.is_file()
    ]

    if not all_photos:
        raise ValueError(f"文件夹 '{folder_path}' 中没有图片")

    selected = random.choice(all_photos)
    logger.info(f"从 [{folder_name}] 随机选取场景图: {os.path.basename(selected)}")
    return selected


def get_gen_prompt(category: str) -> str:
    logger.info(f"  使用统一提示词（分类={category}）")
    return PROMPT_UNIFIED


def download_product_image(image_url: str, save_path: str) -> bool:
    for attempt in range(3):
        try:
            logger.info(f"下载商品图: {image_url[:80]}...")
            opener = urllib.request.build_opener()
            opener.addheaders = [('User-Agent',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')]
            urllib.request.install_opener(opener)
            urllib.request.urlretrieve(image_url, save_path)
            size_kb = os.path.getsize(save_path) / 1024
            logger.info(f"✅ 商品图已下载: {save_path} ({size_kb:.1f}KB)")
            return True
        except Exception as e:
            logger.warning(f"商品图下载第{attempt+1}次失败: {e}")
            if attempt < 2:
                time.sleep(3)
    logger.error("❌ 商品图下载失败（3次重试）")
    return False


def validate_startup() -> bool:
    """启动校验"""
    if not os.path.isdir(SCENE_ROOT):
        logger.error(f"❌ 场景图目录不存在: {SCENE_ROOT}")
        logger.error('   请将"商家实拍图"文件夹放到代码所在目录下')
        return False
    found_folders = [f for f in CATEGORY_FOLDER_MAP.values()
                     if os.path.isdir(os.path.join(SCENE_ROOT, f))]
    logger.info(f"场景图目录有效，包含子文件夹({len(found_folders)}个): {found_folders}")
    if not found_folders:
        logger.error("❌ 场景图目录下没有任何分类子文件夹")
        return False
    return True


def _cleanup_temp_files(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


# ============================================================
# Worker 循环
# ============================================================
def worker_loop(worker_id: int):
    logger.info(f"Worker-{worker_id} 启动")

    while True:
        try:
            # 1. 原子性取任务（数据库层面 SELECT FOR UPDATE SKIP LOCKED，多机器/多进程安全）
            task = fetch_and_claim_task(max_days_back=7)
            retry_level = 0  # 0=普通任务，1=一级重试，2=二级重试

            if not task:
                # 一级重试：取 SKIP:3次生成均未通过质量检测 的任务
                task = fetch_retry_task(level=1)
                if task:
                    retry_level = 1
                    logger.info("▶ 无普通任务，进入一级重试模式")

            if not task:
                # 二级重试：取 SKIP:3次生成均未通过质量检测*2 的任务
                task = fetch_retry_task(level=2)
                if task:
                    retry_level = 2
                    logger.info("▶ 无一级重试任务，进入二级重试模式")

            if not task:
                logger.info(f"暂无任何任务（含重试队列），{NO_TASK_WAIT}秒后重试...")
                time.sleep(NO_TASK_WAIT)
                continue

            keer_product_id = task["keer_product_id"]
            product_image   = task["product_image"]
            product_title   = task.get("product_title", "") or ""
            task_status     = task.get("task_status", "")

            logger.info(f"▶ 任务开始 keer_product_id={keer_product_id}")
            logger.info(f"  product_image = {str(product_image)[:80]}...")
            if product_title:
                logger.info(f"  product_title = {product_title[:60]}...")

            # 2. 跳过空图
            if not product_image:
                logger.warning("⚠ product_image为空，跳过")
                mark_task_skipped(keer_product_id, "商品图URL为空")
                continue

            # 3. AI分类
            logger.info("🔍 AI分类中...")
            try:
                category = classify_image_sync(product_image, product_title)
                logger.info(f"✅ AI分类结果: {category}")
            except Exception as e:
                logger.error(f"❌ AI分类失败: {e}，跳过")
                mark_task_skipped(keer_product_id, f"AI分类失败:{e}")
                continue

            # 4. 提示词
            gen_prompt = get_gen_prompt(category)

            # 5. 场景图
            try:
                scene_photo = get_scene_photo(category)
            except Exception as e:
                logger.error(f"❌ 获取场景图失败: {e}，跳过")
                mark_task_skipped(keer_product_id, f"场景图获取失败:{e}")
                continue

            # 6. 下载商品图（临时文件加 worker_id 前缀避免同进程内线程冲突）
            product_local   = os.path.join(TEMP_DIR, f"product_{keer_product_id}_w{worker_id}.jpg")
            generated_local = os.path.join(TEMP_DIR, f"generated_{keer_product_id}_w{worker_id}.png")

            if not download_product_image(product_image, product_local):
                mark_task_skipped(keer_product_id, "商品图下载失败")
                continue

            # 7. 生成 + 质量检测（最多3次）
            final_passed    = False
            final_thumb_url = None

            for gen_attempt in range(1, MAX_GENERATE_ATTEMPTS + 1):
                logger.info(f"🎨 GeminiGen生成（第{gen_attempt}/{MAX_GENERATE_ATTEMPTS}次）...")

                if gen_attempt > 1:
                    try:
                        scene_photo = get_scene_photo(category)
                    except Exception:
                        pass

                # run_task 返回 (success, thumb_url, error_type)
                success, thumb_url, error_type = gemini_gen.run_task(
                    scene_photo=scene_photo,
                    product_image=product_local,
                    save_path=generated_local,
                    prompt_text=gen_prompt,
                )

                # ── 图片格式/解析错误：直接判定失败，不再重试 ──
                if error_type == "IMAGE_FORMAT_ERROR":
                    logger.error("❌ 图片输入格式/解析错误，任务直接判定为失败，不再重试")
                    mark_task_skipped(keer_product_id, "图片格式/解析错误")
                    _cleanup_temp_files(product_local, generated_local)
                    break  # 跳出生成循环，不进入上传流程

                if not success:
                    logger.error(f"  生成失败（第{gen_attempt}次）")
                    if gen_attempt < MAX_GENERATE_ATTEMPTS:
                        logger.info("  等待30秒后重新生成...")
                        time.sleep(30)
                    continue

                score_url = thumb_url if thumb_url else ""
                if not score_url:
                    logger.warning("  无生成图URL，视为不合格")
                    if gen_attempt < MAX_GENERATE_ATTEMPTS:
                        logger.info("  等待30秒后重新生成...")
                        time.sleep(30)
                    continue

                # ── 质量检测 ──
                q_passed, q_reason = quality_check(scene_photo, product_image, score_url)

                if q_passed:
                    logger.info("  ✅ 质量检测合格！")
                    final_passed    = True
                    final_thumb_url = thumb_url
                    break
                else:
                    logger.warning(f"  ❌ 质量检测不合格: {q_reason}（第{gen_attempt}/{MAX_GENERATE_ATTEMPTS}次）")
                    if gen_attempt < MAX_GENERATE_ATTEMPTS:
                        logger.info("  等待10秒后重新生成...")
                        time.sleep(10)

            # 已在循环内处理 IMAGE_FORMAT_ERROR（break后 final_passed=False）
            if not final_passed:
                logger.error(f"❌ {MAX_GENERATE_ATTEMPTS}次生成均未通过，跳过任务")
                if retry_level == 0:
                    # 普通任务首次失败 → 写 SKIP:3次生成均未通过质量检测，image_task_num重置为0等待重试
                    mark_task_skipped(keer_product_id, "3次生成均未通过质量检测")
                else:
                    # 一级/二级重试失败 → 升级标记（一级→*2，二级→彻底放弃）
                    mark_retry_failed(keer_product_id, retry_level)
                _cleanup_temp_files(product_local, generated_local)
                continue

            # 8. 上传 COS
            cos_filename = f"{keer_product_id}.png"
            logger.info(f"☁️ 上传COS: {cos_filename}")
            cos_url = upload_to_cos(generated_local, cos_filename)

            if not cos_url:
                mark_task_skipped(keer_product_id, "COS上传失败")
                _cleanup_temp_files(product_local, generated_local)
                continue

            # 9. 回写数据库
            update_ai_image(keer_product_id, cos_url)

            # 10. 更新 task_status
            if task_status:
                new_status = task_status + "-实拍图待确认"
                update_task_status(keer_product_id, new_status)

            # 11. 清理
            _cleanup_temp_files(product_local, generated_local)

            total = _inc_counter()
            logger.info(f"🎉 任务完成！实例#{INSTANCE_IDX} 本次累计: {total} 条")

        except KeyboardInterrupt:
            logger.info(f"Worker-{worker_id} 收到中断信号")
            return
        except Exception as e:
            logger.error(f"Worker异常: {e}")
            import traceback
            traceback.print_exc()
            logger.info("等待10秒后继续...")
            time.sleep(10)


# ============================================================
# 主入口
# ============================================================
def main():
    # 校验账号
    if not INSTANCE_USERNAME or not INSTANCE_PASSWORD:
        logger.error("❌ 未传入账号密码，请通过 start.py 启动，或手动传入 --username/--password")
        sys.exit(1)

    # 注入账号到 gemini_gen 模块
    gemini_gen.set_account(INSTANCE_USERNAME, INSTANCE_PASSWORD, INSTANCE_IDX)

    logger.info("=" * 60)
    logger.info(f"Kewen AI Pipeline 启动（实例#{INSTANCE_IDX} / {WORKER_COUNT}路并发）")
    logger.info(f"账号: {INSTANCE_USERNAME}")
    logger.info("=" * 60)

    if not validate_startup():
        sys.exit(1)

    os.makedirs(TEMP_DIR, exist_ok=True)
    logger.info(f"场景图目录: {SCENE_ROOT}")
    logger.info(f"临时文件目录: {TEMP_DIR}")

    if not gemini_gen.init_login():
        logger.error("❌ 登录失败，无法启动")
        sys.exit(1)

    # 启动 worker 线程
    threads = []
    for i in range(1, WORKER_COUNT + 1):
        t = threading.Thread(
            target=worker_loop,
            args=(i,),
            name=f"W{INSTANCE_IDX}-{i}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        logger.info(f"Worker-{i} 已启动")
        if i < WORKER_COUNT:
            time.sleep(3)

    logger.info(f"全部 {WORKER_COUNT} 个Worker已启动，等待任务...")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("\n用户中断，正在退出...")
        gemini_gen.quit_driver()
        logger.info("退出完成")


if __name__ == "__main__":
    main()