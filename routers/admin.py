from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from config import settings
from db import get_db
from dependencies.auth import require_admin
from models import RefreshToken, User, UserGroup
from models.gemini_usage import GeminiUsageEvent
from schemas.user import (
    AdminGroupDetailOut,
    AdminUserDetailOut,
    CreateGroupRequest,
    CreateUserRequest,
    GroupLargeRowsLimitUpdate,
    GroupMemberOut,
    GroupOut,
    UserActiveUpdate,
    UserGroupUpdate,
    UserGeminiPolicyUpdate,
    UserLargeRowsLimitUpdate,
    UserOut,
)
from services.auth_service import (
    AuthServiceError,
)
from services.auth_service import (
    create_user as auth_create_user,
)
from services.auth_service import (
    reset_user_device as auth_reset_user_device,
)
from services.auth_service import (
    set_user_active as auth_set_user_active,
)
from services.check_group_sync import (
    delete_group_mirrors_postgres,
    sync_user_group_membership_postgres,
)
from services.check_postgres import count_rows_for_user_ids_sync
from services.check_postgres import (
    count_rows_server_sync,
    count_storage_bytes_for_user_ids_sync,
    count_storage_bytes_server_sync,
)
from services.gemini_usage import (
    gemini_carryover_filter,
    gemini_cycle_base_filter,
)
from services.gemini_catalog import is_gemini_model_allowed_sync
from services.subscription import (
    cycle_end_utc,
    in_grace_period,
    subscription_days_remaining,
)

router = APIRouter(prefix="/admin", tags=["admin"])


def _check_pg_dsn() -> str | None:
    u = (settings.check_postgres_dsn or "").strip()
    return u or None


def _sync_pg_membership(user_id: int, group_id: int | None) -> None:
    dsn = _check_pg_dsn()
    if dsn:
        sync_user_group_membership_postgres(dsn, user_id, group_id)


def _user_out(db_user: User) -> UserOut:
    return UserOut(
        id=db_user.id,
        username=db_user.username,
        display_name=str(db_user.display_name or "").strip(),
        is_admin=db_user.is_admin,
        is_active=db_user.is_active,
        device_id=db_user.device_id,
        group_id=db_user.group_id,
        group_name=db_user.group.name if db_user.group else None,
        max_stored_large_rows=(
            int(db_user.max_stored_large_rows)
            if db_user.max_stored_large_rows is not None
            else None
        ),
        used_stored_large_rows=0,
        used_stored_large_bytes=0,
        gemini_rest_model_id=(db_user.gemini_rest_model_id or None),
        gemini_live_model_id=(db_user.gemini_live_model_id or None),
        gemini_spend_limit_enabled=bool(db_user.gemini_spend_limit_enabled),
        gemini_spend_limit_usd=(
            float(db_user.gemini_spend_limit_usd)
            if db_user.gemini_spend_limit_usd is not None
            else None
        ),
    )


def _gemini_cycle_channel_bundle(
    db: Session, *, user: User, started: datetime | None, channel: str
) -> tuple[float, int, int, str]:
    """Sum cost/tokens/events and list distinct model_id for one channel in the cycle."""
    if started is None:
        return 0.0, 0, 0, "—"
    ch = (channel or "").strip().lower()
    filt = (
        gemini_cycle_base_filter(user, started),
        GeminiUsageEvent.channel == ch,
    )
    row = (
        db.query(
            func.coalesce(func.sum(GeminiUsageEvent.cost_usd), 0),
            func.coalesce(func.sum(GeminiUsageEvent.total_tokens), 0),
            func.count(GeminiUsageEvent.id),
        )
        .filter(*filt)
        .one()
    )
    raw_cost = row[0]
    if isinstance(raw_cost, Decimal):
        cost = float(raw_cost)
    else:
        cost = float(raw_cost or 0)
    toks = int(row[1] or 0)
    evs = int(row[2] or 0)
    mids = db.query(GeminiUsageEvent.model_id).filter(*filt).distinct().all()
    mlist = sorted({str(m[0]).strip() for m in mids if m[0] and str(m[0]).strip()})
    models = ", ".join(mlist) if mlist else "—"
    return cost, toks, evs, models


