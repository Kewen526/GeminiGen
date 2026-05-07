# -*- coding: utf-8 -*-
from fastapi import APIRouter, Depends, HTTPException

from ..models import BalanceResponse, AdminRechargeRequest, TransactionResponse
from ..auth import get_current_user, require_admin
from ..config import POINTS_PER_YUAN
from .. import database as db

router = APIRouter(prefix="/v1", tags=["余额"])


@router.get("/balance", response_model=BalanceResponse)
def get_balance(current_user: dict = Depends(get_current_user)):
    user = db.get_user_by_id(current_user["id"])
    balance = float(user["balance"])
    return BalanceResponse(balance=balance, points=int(balance * POINTS_PER_YUAN))


@router.get("/transactions")
def list_transactions(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    rows = db.get_transactions(current_user["id"], limit=min(limit, 200))
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    return [
        {
            "id": r["id"],
            "amount": float(r["amount"]),
            "type": r["type"],
            "task_id": r.get("task_id"),
            "note": r.get("note"),
            "balance_after": float(r["balance_after"]) if r.get("balance_after") is not None else None,
            "created_at": _fmt(r["created_at"]),
        }
        for r in rows
    ]


# ── 管理员充值 ────────────────────────────────────────────────
@router.post("/admin/recharge")
def admin_recharge(
    req: AdminRechargeRequest,
    _admin: dict = Depends(require_admin),
):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="充值金额必须大于 0")
    user = db.get_user_by_id(req.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    new_bal = db.add_balance(
        req.user_id, req.amount,
        tx_type="recharge", note=req.note or "管理员充值"
    )
    return {"user_id": req.user_id, "amount": req.amount, "balance_after": new_bal}


# ── 管理员按邮箱查用户 ────────────────────────────────────────
@router.get("/admin/user-by-email")
def admin_get_user_by_email(
    email: str,
    _admin: dict = Depends(require_admin),
):
    user = db.get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user.get("username"),
        "balance": float(user["balance"]),
        "total_tasks": user.get("total_tasks", 0),
    }


# ── 管理员查用户流水 ──────────────────────────────────────────
@router.get("/admin/transactions")
def admin_get_transactions(
    user_id: int,
    limit: int = 10,
    _admin: dict = Depends(require_admin),
):
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    rows = db.get_transactions(user_id, limit=min(limit, 50))
    return [
        {
            "id": r["id"],
            "amount": float(r["amount"]),
            "type": r["type"],
            "note": r.get("note"),
            "balance_after": float(r["balance_after"]) if r.get("balance_after") is not None else None,
            "created_at": _fmt(r["created_at"]),
        }
        for r in rows
    ]


# ── 管理员查用户 API Keys ─────────────────────────────────────
@router.get("/admin/api-keys")
def admin_get_api_keys(
    user_id: int,
    _admin: dict = Depends(require_admin),
):
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt) if dt else None
    rows = db.get_api_keys(user_id)
    return [
        {
            "id": r["id"],
            "key_name": r["key_name"],
            "key_value": r["key_value"],
            "total_calls": r["total_calls"],
            "last_used_at": _fmt(r["last_used_at"]),
            "created_at": _fmt(r["created_at"]),
        }
        for r in rows
    ]


# ── 管理员创建 API Key ────────────────────────────────────────
@router.post("/admin/create-api-key")
def admin_create_api_key(
    user_id: int,
    key_name: str = "管理员创建",
    _admin: dict = Depends(require_admin),
):
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    result = db.admin_create_api_key(user_id, key_name)
    return {"user_id": user_id, "key_name": result["key_name"], "key_value": result["key_value"]}


# ── 管理员统计概览 ────────────────────────────────────────────
@router.get("/admin/stats/overview")
def admin_stats_overview(_admin: dict = Depends(require_admin)):
    return db.admin_stats_overview()


# ── 管理员用户消耗列表 ────────────────────────────────────────
@router.get("/admin/stats/users")
def admin_stats_users(
    limit: int = 50,
    _admin: dict = Depends(require_admin),
):
    def _fmt(dt) -> str:
        return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)
    rows = db.admin_user_stats(limit=min(limit, 200))
    return [
        {
            "id": r["id"],
            "email": r["email"],
            "username": r.get("username"),
            "balance": float(r["balance"]),
            "total_spend": float(r["total_spend"]),
            "total_recharge": float(r["total_recharge"]),
            "api_tasks": int(r["api_tasks"]),
            "web_tasks": int(r["web_tasks"]),
            "created_at": _fmt(r["created_at"]),
        }
        for r in rows
    ]
