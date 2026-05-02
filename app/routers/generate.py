# -*- coding: utf-8 -*-
import os
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form

from ..models import GenerateRequest, TaskResponse
from ..auth import get_current_user
from ..config import MODEL_PRICES, DEFAULT_MODEL, TEMP_DIR
from .. import database as db
from ..upload_helper import save_upload_to_url

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["生成"])


def _task_to_response(row: dict) -> TaskResponse:
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    return TaskResponse(
        task_id=row["task_id"],
        status=row["status"],
        model=row["model"],
        prompt=row.get("prompt_text"),
        aspect_ratio=row.get("aspect_ratio"),
        output_format=row.get("output_format"),
        resolution=row.get("resolution"),
        cost=float(row["cost"]) if row["cost"] else None,
        result_image_url=row.get("result_image_url"),
        error_msg=row.get("error_msg"),
        created_at=_fmt(row["created_at"]),
        updated_at=_fmt(row["updated_at"]),
    )


# ── 通过 JSON 提交（API 调用方式）────────────────────────────
@router.post("/generate", response_model=TaskResponse)
def generate(req: GenerateRequest, current_user: dict = Depends(get_current_user)):
    model = req.model or DEFAULT_MODEL
    cost  = MODEL_PRICES.get(model, MODEL_PRICES[DEFAULT_MODEL])

    if current_user["balance"] < cost:
        raise HTTPException(status_code=402, detail=f"余额不足，当前余额 ¥{current_user['balance']:.4f}，需要 ¥{cost}")

    task_id = db.create_task(
        user_id=current_user["id"],
        model=model,
        prompt_text=req.prompt,
        cost=cost,
        aspect_ratio=req.aspect_ratio,
        output_format=req.output_format,
        resolution=req.resolution,
        reference_image_url=req.reference_image_url or "",
        api_key_id=current_user.get("key_id"),
    )

    ok = db.deduct_balance(current_user["id"], cost, task_id, f"生成任务 {model}")
    if not ok:
        db.fail_task(task_id, "余额不足", refund=False)
        raise HTTPException(status_code=402, detail="余额不足")

    row = db.get_task(task_id)
    return _task_to_response(row)


# ── 通过文件上传（网页使用）──────────────────────────────────
@router.post("/generate/upload", response_model=TaskResponse)
async def generate_upload(
    prompt: str = Form(...),
    model: str = Form(DEFAULT_MODEL),
    aspect_ratio: str = Form("1:1"),
    output_format: str = Form("png"),
    resolution: str = Form("1K"),
    reference_image: Optional[UploadFile] = File(None),
    current_user: dict = Depends(get_current_user),
):
    if model not in MODEL_PRICES:
        raise HTTPException(status_code=400, detail=f"不支持的模型: {model}")
    if aspect_ratio not in {"1:1", "16:9", "9:16", "3:4", "4:3"}:
        raise HTTPException(status_code=400, detail=f"不支持的纵横比: {aspect_ratio}")
    if output_format.lower() not in {"png", "jpeg"}:
        raise HTTPException(status_code=400, detail=f"不支持的格式: {output_format}")
    if resolution.upper() not in {"1K", "2K", "4K"}:
        raise HTTPException(status_code=400, detail=f"不支持的分辨率: {resolution}")

    cost = MODEL_PRICES[model]
    if current_user["balance"] < cost:
        raise HTTPException(status_code=402, detail=f"余额不足，需要 ¥{cost}")

    os.makedirs(TEMP_DIR, exist_ok=True)

    ref_url = ""
    if reference_image and reference_image.filename:
        ref_url = await save_upload_to_url(reference_image, TEMP_DIR)

    task_id = db.create_task(
        user_id=current_user["id"],
        model=model,
        prompt_text=prompt,
        cost=cost,
        aspect_ratio=aspect_ratio,
        output_format=output_format.lower(),
        resolution=resolution.upper(),
        reference_image_url=ref_url,
        api_key_id=current_user.get("key_id"),
    )

    ok = db.deduct_balance(current_user["id"], cost, task_id, f"生成任务 {model}")
    if not ok:
        db.fail_task(task_id, "余额不足", refund=False)
        raise HTTPException(status_code=402, detail="余额不足")

    row = db.get_task(task_id)
    return _task_to_response(row)


# ── 查询任务状态 ──────────────────────────────────────────────
@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, current_user: dict = Depends(get_current_user)):
    row = db.get_task(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["user_id"] != current_user["id"] and not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="无权限")
    return _task_to_response(row)


# ── 任务列表 ──────────────────────────────────────────────────
@router.get("/tasks")
def list_tasks(
    limit: int = 20,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
):
    rows = db.get_user_tasks(current_user["id"], limit=min(limit, 100), offset=offset)
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    return [
        {
            "task_id": r["task_id"],
            "model": r["model"],
            "prompt": r.get("prompt_text"),
            "aspect_ratio": r.get("aspect_ratio"),
            "output_format": r.get("output_format"),
            "resolution": r.get("resolution"),
            "status": r["status"],
            "cost": float(r["cost"]) if r["cost"] else None,
            "result_image_url": r.get("result_image_url"),
            "error_msg": r.get("error_msg"),
            "created_at": _fmt(r["created_at"]),
        }
        for r in rows
    ]