def _admin_user_detail_out(db: Session, admin: User, db_user: User) -> AdminUserDetailOut:
    used_map = _user_used_rows_map(db, admin, [int(db_user.id)])
    used_bytes_map = _user_used_bytes_map(db, admin, [int(db_user.id)])
    started = db_user.subscription_cycle_started_at
    end_at = cycle_end_utc(started) if started and not db_user.is_admin else None
    days_rem = subscription_days_remaining(started) if started and not db_user.is_admin else 0
    grace = bool(started and not db_user.is_admin and in_grace_period(started))
    cost = 0.0
    toks = 0
    evs = 0
    r_cost = r_tok = r_ev = 0
    r_models = "—"
    l_cost = l_tok = l_ev = 0
    l_models = "—"
    c_cost = 0.0
    c_tok = 0
    c_evs = 0
    if started and not db_user.is_admin:
        row = (
            db.query(
                func.coalesce(func.sum(GeminiUsageEvent.cost_usd), 0),
                func.coalesce(func.sum(GeminiUsageEvent.total_tokens), 0),
                func.count(GeminiUsageEvent.id),
            )
            .filter(gemini_cycle_base_filter(db_user, started))
            .one()
        )
        raw_cost = row[0]
        if isinstance(raw_cost, Decimal):
            cost = float(raw_cost)
        else:
            cost = float(raw_cost or 0)
        toks = int(row[1] or 0)
        evs = int(row[2] or 0)
        r_cost, r_tok, r_ev, r_models = _gemini_cycle_channel_bundle(
            db, user=db_user, started=started, channel="rest"
        )
        l_cost, l_tok, l_ev, l_models = _gemini_cycle_channel_bundle(
            db, user=db_user, started=started, channel="live"
        )
        crow = (
            db.query(
                func.coalesce(func.sum(GeminiUsageEvent.cost_usd), 0),
                func.coalesce(func.sum(GeminiUsageEvent.total_tokens), 0),
                func.count(GeminiUsageEvent.id),
            )
            .filter(gemini_carryover_filter(db_user, started))
            .one()
        )
        rc = crow[0]
        if isinstance(rc, Decimal):
            c_cost = float(rc)
        else:
            c_cost = float(rc or 0)
        c_tok = int(crow[1] or 0)
        c_evs = int(crow[2] or 0)
    lim = (
        float(db_user.gemini_spend_limit_usd)
        if db_user.gemini_spend_limit_usd is not None
        else None
    )
    rem = None
    if bool(db_user.gemini_spend_limit_enabled) and lim is not None:
        rem = max(0.0, float(lim - cost))
    return AdminUserDetailOut(
        id=int(db_user.id),
        username=str(db_user.username),
        display_name=str(db_user.display_name or "").strip(),
        is_admin=bool(db_user.is_admin),
        is_active=bool(db_user.is_active),
        device_id=db_user.device_id,
        group_id=int(db_user.group_id) if db_user.group_id is not None else None,
        group_name=db_user.group.name if db_user.group else None,
        max_stored_large_rows=(
            int(db_user.max_stored_large_rows)
            if db_user.max_stored_large_rows is not None
            else None
        ),
        used_stored_large_rows=int(used_map.get(int(db_user.id), 0)),
        used_stored_large_bytes=int(used_bytes_map.get(int(db_user.id), 0)),
        subscription_cycle_started_at=started,
        subscription_cycle_ends_at=end_at,
        cycle_days_remaining=int(days_rem),
        in_grace_period=grace,
        subscription_early_renew_at=getattr(db_user, "subscription_early_renew_at", None),
        gemini_cycle_cost_usd=cost,
        gemini_cycle_tokens=toks,
        gemini_cycle_events=evs,
        gemini_carryover_cost_usd=c_cost,
        gemini_carryover_tokens=c_tok,
        gemini_carryover_events=c_evs,
        gemini_rest_cost_usd=r_cost,
        gemini_rest_tokens=r_tok,
        gemini_rest_events=r_ev,
        gemini_rest_models=r_models,
        gemini_live_cost_usd=l_cost,
        gemini_live_tokens=l_tok,
        gemini_live_events=l_ev,
        gemini_live_models=l_models,
        gemini_rest_model_id=(db_user.gemini_rest_model_id or None),
        gemini_live_model_id=(db_user.gemini_live_model_id or None),
        gemini_spend_limit_enabled=bool(db_user.gemini_spend_limit_enabled),
        gemini_spend_limit_usd=lim,
        gemini_spend_remaining_usd=rem,
    )


