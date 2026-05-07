from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, joinedload

from config import settings
from db import get_db
from dependencies.auth import get_current_user, require_device_header
from models import User
from schemas.auth import AuthSessionOut, LoginRequest, MeOut
from services.auth_cookies import clear_auth_cookies, set_auth_cookies
from services.auth_service import (
    AuthServiceError,
    revoke_user_device_tokens,
)
from services.auth_service import (
    login as auth_login,
)
from services.auth_service import (
    refresh as auth_refresh,
)
from services.gemini_usage import sum_gemini_cost_usd_for_user_cycle_channel
from services.check_postgres import (
    get_database_physical_size_bytes_sync,
    count_rows_for_user_ids_sync,
    count_storage_bytes_for_user_ids_sync,
)
from services.rate_limit import limiter
from services.subscription import (
    cycle_end_utc,
    in_grace_period,
    subscription_days_remaining,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/me", response_model=MeOut)
async def me(
    current: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = db.query(User).options(joinedload(User.group)).filter(User.id == current.id).first()
    if not u:
        raise HTTPException(status_code=401, detail="User not found")
    days_rem: int | None = None
    started_at = None
    ends_at = None
    grace = False
    r_cost = 0.0
    l_cost = 0.0
    used_rows = 0
    used_bytes = 0
    server_phys_bytes = 0
    if not u.is_admin and u.subscription_cycle_started_at is not None:
        started = u.subscription_cycle_started_at
        started_at = started
        days_rem = subscription_days_remaining(started)
        ends_at = cycle_end_utc(started)
        grace = bool(in_grace_period(started))
        r_cost = sum_gemini_cost_usd_for_user_cycle_channel(db, u, started, "rest")
        l_cost = sum_gemini_cost_usd_for_user_cycle_channel(db, u, started, "live")
    dsn = (settings.check_postgres_dsn or "").strip()
    if dsn:
        try:
            used_rows = count_rows_for_user_ids_sync(
                dsn, int(u.id), bool(u.is_admin), [int(u.id)]
            )
            used_bytes = count_storage_bytes_for_user_ids_sync(
                dsn, int(u.id), bool(u.is_admin), [int(u.id)]
            )
            server_phys_bytes = get_database_physical_size_bytes_sync(
                dsn, int(u.id), bool(u.is_admin)
            )
        except Exception:
            used_rows = 0
            used_bytes = 0
            server_phys_bytes = 0
    return MeOut(
        username=u.username,
        is_admin=u.is_admin,
        group_id=u.group_id,
        group_name=u.group.name if u.group else None,
        subscription_days_remaining=days_rem,
        subscription_cycle_started_at=started_at,
        subscription_cycle_ends_at=ends_at,
        in_grace_period=grace,
        gemini_rest_cost_usd=float(r_cost),
        gemini_live_cost_usd=float(l_cost),
        gemini_total_cost_usd=float(r_cost + l_cost),
        used_stored_large_rows=int(used_rows),
        used_stored_large_bytes=int(used_bytes),
        postgres_server_physical_bytes=int(server_phys_bytes),
        postgres_server_physical_mb=round(float(server_phys_bytes) / (1024 * 1024), 3),
    )


@router.post("/login", response_model=AuthSessionOut)
# SECURITY FIX: rate limited to prevent brute-force
@limiter.limit("5/minute")
async def login(
    request: Request,
    payload: LoginRequest,
    x_device_id: str = Depends(require_device_header),
    db: Session = Depends(get_db),
):
    try:
        access_token, refresh_token, is_admin = auth_login(
            db=db,
            username=payload.username,
            password=payload.password,
            device_id=x_device_id,
        )
    except AuthServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    body = AuthSessionOut(is_admin=is_admin).model_dump()
    resp = JSONResponse(content=body)
    set_auth_cookies(resp, access_token, refresh_token)
    return resp


@router.post("/refresh", response_model=AuthSessionOut)
# SECURITY FIX: rate limited to prevent brute-force
@limiter.limit("10/minute")
async def refresh_token(
    request: Request,
    x_device_id: str = Depends(require_device_header),
    db: Session = Depends(get_db),
):
    rt = ""
    try:
        body = await request.json()
        if isinstance(body, dict) and body.get("refresh_token"):
            rt = str(body["refresh_token"]).strip()
    except Exception:
        pass
    if not rt:
        rt = (request.cookies.get(settings.auth_cookie_refresh_name) or "").strip()
    if not rt:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    try:
        access_token, refresh_token, is_admin = auth_refresh(
            db=db,
            refresh_token=rt,
            device_id=x_device_id,
        )
    except AuthServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)

    body = AuthSessionOut(is_admin=is_admin).model_dump()
    resp = JSONResponse(content=body)
    set_auth_cookies(resp, access_token, refresh_token)
    return resp


@router.post("/logout")
async def logout(
    current: User = Depends(get_current_user),
    x_device_id: str = Depends(require_device_header),
    db: Session = Depends(get_db),
):
    revoke_user_device_tokens(db=db, user_id=current.id, device_id=x_device_id)
    resp = JSONResponse(content={"detail": "Logged out successfully"})
    clear_auth_cookies(resp)
    return resp
