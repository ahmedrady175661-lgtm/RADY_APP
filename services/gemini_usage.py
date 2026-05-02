"""Record Gemini usage/cost events and resolve model pricing from SQL."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, desc, func, or_
from sqlalchemy.orm import Session

from models import User
from models.gemini_usage import GeminiModelPricing, GeminiUsageEvent
from services.subscription import cycle_end_utc, gemini_billing_cycle_start_for_event

logger = logging.getLogger(__name__)


def gemini_cycle_base_filter(user: User, started: datetime):
    """Same billing window as admin panel (excludes tail rolled to next cycle)."""
    end = cycle_end_utc(started)
    er = getattr(user, "subscription_early_renew_at", None)
    anchored = GeminiUsageEvent.billing_cycle_start_at == started
    legacy = and_(
        GeminiUsageEvent.billing_cycle_start_at.is_(None),
        GeminiUsageEvent.created_at >= started,
        GeminiUsageEvent.created_at < end,
    )
    if er is not None:
        legacy = and_(legacy, GeminiUsageEvent.created_at < er)
    return or_(anchored, legacy)


def gemini_carryover_filter(user: User, started: datetime):
    """Tail usage after early admin renew, attributed to the next billing cycle."""
    end = cycle_end_utc(started)
    er = getattr(user, "subscription_early_renew_at", None)
    if er is None:
        return GeminiUsageEvent.user_id == -1
    nxt = end
    return and_(
        GeminiUsageEvent.user_id == int(user.id),
        GeminiUsageEvent.billing_cycle_start_at == nxt,
        GeminiUsageEvent.created_at >= er,
        GeminiUsageEvent.created_at < end,
    )


def sum_gemini_cost_usd_for_user_cycle_channel(
    db: Session, user: User, started: datetime | None, channel: str
) -> float:
    """Sum cost_usd for one channel in the user's current subscription Gemini window."""
    if started is None or getattr(user, "is_admin", False):
        return 0.0
    ch = (channel or "").strip().lower()
    if ch not in ("rest", "live"):
        return 0.0
    filt = (gemini_cycle_base_filter(user, started), GeminiUsageEvent.channel == ch)
    row = db.query(func.coalesce(func.sum(GeminiUsageEvent.cost_usd), 0)).filter(*filt).one()
    raw = row[0]
    return float(raw) if not isinstance(raw, Decimal) else float(raw)


def _pick_pricing_row(
    db: Session, *, model_id: str, channel: str, at: datetime | None = None
) -> GeminiModelPricing | None:
    mid = (model_id or "").strip()
    ch = (channel or "").strip().lower()
    if not mid or ch not in ("rest", "live"):
        return None
    when = at or datetime.utcnow()
    return (
        db.query(GeminiModelPricing)
        .filter(
            GeminiModelPricing.model_id == mid,
            GeminiModelPricing.channel == ch,
            GeminiModelPricing.effective_from <= when,
        )
        .order_by(desc(GeminiModelPricing.effective_from))
        .first()
    )


def compute_cost_usd(
    db: Session,
    *,
    model_id: str,
    channel: str,
    input_tokens: int | None,
    output_tokens: int | None,
    at: datetime | None = None,
) -> Decimal | None:
    """Return cost in USD from pricing table, or None if no row or no tokens."""
    row = _pick_pricing_row(db, model_id=model_id, channel=channel, at=at)
    if row is None:
        return None
    inp = int(input_tokens or 0)
    out = int(output_tokens or 0)
    if inp == 0 and out == 0:
        return None
    pin = Decimal(str(row.input_usd_per_1m or 0))
    pout = Decimal(str(row.output_usd_per_1m or 0))
    return (Decimal(inp) / Decimal(1_000_000)) * pin + (Decimal(out) / Decimal(1_000_000)) * pout