def _user_used_rows_map(db: Session, admin: User, user_ids: list[int]) -> dict[int, int]:
    dsn = _check_pg_dsn()
    if not dsn or not user_ids:
        return {int(uid): 0 for uid in user_ids}
    out: dict[int, int] = {}
    for uid in user_ids:
        try:
            out[int(uid)] = count_rows_for_user_ids_sync(
                dsn, int(admin.id), bool(admin.is_admin), [int(uid)]
            )
        except Exception:
            out[int(uid)] = 0
    return out


def _user_used_bytes_map(db: Session, admin: User, user_ids: list[int]) -> dict[int, int]:
    dsn = _check_pg_dsn()
    if not dsn or not user_ids:
        return {int(uid): 0 for uid in user_ids}
    out: dict[int, int] = {}
    for uid in user_ids:
        try:
            out[int(uid)] = count_storage_bytes_for_user_ids_sync(
                dsn, int(admin.id), bool(admin.is_admin), [int(uid)]
            )
        except Exception:
            out[int(uid)] = 0
    return out


def _group_used_rows_map(db: Session, admin: User, group_ids: list[int]) -> dict[int, int]:
    dsn = _check_pg_dsn()
    if not dsn or not group_ids:
        return {int(gid): 0 for gid in group_ids}
    out: dict[int, int] = {}
    for gid in group_ids:
        members = [int(u.id) for u in db.query(User).filter(User.group_id == gid).all()]
        if not members:
            out[int(gid)] = 0
            continue
        try:
            out[int(gid)] = count_rows_for_user_ids_sync(
                dsn, int(admin.id), bool(admin.is_admin), members
            )
        except Exception:
            out[int(gid)] = 0
    return out


def _ensure_group_quota_allows_membership(
    db: Session,
    *,
    member_user_id: int,
    target_group_id: int | None,
    requester_user_id: int,
    requester_is_admin: bool,
) -> None:
    if target_group_id is None:
        return
    g = db.get(UserGroup, target_group_id)
    if g is None:
        raise HTTPException(status_code=400, detail="المجموعة غير موجودة")
    limit = int(getattr(g, "max_stored_large_rows", 0) or 0)
    if limit <= 0:
        return
    dsn = _check_pg_dsn()
    if not dsn:
        return
    current_ids = [int(u.id) for u in db.query(User).filter(User.group_id == target_group_id).all()]
    if member_user_id not in current_ids:
        current_ids.append(int(member_user_id))
    try:
        total_rows = count_rows_for_user_ids_sync(
            dsn, requester_user_id, requester_is_admin, current_ids
        )
    except Exception:
        raise HTTPException(
            status_code=500, detail="تعذّر التحقق من رصيد صفوف المجموعة حالياً."
        ) from None
    if total_rows > limit:
        raise HTTPException(
            status_code=400,
            detail=(
                "لا يمكن إتمام العملية: بيانات المستخدم + بيانات المجموعة ستتجاوز حد المجموعة المسموح."
            ),
        )


