import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import sessionmaker

from core.plate_checker_ws import handle_plate_checker_client
from db import engine
from models import User
from services.subscription import deactivate_expired_subscription
from services.ws_check_live_ticket import consume_ticket

logger = logging.getLogger(__name__)

SessionLocal = sessionmaker(bind=engine)

router = APIRouter(tags=["check-live"])


@router.websocket("/ws/check-live")
async def check_live_websocket(websocket: WebSocket) -> None:
    await websocket.accept()
    ticket = (websocket.query_params.get("ticket") or "").strip()
    if not ticket:
        await websocket.close(code=4401, reason="missing ticket")
        return
    user_id = consume_ticket(ticket)
    if user_id is None:
        await websocket.close(code=4401, reason="invalid or used ticket")
        return
    u: User | None = None
    is_admin_ws = False
    is_active_ws = False
    with SessionLocal() as db:
        u = db.get(User, user_id)
        if u is not None:
            deactivate_expired_subscription(db, u)
            db.refresh(u)
            is_admin_ws = bool(u.is_admin)
            is_active_ws = bool(u.is_active)
    if u is None or not is_active_ws:
        await websocket.close(code=4401, reason="invalid ticket")
        return
    try:
        await handle_plate_checker_client(websocket, int(user_id), is_admin_ws)
    except WebSocketDisconnect:
        logger.debug("Live check WS disconnect user_id=%s", user_id)
    except Exception:
        logger.exception("Live check WS error user_id=%s", user_id)
