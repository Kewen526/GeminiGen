# -*- coding: utf-8 -*-
"""JWT 与 API Key 认证工具"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext

from .config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from . import database as db

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False)


# ── 密码 ──────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


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
    """接受 JWT 或 API Key，返回用户信息 dict"""
    if not credentials:
        _unauth("未提供认证信息")

    token = credentials.credentials

    # 1. 尝试 API Key（以 sk- 开头）
    if token.startswith("sk-"):
        user = db.get_user_by_api_key(token)
        if not user:
            _unauth("无效的 API Key")
        return {"id": user["user_id"], "email": user["email"],
                "balance": float(user["balance"]), "is_admin": user["is_admin"],
                "key_id": user["key_id"]}

    # 2. 尝试 JWT
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
