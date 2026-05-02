# -*- coding: utf-8 -*-
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator


# ── Auth ─────────────────────────────────────────────────────
class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str

    @field_validator("password")
    @classmethod
    def password_length(cls, v):
        if len(v) < 6:
            raise ValueError("密码至少6位")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str
    balance: float


# ── 用户 ──────────────────────────────────────────────────────
class UserResponse(BaseModel):
    id: int
    email: str
    username: Optional[str]
    balance: float
    total_tasks: int


# ── API Key ───────────────────────────────────────────────────
class ApiKeyCreate(BaseModel):
    key_name: str = "default"


class ApiKeyResponse(BaseModel):
    id: int
    key_name: str
    key_value: str
    total_calls: int
    last_used_at: Optional[str]
    created_at: str


# ── 生成任务 ──────────────────────────────────────────────────
class GenerateRequest(BaseModel):
    product_image_url: str
    model: str = "nano-banana-2"
    scene_image_url: Optional[str] = None
    prompt: Optional[str] = None

    @field_validator("model")
    @classmethod
    def valid_model(cls, v):
        allowed = {"nano-banana-2", "nano-banana-pro"}
        if v not in allowed:
            raise ValueError(f"model 只支持: {', '.join(allowed)}")
        return v


class TaskResponse(BaseModel):
    task_id: str
    status: str           # pending | processing | success | failed
    model: str
    cost: Optional[float]
    result_image_url: Optional[str]
    error_msg: Optional[str]
    created_at: str
    updated_at: str


# ── 余额 ──────────────────────────────────────────────────────
class BalanceResponse(BaseModel):
    balance: float


class AdminRechargeRequest(BaseModel):
    user_id: int
    amount: float
    note: str = ""


class TransactionResponse(BaseModel):
    id: int
    amount: float
    type: str
    task_id: Optional[str]
    note: Optional[str]
    balance_after: Optional[float]
    created_at: str
