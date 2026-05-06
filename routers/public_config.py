"""Authenticated users: Gemini model lists, Google Maps JS key (from DB)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException

from dependencies.auth import get_current_user
from models import User
from services.gemini_catalog import list_public_gemini_models_sync
from services.provider_keys import get_gmaps_api_key_sync
from services.user_gemini_policy import resolve_model_for_user

router = APIRouter(prefix="/api/config", tags=["config"])


@router.get("/gemini-models")
async def public_gemini_models(
    channel: str,
    user: User = Depends(get_current_user),
):
    if channel not in ("rest", "live"):
        raise HTTPException(status_code=400, detail="channel must be rest|live")
    rows = await asyncio.to_thread(list_public_gemini_models_sync, channel)
    if not rows:
        raise HTTPException(
            status_code=503,
            detail="لا توجد موديلات مفعّلة — أضف موديلات من لوحة الأدمن.",
        )
    if not user.is_admin:
        try:
            allowed_mid = await asyncio.to_thread(resolve_model_for_user, None, user, channel, "")
        except Exception:
            raise HTTPException(
                status_code=403,
                detail="لم يتم تعيين موديل لهذا المستخدم — راجع الأدمن.",
            )
        rows = [r for r in rows if str(r.get("model_id") or "").strip() == allowed_mid]
        if not rows:
            raise HTTPException(
                status_code=403,
                detail="الموديل المخصص للمستخدم غير مفعّل حالياً في الكتالوج.",
            )
    return {"channel": channel, "models": rows}


@router.get("/maps-js-key")
async def public_maps_js_key(_user: User = Depends(get_current_user)):
    key = await asyncio.to_thread(get_gmaps_api_key_sync)
    if not key:
        raise HTTPException(
            status_code=503,
            detail="خدمة الخرائط غير متاحة — أضف مفاتيح Maps في Redis من لوحة الأدمن (REDIS_URL).",
        )
    return {"key": key}