def record_gemini_usage_event(
    db: Session,
    *,
    user_id: int | None,
    channel: str,
    redis_key_id: str,
    model_id: str,
    correlation_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    confidence: str,
    request_kind: str,
    audio_seconds: float | None = None,
    live_session_seconds: float | None = None,
    extra: dict[str, Any] | None = None,
) -> GeminiUsageEvent | None:
    """Insert one usage row; commit is caller's responsibility."""
    ch = (channel or "").strip().lower()
    if ch not in ("rest", "live"):
        logger.warning("gemini_usage: invalid channel %r", channel)
        return None
    kid = (redis_key_id or "").strip()
    if not kid:
        logger.warning("gemini_usage: missing redis_key_id")
        return None
    cost: Decimal | None = None
    try:
        cost = compute_cost_usd(
            db,
            model_id=model_id,
            channel=ch,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception:
        logger.exception("gemini_usage: compute_cost failed")
    anchor: datetime | None = None
    if user_id:
        u = db.get(User, int(user_id))
        if u is not None:
            anchor = gemini_billing_cycle_start_for_event(u, datetime.now(timezone.utc))
    ev = GeminiUsageEvent(
        user_id=user_id,
        channel=ch,
        redis_key_id=kid,
        model_id=(model_id or "").strip()[:200],
        correlation_id=(correlation_id or "").strip()[:200],
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=float(cost) if cost is not None else None,
        confidence=(confidence or "api")[:16],
        request_kind=(request_kind or "")[:32],
        audio_seconds=audio_seconds,
        live_session_seconds=live_session_seconds,
        extra_json=json.dumps(extra, ensure_ascii=False)[:8000] if extra else None,
        billing_cycle_start_at=anchor,
    )
    db.add(ev)
    return ev


def record_gemini_usage_event_sync(
    *,
    user_id: int | None,
    channel: str,
    redis_key_id: str,
    model_id: str,
    correlation_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    confidence: str,
    request_kind: str,
    audio_seconds: float | None = None,
    live_session_seconds: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Open a short-lived Session, insert, commit."""
    from db import SessionLocal

    db = SessionLocal()
    try:
        record_gemini_usage_event(
            db,
            user_id=user_id,
            channel=channel,
            redis_key_id=redis_key_id,
            model_id=model_id,
            correlation_id=correlation_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            confidence=confidence,
            request_kind=request_kind,
            audio_seconds=audio_seconds,
            live_session_seconds=live_session_seconds,
            extra=extra,
        )
        db.commit()
    except Exception:
        logger.exception("gemini_usage: failed to record event")
        db.rollback()
    finally:
        db.close()


def flush_live_gemini_usage_for_session_sync(session: Any, correlation_id: str) -> None:
    """Persist one Live row from SessionState metering fields."""
    uid = int(getattr(session, "user_id", 0) or 0)
    kid = (getattr(session, "gemini_redis_key_id", "") or "").strip()
    if not uid or not kid:
        return
    model_id = (getattr(session, "gemini_live_model", "") or "").strip()
    pin = int(getattr(session, "live_usage_prompt_tokens", 0) or 0)
    pout = int(getattr(session, "live_usage_output_tokens", 0) or 0)
    ptot = int(getattr(session, "live_usage_total_tokens", 0) or 0)
    started = getattr(session, "live_connected_at", None)
    duration_sec: float | None = None
    if started is not None:
        try:
            duration_sec = (datetime.now(timezone.utc) - started).total_seconds()
        except Exception:
            duration_sec = None
    b64chars = int(getattr(session, "live_audio_b64_chars", 0) or 0)
    # Approximate PCM16 mono 16kHz seconds from base64 audio payload volume.
    approx_audio_sec = (b64chars * 0.75) / (16000 * 2) if b64chars else None
    has_api = pin > 0 or pout > 0 or ptot > 0
    confidence = "api" if has_api else "estimated"
    inp_t = pin if has_api else None
    out_t = pout if has_api else None
    tot_t = ptot if has_api else None
    if not has_api and approx_audio_sec and approx_audio_sec > 0:
        # Rough token proxy for pricing when API omits usage (internal only).
        inp_t = max(1, int(approx_audio_sec * 500))
        out_t = max(1, int(approx_audio_sec * 120))
        tot_t = (inp_t or 0) + (out_t or 0)
    elif not has_api and duration_sec and duration_sec > 0:
        inp_t = max(1, int(duration_sec * 30))
        out_t = max(1, int(duration_sec * 10))
        tot_t = (inp_t or 0) + (out_t or 0)
    record_gemini_usage_event_sync(
        user_id=uid,
        channel="live",
        redis_key_id=kid,
        model_id=model_id or "unknown",
        correlation_id=correlation_id[:200],
        input_tokens=inp_t,
        output_tokens=out_t,
        total_tokens=tot_t,
        confidence=confidence,
        request_kind="live_bidi",
        audio_seconds=float(approx_audio_sec) if approx_audio_sec else None,
        live_session_seconds=float(duration_sec) if duration_sec is not None else None,
        extra={"correlation": correlation_id},
    )


def extract_usage_from_generate_content_response(response: Any) -> dict[str, int | None]:
    """Map google-genai GenerateContentResponse.usage_metadata to token ints.

    Verified against installed `google.genai.types.GenerateContentResponseUsageMetadata`
    (prompt_token_count, candidates_token_count, total_token_count).
    """
    out: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
    }
    try:
        um = getattr(response, "usage_metadata", None)
        if um is None:
            return out
        # GenerateContentResponseUsageMetadata: prompt_token_count, candidates_token_count, total_token_count
        pin = getattr(um, "prompt_token_count", None)
        cout = getattr(um, "candidates_token_count", None)
        tot = getattr(um, "total_token_count", None)
        out["input_tokens"] = int(pin) if pin is not None else None
        out["output_tokens"] = int(cout) if cout is not None else None
        out["total_tokens"] = int(tot) if tot is not None else None
    except Exception:
        logger.debug("extract_usage_from_generate_content_response: no usage", exc_info=True)
    return out
