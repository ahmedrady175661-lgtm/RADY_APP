"""Per-user Gemini usage events and optional model pricing (chargeback)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db import Base


class GeminiModelPricing(Base):
    """Admin-managed USD per 1M tokens (input/output) by model_id and channel."""

    __tablename__ = "gemini_model_pricing"
    __table_args__ = (
        Index("ix_gemini_pricing_model_channel_from", "model_id", "channel", "effective_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(8), nullable=False)  # rest | live
    effective_from: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    input_usd_per_1m: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False, default=0)
    output_usd_per_1m: Mapped[float] = mapped_column(Numeric(14, 6), nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class GeminiUsageEvent(Base):
    """One row per billed unit (REST job or Live session flush)."""

    __tablename__ = "gemini_usage_events"
    __table_args__ = (
        Index("ix_gemini_usage_user_created", "user_id", "created_at"),
        Index("ix_gemini_usage_key_created", "redis_key_id", "created_at"),
        Index("ix_gemini_usage_channel_created", "channel", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    # Start of the 30-day Gemini billing bucket this row belongs to (usually equals
    # user.subscription_cycle_started_at; after early admin renew, tail events use next cycle start).
    billing_cycle_start_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(8), nullable=False)  # rest | live
    redis_key_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    model_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    correlation_id: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(14, 6), nullable=True)
    confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, default="api"
    )  # api | estimated
    request_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="generate_content"
    )
    audio_seconds: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    live_session_seconds: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    extra_json: Mapped[str | None] = mapped_column(Text, nullable=True)
