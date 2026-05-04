# -*- coding: utf-8 -*-
"""数据库工具层 —— 全部使用 pymysql 同步调用"""

import uuid
import secrets
import logging
from datetime import datetime
from typing import Optional

import pymysql
import pymysql.cursors

from .config import DB_CONFIG

logger = logging.getLogger(__name__)


def get_conn():
    return pymysql.connect(
        **DB_CONFIG,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


# ============================================================
# 用户
# ============================================================
def create_user(email: str, username: str, password_hash: str) -> Optional[int]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO platform_users (email, username, password_hash) VALUES (%s, %s, %s)",
                (email.lower().strip(), username, password_hash),
            )
        conn.commit()
        return cur.lastrowid
    except pymysql.err.IntegrityError:
        conn.rollback()
        return None
    finally:
        conn.close()


def get_user_by_email(email: str) -> Optional[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM platform_users WHERE email = %s AND is_active = 1",
                (email.lower().strip(),),
            )
            return cur.fetchone()
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> Optional[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM platform_users WHERE id = %s AND is_active = 1",
                (user_id,),
            )
            return cur.fetchone()
    finally:
        conn.close()


# ============================================================
# API Key
# ============================================================
def create_api_key(user_id: int, key_name: str) -> dict:
    key_value = "sk-" + secrets.token_hex(32)
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_keys (user_id, key_name, key_value) VALUES (%s, %s, %s)",
                (user_id, key_name, key_value),
            )
        conn.commit()
        key_id = cur.lastrowid
        return {"id": key_id, "key_name": key_name, "key_value": key_value}
    finally:
        conn.close()


def get_api_keys(user_id: int) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, key_name, key_value, total_calls, last_used_at, created_at "
                "FROM api_keys WHERE user_id = %s AND is_active = 1 ORDER BY created_at DESC",
                (user_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def deactivate_api_key(key_id: int, user_id: int) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            affected = cur.execute(
                "UPDATE api_keys SET is_active = 0 WHERE id = %s AND user_id = %s",
                (key_id, user_id),
            )
        conn.commit()
        return affected > 0
    finally:
        conn.close()


def get_user_by_api_key(key_value: str) -> Optional[dict]:
    """验证 API Key，返回对应用户信息（同时更新使用记录）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ak.id as key_id, ak.user_id, pu.email, pu.balance, pu.is_active, pu.is_admin "
                "FROM api_keys ak "
                "JOIN platform_users pu ON ak.user_id = pu.id "
                "WHERE ak.key_value = %s AND ak.is_active = 1 AND pu.is_active = 1",
                (key_value,),
            )
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE api_keys SET last_used_at = NOW(), total_calls = total_calls + 1 WHERE id = %s",
                    (row["key_id"],),
                )
                conn.commit()
            return row
    finally:
        conn.close()


# ============================================================
# 余额（原子扣费）
# ============================================================
def deduct_balance(user_id: int, amount: float, task_id: str, note: str = "") -> bool:
    """原子扣费，余额不足返回 False"""
    conn = get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT balance FROM platform_users WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            row = cur.fetchone()
            if not row or row["balance"] < amount:
                conn.rollback()
                return False
            new_bal = float(row["balance"]) - amount
            cur.execute(
                "UPDATE platform_users SET balance = %s WHERE id = %s",
                (round(new_bal, 4), user_id),
            )
            cur.execute(
                "INSERT INTO balance_transactions (user_id, amount, type, task_id, note, balance_after) "
                "VALUES (%s, %s, 'deduct', %s, %s, %s)",
                (user_id, -round(amount, 4), task_id, note, round(new_bal, 4)),
            )
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"deduct_balance error: {e}")
        return False
    finally:
        conn.close()


def add_balance(user_id: int, amount: float, tx_type: str = "recharge",
                task_id: str = "", note: str = "") -> float:
    """增加余额，返回新余额"""
    conn = get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT balance FROM platform_users WHERE id = %s FOR UPDATE",
                (user_id,),
            )
            row = cur.fetchone()
            new_bal = float(row["balance"]) + amount
            cur.execute(
                "UPDATE platform_users SET balance = %s WHERE id = %s",
                (round(new_bal, 4), user_id),
            )
            cur.execute(
                "INSERT INTO balance_transactions (user_id, amount, type, task_id, note, balance_after) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, round(amount, 4), tx_type, task_id, note, round(new_bal, 4)),
            )
        conn.commit()
        return round(new_bal, 4)
    except Exception as e:
        conn.rollback()
        logger.error(f"add_balance error: {e}")
        raise
    finally:
        conn.close()


def get_user_monthly_spend(user_id: int) -> float:
    """当月累计消费金额（元）"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(ABS(amount)), 0) AS total "
                "FROM balance_transactions "
                "WHERE user_id = %s AND type = 'deduct' "
                "AND YEAR(created_at) = YEAR(NOW()) "
                "AND MONTH(created_at) = MONTH(NOW())",
                (user_id,),
            )
            row = cur.fetchone()
            return float(row["total"]) if row else 0.0
    finally:
        conn.close()


