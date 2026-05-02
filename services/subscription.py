"""30-day subscription window (last 3 days are grace, same access). Admins exempt."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from models import RefreshToken, User

SUBSCRIPTION_CYCLE_DAYS = 30
SUBSCRIPTION_GRACE_DAYS = 3


def cycle_end_utc(started: datetime) -> datetime:
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return started + timedelta(days=SUBSCRIPTION_CYCLE_DAYS)


def subscription_days_remaining(started: datetime, now: datetime | None = None) -> int:
    """Days until cycle end (ceil partial day); 0 after expiry."""
    now = now or datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    end = cycle_end_utc(started)
    secs = (end - now).total_seconds()
    if secs <= 0:
        return 0
    return max(1, math.ceil(secs / 86400.0))


def gemini_billing_cycle_start_for_event(user: User, event_time: datetime) -> datetime | None:
    """UTC anchor for which 30-day Gemini bucket counts this usage (None for admins / no cycle)."""
    if getattr(user, "is_admin", False):
        return None
    t0 = user.subscription_cycle_started_at
    if t0 is None:
        return None
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    t1 = cycle_end_utc(t0)
    er = getattr(user, "subscription_early_renew_at", None)
    if er is not None:
        if er.tzinfo is None:
            er = er.replace(tzinfo=timezone.utc)
        if er <= event_time < t1:
            return t1
    return t0


def in_grace_period(started: datetime, now: datetime | None = None) -> bool:
    """True during the last SUBSCRIPTION_GRACE_DAYS of the 30-day window."""
    now = now or datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    end = cycle_end_utc(started)
    grace_start = end - timedelta(days=SUBSCRIPTION_GRACE_DAYS)
    return grace_start <= now < end


def promote_subscription_after_early_renew(db: Session, user: User) -> bool:
    """
    If admin renewed early and the natural cycle end has passed, advance
    subscription_cycle_started_at to the next window start without deactivating.
    """
    if user.is_admin or user.subscription_cycle_started_at is None:
        return False
    er = user.subscription_early_renew_at
    if er is None:
        return False
    started = user.subscription_cycle_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    end = cycle_end_utc(started)
    now = datetime.now(timezone.utc)
    if now < end:
        return False
    user.subscription_cycle_started_at = end
    user.subscription_early_renew_at = None
    db.add(user)
    db.commit()
    db.refresh(user)
    return True


def deactivate_expired_subscription(db: Session, user: User) -> bool:
    """
    If the 30-day window ended, set is_active=False and revoke refresh tokens.
    If the user had renewed early, promote the cycle instead (no deactivation).
    Returns True if the user was deactivated in this call.
    """
    promote_subscription_after_early_renew(db, user)
    db.refresh(user)
    if user.is_admin:
        return False
    started = user.subscription_cycle_started_at
    if started is None:
        return False
    if datetime.now(timezone.utc) < cycle_end_utc(started):
        return False
    if not user.is_active:
        return False
    user.is_active = False
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user.id).values(is_revoked=True))
    db.add(user)
    db.commit()
    db.refresh(user)
    return True
