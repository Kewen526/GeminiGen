# -*- coding: utf-8 -*-
from fastapi import APIRouter, HTTPException

from ..models import RegisterRequest, LoginRequest, TokenResponse, UserResponse
from ..auth import hash_password, verify_password, create_access_token, get_current_user
from .. import database as db
from fastapi import Depends

router = APIRouter(prefix="/auth", tags=["认证"])


@router.post("/register", response_model=TokenResponse)
def register(req: RegisterRequest):
    existing = db.get_user_by_email(req.email)
    if existing:
        raise HTTPException(status_code=400, detail="该邮箱已注册")

    user_id = db.create_user(req.email, req.username, hash_password(req.password))
    if not user_id:
        raise HTTPException(status_code=400, detail="注册失败，邮箱可能已存在")

    token = create_access_token(user_id, req.email)
    return TokenResponse(
        access_token=token,
        user_id=user_id,
        email=req.email,
        balance=0.0,
    )


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest):
    user = db.get_user_by_email(req.email)
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")

    token = create_access_token(user["id"], user["email"])
    return TokenResponse(
        access_token=token,
        user_id=user["id"],
        email=user["email"],
        balance=float(user["balance"]),
    )


@router.get("/me", response_model=UserResponse)
def me(current_user: dict = Depends(get_current_user)):
    user = db.get_user_by_id(current_user["id"])
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return UserResponse(
        id=user["id"],
        email=user["email"],
        username=user["username"],
        balance=float(user["balance"]),
        total_tasks=user["total_tasks"],
    )