@router.post("/users", response_model=UserOut)
async def create_user(
    payload: CreateUserRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ensure_group_quota_allows_membership(
        db,
        member_user_id=-1,  # new user has no stored rows yet
        target_group_id=payload.group_id,
        requester_user_id=_admin.id,
        requester_is_admin=bool(_admin.is_admin),
    )
    try:
        user = auth_create_user(
            db=db,
            username=payload.username,
            password=payload.password,
            display_name=(payload.display_name or "").strip()[:200],
            is_admin=payload.is_admin,
            group_id=payload.group_id,
            max_stored_large_rows=payload.max_stored_large_rows,
        )
    except AuthServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    u = db.query(User).options(joinedload(User.group)).filter(User.id == user.id).first()
    assert u is not None
    _sync_pg_membership(u.id, u.group_id)
    return _user_out(u)


@router.get("/users", response_model=list[UserOut])
async def list_users(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(User).options(joinedload(User.group)).order_by(User.id.asc()).all()
    used_map = _user_used_rows_map(db, admin, [int(u.id) for u in rows])
    used_bytes_map = _user_used_bytes_map(db, admin, [int(u.id) for u in rows])
    out: list[UserOut] = []
    for u in rows:
        item = _user_out(u)
        item.used_stored_large_rows = int(used_map.get(int(u.id), 0))
        item.used_stored_large_bytes = int(used_bytes_map.get(int(u.id), 0))
        out.append(item)
    return out


@router.get("/storage-summary")
async def admin_storage_summary(
    admin: User = Depends(require_admin),
):
    dsn = _check_pg_dsn()
    if not dsn:
        return {"server_rows": 0, "server_used_bytes": 0, "server_used_mb": 0.0}
    try:
        server_rows = count_rows_server_sync(dsn, int(admin.id), bool(admin.is_admin))
        # For server total bytes we intentionally read all rows (admin context).
        server_used_bytes = count_storage_bytes_server_sync(dsn, int(admin.id), bool(admin.is_admin))
        return {
            "server_rows": int(server_rows),
            "server_used_bytes": int(server_used_bytes),
            "server_used_mb": round(float(server_used_bytes) / (1024 * 1024), 3),
        }
    except Exception:
        return {"server_rows": 0, "server_used_bytes": 0, "server_used_mb": 0.0}


@router.get("/users/{user_id}", response_model=AdminUserDetailOut)
async def get_user_admin_detail(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    u = db.query(User).options(joinedload(User.group)).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    return _admin_user_detail_out(db, admin, u)


@router.post("/users/{user_id}/renew-subscription", response_model=AdminUserDetailOut)
async def renew_user_subscription(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin confirms payment.

    Before natural cycle end: keep subscription window until cycle end; roll Gemini tail
    into the next billing bucket. After cycle end: reset Gemini rows and start a new cycle from now.
    """
    u = db.query(User).options(joinedload(User.group)).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    if u.is_admin:
        raise HTTPException(status_code=400, detail="حسابات الأدمن لا تستخدم دورة الاشتراك")
    started = u.subscription_cycle_started_at
    if started is None:
        raise HTTPException(
            status_code=400,
            detail="لا توجد دورة اشتراك لهذا الحساب — لا يمكن التجديد",
        )
    now = datetime.now(timezone.utc)
    t1 = cycle_end_utc(started)

    if now < t1:
        if u.subscription_early_renew_at is None:
            u.subscription_early_renew_at = now
            db.add(u)
            db.flush()
        er = u.subscription_early_renew_at
        db.execute(
            update(GeminiUsageEvent)
            .where(
                GeminiUsageEvent.user_id == user_id,
                GeminiUsageEvent.created_at >= started,
                GeminiUsageEvent.created_at < er,
                GeminiUsageEvent.billing_cycle_start_at.is_(None),
            )
            .values(billing_cycle_start_at=started)
        )
        db.execute(
            update(GeminiUsageEvent)
            .where(
                GeminiUsageEvent.user_id == user_id,
                GeminiUsageEvent.created_at >= er,
                GeminiUsageEvent.created_at < t1,
                or_(
                    GeminiUsageEvent.billing_cycle_start_at.is_(None),
                    GeminiUsageEvent.billing_cycle_start_at == started,
                ),
            )
            .values(billing_cycle_start_at=t1)
        )
        u.is_active = True
        db.add(u)
        db.commit()
    else:
        u.subscription_early_renew_at = None
        db.query(GeminiUsageEvent).filter(GeminiUsageEvent.user_id == user_id).delete(
            synchronize_session=False
        )
        u.subscription_cycle_started_at = now
        u.is_active = True
        db.add(u)
        db.commit()
    db.refresh(u)
    u2 = db.query(User).options(joinedload(User.group)).filter(User.id == user_id).first()
    assert u2 is not None
    return _admin_user_detail_out(db, _admin, u2)


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(UserGroup).order_by(UserGroup.id.asc()).all()
    used_map = _group_used_rows_map(db, admin, [int(g.id) for g in rows])
    return [
        GroupOut(
            id=int(g.id),
            name=str(g.name),
            max_stored_large_rows=(
                int(g.max_stored_large_rows) if g.max_stored_large_rows is not None else None
            ),
            used_stored_large_rows=int(used_map.get(int(g.id), 0)),
        )
        for g in rows
    ]


def _admin_group_detail_out(db: Session, admin: User, g: UserGroup) -> AdminGroupDetailOut:
    used_map_g = _group_used_rows_map(db, admin, [int(g.id)])
    members = db.query(User).filter(User.group_id == int(g.id)).order_by(User.id.asc()).all()
    m_ids = [int(m.id) for m in members]
    used_map_u = _user_used_rows_map(db, admin, m_ids) if m_ids else {}
    mem_out: list[GroupMemberOut] = []
    for m in members:
        mem_out.append(
            GroupMemberOut(
                id=int(m.id),
                username=str(m.username),
                display_name=str(m.display_name or "").strip(),
                is_admin=bool(m.is_admin),
                is_active=bool(m.is_active),
                device_id=m.device_id,
                max_stored_large_rows=(
                    int(m.max_stored_large_rows) if m.max_stored_large_rows is not None else None
                ),
                used_stored_large_rows=int(used_map_u.get(int(m.id), 0)),
            )
        )
    return AdminGroupDetailOut(
        id=int(g.id),
        name=str(g.name),
        max_stored_large_rows=(
            int(g.max_stored_large_rows) if g.max_stored_large_rows is not None else None
        ),
        used_stored_large_rows=int(used_map_g.get(int(g.id), 0)),
        member_count=len(mem_out),
        members=mem_out,
    )


@router.get("/groups/{group_id}", response_model=AdminGroupDetailOut)
async def get_group_admin_detail(
    group_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    g = db.get(UserGroup, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
    return _admin_group_detail_out(db, admin, g)


@router.post("/groups", response_model=GroupOut)
async def create_group(
    payload: CreateGroupRequest,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="اسم المجموعة مطلوب")
    g = UserGroup(name=name, max_stored_large_rows=int(payload.max_stored_large_rows))
    db.add(g)
    try:
        db.commit()
        db.refresh(g)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="يوجد مجموعة بنفس الاسم مسبقاً") from None
    return g


@router.delete("/groups/{group_id}")
async def delete_group(
    group_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    g = db.get(UserGroup, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
    users = db.query(User).filter(User.group_id == group_id).all()
    for u in users:
        u.group_id = None
        db.add(u)
    db.delete(g)
    db.commit()
    dsn = _check_pg_dsn()
    if dsn:
        delete_group_mirrors_postgres(dsn, group_id)
    return {"deleted": True, "group_id": group_id}


@router.patch("/users/{user_id}/group", response_model=UserOut)
async def update_user_group(
    user_id: int,
    payload: UserGroupUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if payload.group_id is not None:
        _ensure_group_quota_allows_membership(
            db,
            member_user_id=user_id,
            target_group_id=payload.group_id,
            requester_user_id=_admin.id,
            requester_is_admin=bool(_admin.is_admin),
        )
    target = db.query(User).options(joinedload(User.group)).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    target.group_id = payload.group_id
    db.add(target)
    db.commit()
    db.refresh(target)
    _sync_pg_membership(target.id, target.group_id)
    u = db.query(User).options(joinedload(User.group)).filter(User.id == user_id).first()
    assert u is not None
    return _user_out(u)


@router.patch("/users/{user_id}/rows-limit", response_model=UserOut)
async def update_user_rows_limit(
    user_id: int,
    payload: UserLargeRowsLimitUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).options(joinedload(User.group)).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    target.max_stored_large_rows = int(payload.max_stored_large_rows)
    db.add(target)
    db.commit()
    db.refresh(target)
    return _user_out(target)


@router.patch("/users/{user_id}/gemini-policy", response_model=UserOut)
async def update_user_gemini_policy(
    user_id: int,
    payload: UserGeminiPolicyUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target = db.query(User).options(joinedload(User.group)).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")
    rest_mid = (payload.gemini_rest_model_id or "").strip()
    live_mid = (payload.gemini_live_model_id or "").strip()
    if rest_mid and not is_gemini_model_allowed_sync("rest", rest_mid):
        raise HTTPException(status_code=400, detail="موديل REST غير مسموح أو غير مفعّل.")
    if live_mid and not is_gemini_model_allowed_sync("live", live_mid):
        raise HTTPException(status_code=400, detail="موديل Live غير مسموح أو غير مفعّل.")
    target.gemini_rest_model_id = rest_mid or None
    target.gemini_live_model_id = live_mid or None
    target.gemini_spend_limit_enabled = bool(payload.gemini_spend_limit_enabled)
    if target.gemini_spend_limit_enabled:
        if payload.gemini_spend_limit_usd is None:
            raise HTTPException(status_code=400, detail="حدد قيمة حد الدولار عند التفعيل.")
        target.gemini_spend_limit_usd = float(payload.gemini_spend_limit_usd)
    else:
        target.gemini_spend_limit_usd = (
            float(payload.gemini_spend_limit_usd)
            if payload.gemini_spend_limit_usd is not None
            else target.gemini_spend_limit_usd
        )
    db.add(target)
    db.commit()
    db.refresh(target)
    return _user_out(target)


@router.patch("/groups/{group_id}/rows-limit", response_model=GroupOut)
async def update_group_rows_limit(
    group_id: int,
    payload: GroupLargeRowsLimitUpdate,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    g = db.get(UserGroup, group_id)
    if not g:
        raise HTTPException(status_code=404, detail="المجموعة غير موجودة")
    new_limit = int(payload.max_stored_large_rows)
    dsn = _check_pg_dsn()
    if dsn:
        member_ids = [int(u.id) for u in db.query(User).filter(User.group_id == group_id).all()]
        if member_ids:
            try:
                cur_rows = count_rows_for_user_ids_sync(
                    dsn, _admin.id, bool(_admin.is_admin), member_ids
                )
            except Exception:
                raise HTTPException(
                    status_code=500, detail="تعذّر التحقق من رصيد صفوف المجموعة حالياً."
                ) from None
            if cur_rows > new_limit:
                raise HTTPException(
                    status_code=400,
                    detail=("لا يمكن تقليل حد المجموعة: البيانات الحالية تتجاوز الحد الجديد."),
                )
    g.max_stored_large_rows = new_limit
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user_active(
    user_id: int,
    payload: UserActiveUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        u = auth_set_user_active(db=db, admin=admin, user_id=user_id, is_active=payload.is_active)
    except AuthServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    u2 = db.query(User).options(joinedload(User.group)).filter(User.id == u.id).first()
    assert u2 is not None
    return _user_out(u2)


@router.post("/users/{user_id}/reset-device", response_model=UserOut)
async def reset_device(
    user_id: int,
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        u = auth_reset_user_device(db=db, user_id=user_id)
    except AuthServiceError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    u2 = db.query(User).options(joinedload(User.group)).filter(User.id == u.id).first()
    assert u2 is not None
    return _user_out(u2)


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="لا يمكن حذف حسابك الحالي")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="المستخدم غير موجود")

    if user.is_admin:
        admins_count = db.query(User).filter(User.is_admin == True).count()  # noqa: E712
        if admins_count <= 1:
            raise HTTPException(status_code=400, detail="لا يمكن حذف آخر Admin في النظام")

    _sync_pg_membership(user_id, None)

    db.query(GeminiUsageEvent).filter(GeminiUsageEvent.user_id == user_id).delete(
        synchronize_session=False
    )
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id).delete(synchronize_session=False)
    db.delete(user)
    db.commit()
    return {"deleted": True, "user_id": user_id}
