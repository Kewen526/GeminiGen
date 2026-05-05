# -*- coding: utf-8 -*-
import os
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request

from ..models import GenerateRequest, VideoGenerateRequest, TaskResponse
from ..auth import get_current_user, generate_user_limiter, task_poll_limiter, get_max_concurrent
from ..config import MODEL_PRICES, DEFAULT_MODEL, TEMP_DIR, SENSITIVE_WORDS, POINTS_PER_YUAN, VIDEO_MODELS
from .. import database as db
from ..upload_helper import save_upload_to_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["生成"])


def _task_to_response(row: dict) -> TaskResponse:
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    cost = float(row["cost"]) if row.get("cost") else None
    return TaskResponse(
        task_id=row["task_id"],
        status=row["status"],
        model=row["model"],
        task_type=row.get("task_type") or "image",
        cost=cost,
        points_cost=int(cost * POINTS_PER_YUAN) if cost else None,
        result_image_url=row.get("result_image_url"),
        result_video_url=row.get("result_video_url"),
        error_msg=row.get("error_msg"),
        created_at=_fmt(row["created_at"]),
        updated_at=_fmt(row["updated_at"]),
        duration_seconds=row.get("duration_seconds"),
        prompt_text=row.get("prompt_text"),
    )


def _check_prompt(prompt: Optional[str]):
    if not prompt:
        return
    lower = prompt.lower()
    for word in SENSITIVE_WORDS:
        if word in lower:
            raise HTTPException(status_code=400, detail="提示词包含违禁内容，请修改后重试")


def _check_concurrent(user_id: int):
    monthly_spend = db.get_user_monthly_spend(user_id)
    max_c = get_max_concurrent(monthly_spend)
    current = db.get_user_processing_count(user_id)
    if current >= max_c:
        raise HTTPException(
            status_code=429,
            detail=f"并发任务数已达上限（当前档位最多 {max_c} 个）。请等待当前任务完成后再提交。"
        )


