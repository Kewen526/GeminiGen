# -*- coding: utf-8 -*-
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator


# ── Auth ─────────────────────────────────────────────────────
class SendCodeRequest(BaseModel):
    email: EmailStr


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str
    code: Optional[str] = None  # 邮箱验证码（SMTP 启用时必填）

    @field_validator("password")
    @classmethod
    def password_length(cls, v):
        if len(v) < 6:
            raise ValueError("密码至少6位")
        return v

    @field_validator("username")
    @classmethod
    def username_length(cls, v):
        v = v.strip()
        if len(v) < 1 or len(v) > 30:
            raise ValueError("用户名 1-30 个字符")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str
    username: Optional[str] = None
    balance: float
    points: int = 0


# ── 用户 ──────────────────────────────────────────────────────
class UserResponse(BaseModel):
    id: int
    email: str
    username: Optional[str]
    balance: float
    points: int = 0
    total_tasks: int
    avatar_url: Optional[str] = None
    email_verified: bool = False
    monthly_spend_points: int = 0
    max_concurrent: int = 1


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
    product_image_url: Optional[str] = None   # 参考图可选
    model: str = "nano-banana-2"
    scene_image_url: Optional[str] = None
    prompt: Optional[str] = None
    aspect_ratio: Optional[str] = "1:1"
    resolution: Optional[str] = "1K"
    output_format: Optional[str] = "PNG"

    @field_validator("model")
    @classmethod
    def valid_model(cls, v):
        allowed = {"nano-banana-2", "nano-banana-pro"}
        if v not in allowed:
            raise ValueError(f"model 只支持: {', '.join(allowed)}")
        return v

    @field_validator("aspect_ratio")
    @classmethod
    def valid_aspect_ratio(cls, v):
        allowed = {"1:1", "16:9", "9:16", "3:4", "4:3"}
        if v and v not in allowed:
            raise ValueError(f"aspect_ratio 只支持: {', '.join(allowed)}")
        return v or "1:1"

    @field_validator("resolution")
    @classmethod
    def valid_resolution(cls, v):
        allowed = {"1K", "2K", "4K"}
        if v and v not in allowed:
            raise ValueError(f"resolution 只支持: {', '.join(allowed)}")
        return v or "1K"

    @field_validator("output_format")
    @classmethod
    def valid_output_format(cls, v):
        allowed = {"PNG", "JPEG"}
        if v and v.upper() not in allowed:
            raise ValueError(f"output_format 只支持: {', '.join(allowed)}")
        return (v or "PNG").upper()


class TaskResponse(BaseModel):
    task_id: str
    status: str           # pending | processing | success | failed
    model: str
    cost: Optional[float]
    points_cost: Optional[int] = None
    result_image_url: Optional[str]
    error_msg: Optional[str]
    created_at: str
    updated_at: str
    duration_seconds: Optional[int] = None
    prompt_text: Optional[str] = None


# ── 余额 ──────────────────────────────────────────────────────
class BalanceResponse(BaseModel):
    balance: float
    points: int = 0


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
