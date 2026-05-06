"""Per-user Gemini policy: assigned model resolution and spend-limit enforcement."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from models import User
from services.gemini_catalog import get_server_default_model_sync, list_public_gemini_models_sync
from services.gemini_usage import sum_gemini_cost_usd_for_user_cycle_channel


class GeminiPolicyError(ValueError):
    """Policy violation with a user-facing Arabic message."""


@dataclass(frozen=True)
class GeminiBudgetSnapshot:
    used_usd: float
    limit_usd: float | None
    enabled: bool
    blocked: bool


def resolve_model_for_user(
    db: Session | None, user: User, channel: str, requested_model: str = ""
) -> str:
    """
    Resolve effective model_id for a user/channel.

    Priority:
    1) user-assigned model (admin-managed)
    2) server default model for that channel
    3) first enabled catalog model for that channel (fallback)
    """
    ch = (channel or "").strip().lower()
    if ch not in ("rest", "live"):
        raise GeminiPolicyError("قناة الموديل غير صحيحة.")
    req = (requested_model or "").strip()
    assigned = (
        (user.gemini_rest_model_id if ch == "rest" else user.gemini_live_model_id) or ""
    ).strip()
    if assigned:
        if req and req != assigned and not user.is_admin:
            raise GeminiPolicyError("لا يمكنك تغيير الموديل — يتم تحديده من لوحة الأدمن.")
        return assigned
    rows = list_public_gemini_models_sync(ch)
    if not rows:
        raise GeminiPolicyError("لا توجد موديلات مفعّلة لهذه القناة من لوحة الأدمن.")
    default_mid = get_server_default_model_sync(ch)
    if default_mid:
        allowed = {
            str(r.get("model_id") or "").strip()
            for r in rows
            if str(r.get("model_id") or "").strip()
        }
        if default_mid in allowed:
            return default_mid
    # Backward compatibility only: if server default not set yet, allow explicit request
    # from admin UI when present in enabled catalog.
    if req:
        allowed = {str(r.get("model_id") or "").strip() for r in rows if str(r.get("model_id") or "").strip()}
        if req in allowed:
            return req
    return str(rows[0].get("model_id") or "").strip()


def user_budget_snapshot(db: Session, user: User) -> GeminiBudgetSnapshot:
    if user.is_admin:
        return GeminiBudgetSnapshot(used_usd=0.0, limit_usd=None, enabled=False, blocked=False)
    started = user.subscription_cycle_started_at
    if started is None:
        return GeminiBudgetSnapshot(used_usd=0.0, limit_usd=None, enabled=False, blocked=False)
    rest = sum_gemini_cost_usd_for_user_cycle_channel(db, user, started, "rest")
    live = sum_gemini_cost_usd_for_user_cycle_channel(db, user, started, "live")
    used = float(rest + live)
    enabled = bool(getattr(user, "gemini_spend_limit_enabled", False))
    raw_limit = getattr(user, "gemini_spend_limit_usd", None)
    limit = float(raw_limit) if raw_limit is not None else None
    blocked = bool(enabled and limit is not None and used >= limit)
    return GeminiBudgetSnapshot(used_usd=used, limit_usd=limit, enabled=enabled, blocked=blocked)


def ensure_user_can_start_request(db: Session, user: User) -> GeminiBudgetSnapshot:
    snap = user_budget_snapshot(db, user)
    if snap.blocked:
        raise GeminiPolicyError(
            "تم الوصول إلى حد استخدام Gemini بالدولار لهذا العميل. "
            "يرجى التواصل مع الأدمن لزيادة الحد."
        )
    return snap

