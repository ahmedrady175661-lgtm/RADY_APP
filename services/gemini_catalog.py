"""Gemini model catalog helpers (admin DB). Not for API key storage."""

from __future__ import annotations

from sqlalchemy.orm import Session

from db import SessionLocal
from models.provider_config import GeminiModelCatalog, GeminiServerDefaultModel


def is_gemini_model_allowed_sync(channel: str, model_id: str) -> bool:
    mid = (model_id or "").strip()
    if not mid or channel not in ("rest", "live"):
        return False
    with SessionLocal() as db:
        return (
            db.query(GeminiModelCatalog)
            .filter(
                GeminiModelCatalog.channel == channel,
                GeminiModelCatalog.enabled.is_(True),
                GeminiModelCatalog.model_id == mid,
            )
            .first()
            is not None
        )


def list_gemini_models_sync(db: Session, channel: str | None) -> list[dict]:
    q = db.query(GeminiModelCatalog)
    if channel in ("rest", "live"):
        q = q.filter(GeminiModelCatalog.channel == channel)
    rows = q.order_by(
        GeminiModelCatalog.channel.asc(),
        GeminiModelCatalog.sort_order.asc(),
        GeminiModelCatalog.id.asc(),
    ).all()
    return [
        {
            "id": r.id,
            "channel": r.channel,
            "model_id": r.model_id,
            "label": r.label,
            "enabled": bool(r.enabled),
            "sort_order": r.sort_order,
        }
        for r in rows
    ]


def list_public_gemini_models_sync(channel: str) -> list[dict]:
    with SessionLocal() as db:
        rows = (
            db.query(GeminiModelCatalog)
            .filter(
                GeminiModelCatalog.channel == channel,
                GeminiModelCatalog.enabled.is_(True),
            )
            .order_by(GeminiModelCatalog.sort_order.asc(), GeminiModelCatalog.id.asc())
            .all()
        )
        return [{"id": r.id, "model_id": r.model_id, "label": r.label or r.model_id} for r in rows]


def get_server_default_model_sync(channel: str) -> str:
    ch = (channel or "").strip().lower()
    if ch not in ("rest", "live"):
        return ""
    with SessionLocal() as db:
        row = db.query(GeminiServerDefaultModel).filter(GeminiServerDefaultModel.channel == ch).first()
        return (row.model_id or "").strip() if row else ""


def set_server_default_model_sync(db: Session, *, channel: str, model_id: str) -> dict:
    ch = (channel or "").strip().lower()
    mid = (model_id or "").strip()
    if ch not in ("rest", "live"):
        raise ValueError("invalid_channel")
    if not mid:
        raise ValueError("model_required")
    allowed = (
        db.query(GeminiModelCatalog)
        .filter(
            GeminiModelCatalog.channel == ch,
            GeminiModelCatalog.enabled.is_(True),
            GeminiModelCatalog.model_id == mid,
        )
        .first()
    )
    if allowed is None:
        raise ValueError("model_not_allowed")
    row = db.query(GeminiServerDefaultModel).filter(GeminiServerDefaultModel.channel == ch).first()
    if row is None:
        row = GeminiServerDefaultModel(channel=ch, model_id=mid)
        db.add(row)
    else:
        row.model_id = mid
        db.add(row)
    db.commit()
    db.refresh(row)
    return {"channel": row.channel, "model_id": row.model_id}
