# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException

from ..models import ApiKeyCreate, ApiKeyResponse
from ..auth import get_current_user
from .. import database as db

router = APIRouter(prefix="/v1/api-keys", tags=["API Key"])


@router.get("", response_model=list[ApiKeyResponse])
def list_keys(current_user: dict = Depends(get_current_user)):
    rows = db.get_api_keys(current_user["id"])
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt) if dt else ""
    return [
        ApiKeyResponse(
            id=r["id"],
            key_name=r["key_name"],
            key_value=r["key_value"],
            total_calls=r["total_calls"],
            last_used_at=_fmt(r["last_used_at"]) if r["last_used_at"] else None,
            created_at=_fmt(r["created_at"]),
        )
        for r in rows
    ]


@router.post("", response_model=ApiKeyResponse)
def create_key(req: ApiKeyCreate, current_user: dict = Depends(get_current_user)):
    existing = db.get_api_keys(current_user["id"])
    if len(existing) >= 10:
        raise HTTPException(status_code=400, detail="最多创建 10 个 API Key")
    result = db.create_api_key(current_user["id"], req.key_name)
    return ApiKeyResponse(
        id=result["id"],
        key_name=result["key_name"],
        key_value=result["key_value"],
        total_calls=0,
        last_used_at=None,
        created_at="",
    )


@router.delete("/{key_id}")
def delete_key(key_id: int, current_user: dict = Depends(get_current_user)):
    ok = db.deactivate_api_key(key_id, current_user["id"])
    if not ok:
        raise HTTPException(status_code=404, detail="Key 不存在")
    return {"detail": "已删除"}