def _check_rate(request: Request, user_id: int):
    if not generate_user_limiter.is_allowed(str(user_id)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")


# ── JSON 提交（API 调用）─────────────────────────────────────
@router.post("/generate", response_model=TaskResponse)
def generate(req: GenerateRequest, request: Request, current_user: dict = Depends(get_current_user)):
    _check_rate(request, current_user["id"])
    _check_prompt(req.prompt)
    _check_concurrent(current_user["id"])

    model = req.model or DEFAULT_MODEL
    cost  = MODEL_PRICES.get(model, MODEL_PRICES[DEFAULT_MODEL])

    if current_user["balance"] < cost:
        raise HTTPException(status_code=402, detail=f"积分不足，当前余额 {int(current_user['balance'] * POINTS_PER_YUAN)} 积分，需要 {int(cost * POINTS_PER_YUAN)} 积分")

    task_id = db.create_task(
        user_id=current_user["id"],
        model=model,
        product_image_url=req.product_image_url or "",
        scene_image_url=req.scene_image_url or "",
        prompt_text=req.prompt or "",
        cost=cost,
        api_key_id=current_user.get("key_id"),
        aspect_ratio=req.aspect_ratio or "1:1",
        resolution=req.resolution or "1K",
        output_format=req.output_format or "PNG",
    )

    ok = db.deduct_balance(current_user["id"], cost, task_id, f"生成任务 {model}")
    if not ok:
        db.fail_task(task_id, "余额不足", refund=False)
        raise HTTPException(status_code=402, detail="积分不足")

    row = db.get_task(task_id)
    return _task_to_response(row)


# ── 表单上传（网页使用）──────────────────────────────────────
@router.post("/generate/upload", response_model=TaskResponse)
async def generate_upload(
    request: Request,
    model: str = Form(DEFAULT_MODEL),
    product_image: Optional[UploadFile] = File(None),   # 参考图可选
    prompt: Optional[str] = Form(None),
    aspect_ratio: Optional[str] = Form("1:1"),
    resolution: Optional[str] = Form("1K"),
    output_format: Optional[str] = Form("PNG"),
    current_user: dict = Depends(get_current_user),
):
    _check_rate(request, current_user["id"])
    _check_prompt(prompt)
    _check_concurrent(current_user["id"])

    if model not in MODEL_PRICES:
        model = DEFAULT_MODEL

    valid_ratios = {"1:1", "16:9", "9:16", "3:4", "4:3"}
    if aspect_ratio not in valid_ratios:
        aspect_ratio = "1:1"
    valid_resolutions = {"1K", "2K", "4K"}
    if resolution not in valid_resolutions:
        resolution = "1K"
    output_format = (output_format or "PNG").upper()
    if output_format not in {"PNG", "JPEG"}:
        output_format = "PNG"

    cost = MODEL_PRICES[model]
    if current_user["balance"] < cost:
        raise HTTPException(status_code=402, detail=f"积分不足，需要 {int(cost * POINTS_PER_YUAN)} 积分")

    os.makedirs(TEMP_DIR, exist_ok=True)

    # 参考图可选
    product_url = ""
    if product_image and product_image.filename:
        product_url = await save_upload_to_url(product_image, TEMP_DIR)

    task_id = db.create_task(
        user_id=current_user["id"],
        model=model,
        product_image_url=product_url,
        scene_image_url="",
        prompt_text=(prompt or "").strip()[:500],
        cost=cost,
        api_key_id=current_user.get("key_id"),
        aspect_ratio=aspect_ratio,
        resolution=resolution,
        output_format=output_format,
    )

    ok = db.deduct_balance(current_user["id"], cost, task_id, f"生成任务 {model}")
    if not ok:
        db.fail_task(task_id, "余额不足", refund=False)
        raise HTTPException(status_code=402, detail="积分不足")

    row = db.get_task(task_id)
    return _task_to_response(row)


# ── 查询任务状态 ──────────────────────────────────────────────
@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    if not task_poll_limiter.is_allowed(str(current_user["id"])):
        raise HTTPException(status_code=429, detail="轮询过于频繁，请稍后再试")
    row = db.get_task(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["user_id"] != current_user["id"] and not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    return _task_to_response(row)


# ── 任务列表（近 7 天，含运行时长）──────────────────────────
@router.get("/tasks")
def list_tasks(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    rows = db.get_user_tasks_recent(current_user["id"], days=7, limit=min(limit, 100))

    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)

    return [
        {
            "task_id":           r["task_id"],
            "model":             r["model"],
            "task_type":         r.get("task_type") or "image",
            "status":            r["status"],
            "cost":              float(r["cost"]) if r.get("cost") else None,
            "points_cost":       int(float(r["cost"]) * POINTS_PER_YUAN) if r.get("cost") else None,
            "result_image_url":  r.get("result_image_url"),
            "result_video_url":  r.get("result_video_url"),
            "error_msg":         r.get("error_msg"),
            "prompt_text":       (r.get("prompt_text") or "")[:60],
            "created_at":        _fmt(r["created_at"]),
            "duration_seconds":  r.get("duration_seconds"),
        }
        for r in rows
    ]


# ── 视频生成（JSON 提交，API 调用）────────────────────────────
@router.post("/generate/video", response_model=TaskResponse)
def generate_video(req: VideoGenerateRequest, request: Request,
                   current_user: dict = Depends(get_current_user)):
    _check_rate(request, current_user["id"])
    _check_prompt(req.prompt)
    _check_concurrent(current_user["id"])

    if req.model not in VIDEO_MODELS:
        raise HTTPException(status_code=400, detail=f"不支持的视频模型: {req.model}")

    cost = MODEL_PRICES.get(req.model)
    if current_user["balance"] < cost:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足，需要 {int(cost * POINTS_PER_YUAN)} 积分"
        )

    task_id = db.create_task(
        user_id=current_user["id"],
        model=req.model,
        product_image_url=req.ref_image_url or "",
        scene_image_url="",
        prompt_text=(req.prompt or "").strip()[:2000],
        cost=cost,
        api_key_id=current_user.get("key_id"),
        aspect_ratio=req.aspect_ratio or "16:9",
        resolution=req.resolution or "1080p",
        output_format="MP4",
        task_type="video",
        video_duration=req.duration,
        video_mode_image=req.mode_image,
    )

    ok = db.deduct_balance(current_user["id"], cost, task_id, f"视频生成 {req.model}")
    if not ok:
        db.fail_task(task_id, "余额不足", refund=False)
        raise HTTPException(status_code=402, detail="积分不足")

    row = db.get_task(task_id)
    return _task_to_response(row)


# ── 视频生成（表单上传，网页使用）─────────────────────────────
@router.post("/generate/video/upload", response_model=TaskResponse)
async def generate_video_upload(
    request: Request,
    model: str = Form("veo-3-fast"),
    prompt: str = Form(...),
    aspect_ratio: Optional[str] = Form("16:9"),
    resolution: Optional[str] = Form("1080p"),
    duration: Optional[int] = Form(8),
    enhance_prompt: Optional[bool] = Form(True),
    mode_image: Optional[str] = Form("ingredient"),
    ref_image: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user),
):
    _check_rate(request, current_user["id"])
    _check_prompt(prompt)
    _check_concurrent(current_user["id"])

    if model not in VIDEO_MODELS:
        model = "veo-3-fast"

    valid_ratios = {"16:9", "9:16", "1:1", "landscape", "portrait", "square"}
    if aspect_ratio not in valid_ratios:
        aspect_ratio = "16:9"

    cost = MODEL_PRICES.get(model, MODEL_PRICES["veo-3-fast"])
    if current_user["balance"] < cost:
        raise HTTPException(
            status_code=402,
            detail=f"积分不足，需要 {int(cost * POINTS_PER_YUAN)} 积分"
        )

    os.makedirs(TEMP_DIR, exist_ok=True)

    ref_image_url = ""
    if ref_image and ref_image.filename:
        ref_image_url = await save_upload_to_url(ref_image, TEMP_DIR)

    task_id = db.create_task(
        user_id=current_user["id"],
        model=model,
        product_image_url=ref_image_url,
        scene_image_url="",
        prompt_text=prompt.strip()[:2000],
        cost=cost,
        api_key_id=current_user.get("key_id"),
        aspect_ratio=aspect_ratio,
        resolution=resolution or "1080p",
        output_format="MP4",
        task_type="video",
        video_duration=duration,
        video_mode_image=mode_image,
    )

    ok = db.deduct_balance(current_user["id"], cost, task_id, f"视频生成 {model}")
    if not ok:
        db.fail_task(task_id, "余额不足", refund=False)
        raise HTTPException(status_code=402, detail="积分不足")

    row = db.get_task(task_id)
    return _task_to_response(row)