def get_user_processing_count(user_id: int) -> int:
    """当前 pending + processing 任务数"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM gen_tasks "
                "WHERE user_id = %s AND status IN ('pending', 'processing')",
                (user_id,),
            )
            row = cur.fetchone()
            return int(row["cnt"]) if row else 0
    finally:
        conn.close()


def get_user_tasks_recent(user_id: int, days: int = 7, limit: int = 50) -> list:
    """最近 N 天任务，附带运行时长"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT task_id, model, status, cost, result_image_url, "
                "       error_msg, created_at, updated_at, prompt_text, "
                "       TIMESTAMPDIFF(SECOND, created_at, updated_at) AS duration_seconds "
                "FROM gen_tasks WHERE user_id = %s "
                "AND created_at >= NOW() - INTERVAL %s DAY "
                "ORDER BY created_at DESC LIMIT %s",
                (user_id, days, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


def delete_old_tasks(days: int = 7) -> int:
    """删除超过 N 天的任务记录，返回删除数量"""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            affected = cur.execute(
                "DELETE FROM gen_tasks WHERE created_at < NOW() - INTERVAL %s DAY",
                (days,),
            )
        conn.commit()
        return affected
    except Exception as e:
        conn.rollback()
        logger.error(f"delete_old_tasks error: {e}")
        return 0
    finally:
        conn.close()


def store_verification_code(email: str, code: str, expire_minutes: int = 5) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE email_verification_codes SET used = 1 WHERE email = %s AND used = 0",
                (email.lower(),),
            )
            cur.execute(
                "INSERT INTO email_verification_codes (email, code, expires_at) "
                "VALUES (%s, %s, DATE_ADD(NOW(), INTERVAL %s MINUTE))",
                (email.lower(), code, expire_minutes),
            )
        conn.commit()
    finally:
        conn.close()


def verify_email_code(email: str, code: str) -> bool:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM email_verification_codes "
                "WHERE email = %s AND code = %s AND used = 0 AND expires_at > NOW()",
                (email.lower(), code),
            )
            row = cur.fetchone()
            if not row:
                return False
            cur.execute(
                "UPDATE email_verification_codes SET used = 1 WHERE id = %s",
                (row["id"],),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def get_transactions(user_id: int, limit: int = 50) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, amount, type, task_id, note, balance_after, created_at "
                "FROM balance_transactions WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (user_id, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ============================================================
# 生成任务
# ============================================================
def create_task(user_id: int, model: str, product_image_url: str,
                scene_image_url: str, prompt_text: str,
                cost: float, api_key_id: Optional[int] = None,
                aspect_ratio: str = "1:1", resolution: str = "1K",
                output_format: str = "PNG") -> str:
    task_id = str(uuid.uuid4())
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO gen_tasks "
                "(task_id, user_id, api_key_id, model, product_image_url, "
                " scene_image_url, prompt_text, cost, status, "
                " aspect_ratio, resolution, output_format) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)",
                (task_id, user_id, api_key_id, model,
                 product_image_url, scene_image_url, prompt_text, cost,
                 aspect_ratio, resolution, output_format),
            )
        conn.commit()
        return task_id
    finally:
        conn.close()


def get_task(task_id: str) -> Optional[dict]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM gen_tasks WHERE task_id = %s", (task_id,))
            return cur.fetchone()
    finally:
        conn.close()


def get_user_tasks(user_id: int, limit: int = 20, offset: int = 0) -> list:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT task_id, model, status, cost, result_image_url, "
                "       error_msg, created_at, updated_at "
                "FROM gen_tasks WHERE user_id = %s "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (user_id, limit, offset),
            )
            return cur.fetchall()
    finally:
        conn.close()


def claim_pending_task() -> Optional[dict]:
    """原子性取一条 pending 任务并标记为 processing（多 worker 安全）"""
    conn = get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM gen_tasks WHERE status = 'pending' "
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
        logger.error(f"claim_pending_task error: {e}")
        return None
    finally:
        conn.close()


def finish_task(task_id: str, result_url: str) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE gen_tasks SET status = 'success', result_image_url = %s, "
                "updated_at = NOW() WHERE task_id = %s",
                (result_url, task_id),
            )
            # 同步更新 total_tasks 计数
            cur.execute(
                "UPDATE platform_users pu "
                "JOIN gen_tasks gt ON pu.id = gt.user_id "
                "SET pu.total_tasks = pu.total_tasks + 1 "
                "WHERE gt.task_id = %s",
                (task_id,),
            )
        conn.commit()
    finally:
        conn.close()


def fail_task(task_id: str, error_msg: str, refund: bool = True) -> None:
    conn = get_conn()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE gen_tasks SET status = 'failed', error_msg = %s, "
                "updated_at = NOW() WHERE task_id = %s",
                (error_msg[:500], task_id),
            )
            if refund:
                cur.execute(
                    "SELECT user_id, cost FROM gen_tasks WHERE task_id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                if row and row["cost"]:
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
    finally:
        conn.close()
