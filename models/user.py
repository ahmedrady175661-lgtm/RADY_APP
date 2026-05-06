from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    # Optional label for admins (real name / who is this); not used for login.
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Start of current 30-day billing window (UTC). NULL = exempt (admins / legacy).
    subscription_cycle_started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    # Admin confirmed renewal before cycle end: app access until natural cycle_end;
    # Gemini usage from this time until cycle_end is billed on the next cycle (see GeminiUsageEvent).
    subscription_early_renew_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )
    device_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_groups.id", ondelete="SET NULL"), nullable=True
    )
    max_stored_large_rows: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Per-user Gemini model assignment (admin-managed). NULL => use app default catalog.
    gemini_rest_model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    gemini_live_model_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Per-user Gemini spend guard (USD) in the current subscription cycle.
    # Admins are exempt from budget enforcement.
    gemini_spend_limit_usd: Mapped[float | None] = mapped_column(Numeric(14, 6), nullable=True)
    gemini_spend_limit_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    group = relationship("UserGroup", back_populates="users")
