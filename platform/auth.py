# -*- coding: utf-8 -*-
"""JWT、API Key 认证，及限流/登录锁定工具"""

import random
import smtplib
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import (
    SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SMTP_ENABLED,
    AUTH_RATE_LIMIT, GENERATE_RATE_LIMIT, MAX_LOGIN_ATTEMPTS, LOCKOUT_MINUTES,
    POINTS_PER_YUAN,
)
from . import database as db

pwd_ctx = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


# ── 密码 ──────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return pwd_ctx.verify(plain, hashed)
    except Exception:
        return False


# ── JWT ──────────────────────────────────────────────────────
def create_access_token(user_id: int, email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "email": email, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _decode_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


# ── FastAPI 依赖 ──────────────────────────────────────────────
def _unauth(detail: str):
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict:
    if not credentials:
        _unauth("未提供认证信息")

    token = credentials.credentials

    if token.startswith("sk-"):
        user = db.get_user_by_api_key(token)
        if not user:
            _unauth("无效的 API Key")
        return {"id": user["user_id"], "email": user["email"],
                "balance": float(user["balance"]), "is_admin": user["is_admin"],
                "key_id": user["key_id"]}

    payload = _decode_token(token)
    if not payload:
        _unauth("无效或已过期的 Token")

    user = db.get_user_by_id(int(payload["sub"]))
    if not user:
        _unauth("用户不存在")

    return {"id": user["id"], "email": user["email"],
            "balance": float(user["balance"]), "is_admin": user["is_admin"],
            "key_id": None}


def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return current_user


# ── 通用限流器 ────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_calls: int, period_seconds: int = 60):
        self._max = max_calls
        self._period = period_seconds
        self._calls: dict[str, list] = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            calls = self._calls[key]
            calls[:] = [t for t in calls if now - t < self._period]
            if len(calls) >= self._max:
                return False
            calls.append(now)
            return True

    def cleanup(self):
        now = time.time()
        with self._lock:
            for key in list(self._calls):
                self._calls[key] = [t for t in self._calls[key] if now - t < self._period]
                if not self._calls[key]:
                    del self._calls[key]


# 全局限流器实例
auth_ip_limiter       = RateLimiter(max_calls=AUTH_RATE_LIMIT,     period_seconds=60)
generate_user_limiter = RateLimiter(max_calls=GENERATE_RATE_LIMIT, period_seconds=60)
task_poll_limiter     = RateLimiter(max_calls=60,                   period_seconds=60)


# ── 登录锁定 ──────────────────────────────────────────────────
class LoginLockout:
    def __init__(self, max_attempts: int = 5, lockout_minutes: int = 15):
        self._max = max_attempts
        self._window = lockout_minutes * 60
        self._data: dict[str, list] = {}  # email -> [(ts, success)]
        self._lock = threading.Lock()

    def is_locked(self, email: str) -> bool:
        now = time.time()
        with self._lock:
            entries = self._data.get(email.lower(), [])
            recent_fails = [e for e in entries if now - e[0] < self._window and not e[1]]
            return len(recent_fails) >= self._max

    def record(self, email: str, success: bool):
        now = time.time()
        with self._lock:
            key = email.lower()
            if key not in self._data:
                self._data[key] = []
            self._data[key].append((now, success))
            if success:
                self._data[key] = [(now, True)]
            else:
                self._data[key] = [e for e in self._data[key] if now - e[0] < self._window * 2]


login_lockout = LoginLockout(max_attempts=MAX_LOGIN_ATTEMPTS, lockout_minutes=LOCKOUT_MINUTES)


# ── 并发档位 ──────────────────────────────────────────────────
def get_max_concurrent(monthly_spend_yuan: float) -> int:
    if monthly_spend_yuan >= 100:
        return 5
    elif monthly_spend_yuan >= 50:
        return 3
    elif monthly_spend_yuan >= 10:
        return 2
    return 1


# ── 邮件验证码 ────────────────────────────────────────────────
def generate_code() -> str:
    return str(random.randint(100000, 999999))


def send_verification_email(email: str, code: str) -> bool:
    """发送验证码邮件。返回 True 表示发送成功，False 表示未配置 SMTP。"""
    if not SMTP_ENABLED:
        import logging
        logging.getLogger(__name__).info(f"[DEV] 验证码 {code} → {email}（SMTP 未配置，仅打印）")
        return False

    try:
        body = f"""您好，

您的 GeminiGen 注册验证码为：

  {code}

验证码 5 分钟内有效，请勿泄露给他人。

— GeminiGen 团队
"""
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"GeminiGen 注册验证码：{code}"
        msg["From"]    = SMTP_FROM
        msg["To"]      = email

        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10) as s:
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(SMTP_FROM, [email], msg.as_string())
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as s:
                s.starttls()
                s.login(SMTP_USER, SMTP_PASSWORD)
                s.sendmail(SMTP_FROM, [email], msg.as_string())
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"发送验证码失败: {e}")
        return False
