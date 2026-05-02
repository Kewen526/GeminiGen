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
    model: str = "nano-banana-2"
    prompt: str
    reference_image_url: Optional[str] = None
    aspect_ratio: str = "1:1"
    output_format: str = "png"
    resolution: str = "1K"

    @field_validator("model")
    @classmethod
    def valid_model(cls, v):
        allowed = {"nano-banana-2", "nano-banana-pro"}
        if v not in allowed:
            raise ValueError(f"model 只支持: {', '.join(allowed)}")
        return v

    @field_validator("aspect_ratio")
    @classmethod
    def valid_ratio(cls, v):
        allowed = {"1:1", "16:9", "9:16", "3:4", "4:3"}
        if v not in allowed:
            raise ValueError(f"aspect_ratio 只支持: {', '.join(allowed)}")
        return v

    @field_validator("output_format")
    @classmethod
    def valid_format(cls, v):
        v = v.lower()
        if v not in {"png", "jpeg"}:
            raise ValueError("output_format 只支持 png / jpeg")
        return v

    @field_validator("resolution")
    @classmethod
    def valid_resolution(cls, v):
        v = v.upper()
        if v not in {"1K", "2K", "4K"}:
            raise ValueError("resolution 只支持 1K / 2K / 4K")
        return v


class TaskResponse(BaseModel):
    task_id: str
    status: str           # pending | processing | success | failed
    model: str
    prompt: Optional[str]
    aspect_ratio: Optional[str]
    output_format: Optional[str]
    resolution: Optional[str]
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
