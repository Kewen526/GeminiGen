# -*- coding: utf-8 -*-
from typing import List, Optional
from pydantic import BaseModel, EmailStr, field_validator, model_validator


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


# ── 图片生成任务 ───────────────────────────────────────────────
class GenerateRequest(BaseModel):
    product_image_url: Optional[str] = None        # 单张参考图 URL（向后兼容）
    product_image_urls: Optional[List[str]] = None  # 多张参考图 URL 列表（最多5张）
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


# ── 视频生成任务 ───────────────────────────────────────────────
class VideoGenerateRequest(BaseModel):
    model: str = "veo-3-fast"
    prompt: str
    aspect_ratio: Optional[str] = "16:9"
    resolution: Optional[str] = "1080p"
    duration: Optional[int] = 8        # 秒，Veo 支持 5/8，Grok 支持 5/10
    enhance_prompt: Optional[bool] = True
    mode_image: Optional[str] = "ingredient"  # ingredient | reference（Veo 有图时用）
    ref_image_url: Optional[str] = None       # 参考图 URL（可选）

    @field_validator("model")
    @classmethod
    def valid_model(cls, v):
        allowed = {"grok-video", "veo-3-fast"}
        if v not in allowed:
            raise ValueError(f"model 只支持: {', '.join(allowed)}")
        return v

    @field_validator("aspect_ratio")
    @classmethod
    def valid_aspect_ratio(cls, v):
        allowed = {"16:9", "9:16", "1:1", "landscape", "portrait", "square"}
        if v and v not in allowed:
            raise ValueError(f"aspect_ratio 只支持: {', '.join(allowed)}")
        return v or "16:9"

    @model_validator(mode="after")
    def valid_model_params(self):
        model = self.model
        res   = self.resolution
        dur   = self.duration
        if model == "grok-video":
            if res and res not in {"480p"}:
                raise ValueError("grok-video 只支持 resolution=480p")
            if dur is not None and dur not in {5, 6}:
                raise ValueError("grok-video 只支持 duration=5/6 秒")
        elif model == "veo-3-fast":
            if res and res not in {"480p", "720p", "1080p"}:
                raise ValueError("veo-3-fast 只支持 resolution=480p/720p/1080p")
            if dur is not None and dur not in {5, 8}:
                raise ValueError("veo-3-fast 只支持 duration=5/8 秒")
        return self


class TaskResponse(BaseModel):
    task_id: str
    status: str           # pending | processing | success | failed
    model: str
    task_type: str = "image"
    cost: Optional[float]
    points_cost: Optional[int] = None
    result_image_url: Optional[str] = None
    result_video_url: Optional[str] = None
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
