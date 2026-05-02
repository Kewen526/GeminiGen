# -*- coding: utf-8 -*-
"""
数据库操作模块
表：quote_iw.quotation_task_detail
原子性取任务：SELECT FOR UPDATE SKIP LOCKED + UPDATE 在同一事务内完成。
SKIP LOCKED：跳过已被其他进程/机器锁住的行，彻底杜绝多进程重复取任务。
（需要 MySQL 8.0+ / MariaDB 10.6+）
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict

import pymysql
import pymysql.cursors

logger = logging.getLogger(__name__)

DB_CONFIG = {
    "host":            "47.95.157.46",
    "port":            3306,
    "user":            "root",
    "password":        "root@kunkun",
    "database":        "quote_iw",
    "charset":         "utf8mb4",
    "cursorclass":     pymysql.cursors.DictCursor,
    "connect_timeout": 10,
}


def get_connection():
    return pymysql.connect(**DB_CONFIG)


def fetch_and_claim_task(max_days_back: int = 7) -> Optional[Dict]:
    """
    原子性取任务：
    - SELECT FOR UPDATE SKIP LOCKED：跳过已被其他进程锁住的行（多机/多进程安全）
    - 立即 UPDATE image_task_num=1 标记占用
    - 同一事务提交，保证原子性
    """
    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            for days_back in range(max_days_back):
                target_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
                sql = """
                    SELECT keer_product_id, product_image, product_title, task_status, created_at
                    FROM quotation_task_detail
                    WHERE ai_image IS NULL
                      AND task_status LIKE %s
                      AND (image_task_num IS NULL OR image_task_num = 0)
                      AND DATE(created_at) = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """
                cur.execute(sql, ("%报价单创建完毕%", target_date))
                row = cur.fetchone()
                if row:
                    cur.execute(
                        "UPDATE quotation_task_detail SET image_task_num = 1 WHERE keer_product_id = %s",
                        (row["keer_product_id"],)
                    )
                    conn.commit()
                    logger.info(
                        f"获取到任务 [date={target_date}] "
                        f"keer_product_id={row['keer_product_id']}"
                    )
                    return row
        conn.commit()
        return None
    except Exception as e:
        logger.error(f"取任务异常: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        conn.close()


def update_ai_image(keer_product_id: str, ai_image_url: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql      = "UPDATE quotation_task_detail SET ai_image = %s WHERE keer_product_id = %s"
            affected = cur.execute(sql, (ai_image_url, keer_product_id))
            conn.commit()
            if affected:
                logger.info(f"✅ 回写成功 keer_product_id={keer_product_id} → {ai_image_url}")
                return True
            else:
                logger.warning(f"⚠ 回写未影响任何行 keer_product_id={keer_product_id}")
                return False
    except Exception as e:
        logger.error(f"❌ 回写数据库失败: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_task_status(keer_product_id: str, new_status: str) -> bool:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql      = "UPDATE quotation_task_detail SET task_status = %s WHERE keer_product_id = %s"
            affected = cur.execute(sql, (new_status, keer_product_id))
            conn.commit()
            if affected:
                logger.info(f"✅ task_status更新: keer_product_id={keer_product_id} → {new_status}")
                return True
            return False
    except Exception as e:
        logger.error(f"❌ 更新task_status失败: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def mark_task_skipped(keer_product_id: str, reason: str = "生成失败") -> None:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                # 同时重置 image_task_num=0，让重试队列可以取到此行
                "UPDATE quotation_task_detail SET ai_image = %s, image_task_num = 0 WHERE keer_product_id = %s",
                (f"SKIP:{reason}", keer_product_id)
            )
            conn.commit()
            logger.info(f"⚠ 任务 keer_product_id={keer_product_id} 已标记跳过: {reason}")
    except Exception as e:
        logger.error(f"标记跳过失败: {e}")
    finally:
        conn.close()


# ============================================================
# 重试任务常量
# ============================================================
SKIP_LEVEL1 = "SKIP:3次生成均未通过质量检测"       # 第一次失败写入的标记
SKIP_LEVEL2 = "SKIP:3次生成均未通过质量检测*2"     # 第二次失败写入的标记
SKIP_FINAL  = "SKIP:彻底放弃"                      # 彻底放弃


def fetch_retry_task(level: int = 1) -> Optional[Dict]:
    """
    无普通任务时，按重试等级取任务。
    level=1: 取 ai_image = SKIP_LEVEL1 的行
    level=2: 取 ai_image = SKIP_LEVEL2 的行

    原子性同 fetch_and_claim_task：SELECT FOR UPDATE SKIP LOCKED + 立即 UPDATE image_task_num=1。
    """
    if level == 1:
        target_mark = SKIP_LEVEL1
    elif level == 2:
        target_mark = SKIP_LEVEL2
    else:
        raise ValueError(f"不支持的重试等级: {level}")

    conn = get_connection()
    try:
        conn.begin()
        with conn.cursor() as cur:
            sql = """
                SELECT keer_product_id, product_image, product_title, task_status, created_at, ai_image
                FROM quotation_task_detail
                WHERE ai_image = %s
                  AND (image_task_num IS NULL OR image_task_num = 0)
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """
            cur.execute(sql, (target_mark,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE quotation_task_detail SET image_task_num = 1 WHERE keer_product_id = %s",
                    (row["keer_product_id"],)
                )
                conn.commit()
                logger.info(
                    f"获取到重试任务 [level={level}] "
                    f"keer_product_id={row['keer_product_id']}"
                )
                return row
        conn.commit()
        return None
    except Exception as e:
        logger.error(f"取重试任务异常: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None
    finally:
        conn.close()


def mark_retry_failed(keer_product_id: str, level: int) -> None:
    """
    重试失败后回写对应等级的失败标记，并重置 image_task_num=0（下轮可再取）。
    level=1 → 写 SKIP_LEVEL2
    level=2 → 写 SKIP_FINAL（彻底放弃）
    """
    if level == 1:
        new_mark = SKIP_LEVEL2
    else:
        new_mark = SKIP_FINAL

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE quotation_task_detail SET ai_image = %s, image_task_num = 0 "
                "WHERE keer_product_id = %s",
                (new_mark, keer_product_id)
            )
            conn.commit()
            logger.info(f"重试失败标记: keer_product_id={keer_product_id} → {new_mark}")
    except Exception as e:
        logger.error(f"标记重试失败异常: {e}")
    finally:
        conn.close()