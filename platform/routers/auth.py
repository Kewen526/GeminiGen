# -*- coding: utf-8 -*-
import logging

from fastapi import APIRouter, HTTPException, Request, Depends

from ..models import (
    RegisterRequest, LoginRequest, TokenResponse, UserResponse,
    SendCodeRequest,
)
from ..auth import (
    hash_password, verify_password, create_access_token, get_current_user,
    auth_ip_limiter, login_lockout, generate_code, send_verification_email,
)
from ..config import SMTP_ENABLED, POINTS_PER_YUAN
from .. import database as db

router = APIRouter(prefix="/auth", tags=["认证"])
logger = logging.getLogger(__name__)


def _ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host or "unknown")


def _check_ip_rate(request: Request):
    if not auth_ip_limiter.is_allowed(_ip(request)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


# ── 发送验证码 ────────────────────────────────────────────────
@router.post("/send-code")
def send_code(req: SendCodeRequest, request: Request):
    _check_ip_rate(request)
    code = generate_code()
    db.store_verification_code(req.email, code, expire_minutes=5)
    sent = send_verification_email(req.email, code)
    if SMTP_ENABLED and not sent:
        raise HTTPException(status_code=503, detail="验证码发送失败，请稍后重试")
    return {"message": "验证码已发送" if sent else "验证码已生成（开发模式，请查看服务器日志）"}


# ── 注册 ──────────────────────────────────────────────────────
@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest, request: Request):
    _check_ip_rate(request)
    try:
        existing = db.get_user_by_email(req.email)
        if existing:
            raise HTTPException(status_code=400, detail="该邮箱已注册")

        # SMTP 启用时校验验证码
        if SMTP_ENABLED:
            if not req.code:
                raise HTTPException(status_code=400, detail="请先获取邮箱验证码")
            if not db.verify_email_code(req.email, req.code):
                raise HTTPException(status_code=400, detail="验证码错误或已过期")

        user_id = db.create_user(req.email, req.username, hash_password(req.password))
        if not user_id:
            raise HTTPException(status_code=400, detail="注册失败，邮箱可能已存在")

        token = create_access_token(user_id, req.email)
        balance = 0.0
        return TokenResponse(
            access_token=token,
            user_id=user_id,
            email=req.email,
            username=req.username,
            balance=balance,
            points=int(balance * POINTS_PER_YUAN),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("register failed for email=%s: %s", req.email, e)
        raise HTTPException(status_code=503, detail="注册服务暂时不可用，请稍后重试")


# ── 登录 ──────────────────────────────────────────────────────
@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, request: Request):
    _check_ip_rate(request)
    try:
        email = req.email.lower().strip()

        if login_lockout.is_locked(email):
            raise HTTPException(
                status_code=429,
                detail="登录失败次数过多，账号已被暂时锁定，请 15 分钟后再试"
            )

        user = db.get_user_by_email(email)
        if not user or not verify_password(req.password, user.get("password_hash") or ""):
            login_lockout.record(email, success=False)
            raise HTTPException(status_code=401, detail="邮箱或密码错误")

        login_lockout.record(email, success=True)
        token = create_access_token(user["id"], user["email"])
        balance = float(user["balance"])
        return TokenResponse(
            access_token=token,
            user_id=user["id"],
            email=user["email"],
            username=user.get("username"),
            balance=balance,
            points=int(balance * POINTS_PER_YUAN),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("login failed for email=%s: %s", req.email, e)
        raise HTTPException(status_code=503, detail="登录服务暂时不可用，请稍后重试")


# ── 当前用户信息 ──────────────────────────────────────────────
@router.get("/me", response_model=UserResponse)
def me(current_user: dict = Depends(get_current_user)):
    user = db.get_user_by_id(current_user["id"])
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    balance = float(user["balance"])
    return UserResponse(
        id=user["id"],
        email=user["email"],
        username=user.get("username"),
        balance=balance,
        points=int(balance * POINTS_PER_YUAN),
        total_tasks=user["total_tasks"],
        avatar_url=user.get("avatar_url"),
        email_verified=bool(user.get("email_verified", 0)),
    )


# ── Google OAuth（预留，待配置 GOOGLE_CLIENT_ID 后启用）───────
@router.get("/google")
def google_login():
    raise HTTPException(status_code=503, detail="Google 登录尚未配置，请联系管理员")
