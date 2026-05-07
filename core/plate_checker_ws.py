"""WebSocket message handling for Live plate checker (FastAPI WebSocket)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from config import settings
from db import SessionLocal
from models import User
from core.excel_loader import (
    lookup_plate,
    merge_workbook_plate_column,
    normalize_plate,
    parse_excel_workbook_from_path,
    plate_candidates_from_text,
    union_column_headers,
)
from core.gemini_client import create_gemini_session
from core.session import (
    SessionState,
    get_or_create_session,
    get_session,
    remove_session,
    touch_session,
)
from services.check_temp_storage import temp_plate_exists_sync
from services.gemini_catalog import is_gemini_model_allowed_sync
from services.gemini_usage import flush_live_gemini_usage_for_session_sync
from services.live_excel_upload_store import pop_upload_path
from services.plate_utils import format_plate_display
from services.provider_key_pool import (
    delete_key_forever,
    get_sync_redis,
    iter_round_robin,
    park_until_midnight_utc,
    promote_parked_keys,
)
from services.provider_keys import classify_gemini_error
from services.user_gemini_policy import (
    GeminiPolicyError,
    ensure_user_can_start_request,
    resolve_model_for_user,
)

logger = logging.getLogger(__name__)

_live_sem = asyncio.Semaphore(max(1, int(settings.gemini_live_max_concurrent)))
_live_idle_ttl = max(60, int(settings.check_live_idle_ttl_seconds))
_live_hard_ttl = max(_live_idle_ttl, int(settings.check_live_hard_ttl_seconds))
_live_cleanup_tasks: dict[str, asyncio.Task] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _cancel_cleanup_task(session_key: str) -> None:
    task = _live_cleanup_tasks.pop(session_key, None)
    if task and not task.done():
        task.cancel()
        logger.info("Session %s cleanup cancelled - User returned.", session_key)


async def _schedule_idle_cleanup(session_key: str) -> None:
    try:
        await asyncio.sleep(_live_idle_ttl)
        session = get_session(session_key)
        if session is None:
            return
        if session.connected:
            return
        idle_sec = (_utc_now() - session.last_activity_at).total_seconds()
        age_sec = (_utc_now() - session.created_at).total_seconds()
        if idle_sec >= _live_idle_ttl or age_sec >= _live_hard_ttl:
            if session.genai_session:
                try:
                    await session.genai_session.close()
                except Exception:
                    logger.debug("Failed to close genai session for %s", session_key)
                session.genai_session = None
            remove_session(session_key)
            logger.info("Session %s cleaned up after 30m idl", session_key)
    except asyncio.CancelledError:
        pass
    finally:
        _live_cleanup_tasks.pop(session_key, None)


async def _send(websocket: WebSocket, payload: dict) -> None:
    try:
        await websocket.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as e:
        logger.error("Failed to send WS: %s", e)


async def _send_error(websocket: WebSocket, message: str, error_type: str = "general") -> None:
    await _send(
        websocket,
        {"type": "error", "data": {"message": message, "error_type": error_type}},
    )


def _merge_live_usage_metadata(session: SessionState, raw: dict) -> None:
    """Accumulate Live API usage when server sends usageMetadata (cumulative max).

    Field names vary by API revision; we accept camelCase (typical JSON over the wire)
    and snake_case. REST `google-genai` usage for generate_content was verified:
    prompt_token_count / candidates_token_count / total_token_count on the SDK object;
    Live raw WS messages often mirror protobuf JSON (e.g. promptTokenCount).
    """
    um = raw.get("usageMetadata") or raw.get("usage_metadata")
    if not isinstance(um, dict):
        return

    def _read_int(d: dict, *keys: str) -> int | None:
        for k in keys:
            v = d.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    continue
        return None

    pt = _read_int(
        um,
        "promptTokenCount",
        "prompt_token_count",
        "promptTokens",
        "totalPromptTokens",
    )
    ot = _read_int(
        um,
        "candidatesTokenCount",
        "candidates_token_count",
        "candidatesTokens",
        "outputTokenCount",
        "output_token_count",
    )
    tt = _read_int(um, "totalTokenCount", "total_token_count", "totalTokens")
    if pt is not None:
        session.live_usage_prompt_tokens = max(session.live_usage_prompt_tokens, pt)
    if ot is not None:
        session.live_usage_output_tokens = max(session.live_usage_output_tokens, ot)
    if tt is not None:
        session.live_usage_total_tokens = max(session.live_usage_total_tokens, tt)


def _segment_for_current_turn_transcript(session: SessionState) -> str:
    """Only the STT segment since the last model turnComplete (ignore prior turns)."""
    full = (session.input_transcript or "").strip()
    anchor = max(0, int(session.transcript_turn_anchor))
    if anchor >= len(full):
        return full
    return full[anchor:].strip()


async def _maybe_live_sheet_check(websocket: WebSocket, session: SessionState) -> None:
    """While user speaks: infer plate from STT and lookup in Excel (real-time)."""
    if not session.check_temp_enabled and (
        not session.excel_loaded or not (session.excel_plate_column or "").strip()
    ):
        return
    t = _segment_for_current_turn_transcript(session)
    if len(t) < 3:
        return
    cands = plate_candidates_from_text(t)
    if not cands:
        return
    best = cands[-1]
    key = normalize_plate(best)
    if len(key) < 3:
        return
    # Allow re-checking the same plate text so user corrections are not suppressed.
    session.last_live_check_key = key
    if session.check_temp_enabled:
        found = await asyncio.to_thread(
            temp_plate_exists_sync,
            session.check_temp_dsn,
            session.user_id,
            session.is_admin,
            session_token=session.check_temp_session_token,
            plate_text=best,
        )
        safe_row = {}
    else:
        found, row_data = lookup_plate(session.excel_plates, best)
        safe_row = {
            k: (str(v) if v is not None else None)
            for k, v in row_data.items()
            if not str(k).startswith("_")
        }
    plate_show = format_plate_display(best) or best
    matched = (session.excel_plate_column or "") if (found and session.check_temp_enabled) else ""
    hit_sheet = "postgres_temp" if (found and session.check_temp_enabled) else ""
    if not session.check_temp_enabled:
        matched = row_data.get("_matched_column", "") if found else ""
        hit_sheet = row_data.get("_sheet", "") if found else ""
    await _send(
        websocket,
        {
            "type": "live_plate",
            "data": {
                "plate": plate_show,
                "found": found,
                "details": safe_row,
                "transcript": t,
                "compare_column": matched,
                "sheet": hit_sheet,
            },
        },
    )


def _strip_markdown_json_fence(text: str) -> str:
    t = text.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return t


def _entry_moving(item: dict) -> bool:
    """True if model marked moving or Arabic «متحرك» appears on that plate's row."""
    if bool(item.get("moving")):
        return True
    for k in ("vehicle_type", "notes"):
        v = item.get(k)
        if v is not None and "متحرك" in str(v):
            return True
    return False


def _rest_plate_string(obj: dict) -> str | None:
    """
    Build 'LLL NNNN' from Live/REST JSON fields plate_letters + plate_numbers
    (same shape as Tafrigh prompts in gemini_client).
    """
    raw_letters = obj.get("plate_letters")
    raw_numbers = obj.get("plate_numbers")
    if raw_letters is None and raw_numbers is None:
        return None
    letters = "".join(re.findall(r"[\u0621-\u064A]", str(raw_letters or "")))
    digits = "".join(re.findall(r"\d", str(raw_numbers or "")))
    if len(letters) != 3 or len(digits) != 4:
        return None
    return f"{letters} {digits}"


def _plate_entry_from_dict(item: dict) -> dict[str, Any] | None:
    """One plate row: explicit ``plate`` or composed ``plate_letters`` + ``plate_numbers``."""
    moving = _entry_moving(item)
    p = item.get("plate")
    if p is not None and str(p).strip():
        return {"plate": str(p).strip(), "moving": moving}
    composed = _rest_plate_string(item)
    if composed:
        return {"plate": composed, "moving": moving}
    return None


def _parse_plate_payload(blob: str) -> list[dict[str, Any]]:
    """
    Parse Gemini JSON: objects with plate + optional moving,
    or plate_letters + plate_numbers (Live system prompt shape).
    Returns [{"plate": str|None, "moving": bool}, ...].
    """
    blob = blob.strip()
    if not blob:
        return []

    def collect_from_obj(obj: dict) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if "plate" in obj:
            e = _plate_entry_from_dict(obj)
            if e:
                out.append(e)
        inner = obj.get("plates")
        if isinstance(inner, list):
            for el in inner:
                if isinstance(el, dict):
                    e = _plate_entry_from_dict(el)
                    if e:
                        out.append(e)
                elif isinstance(el, str):
                    out.append({"plate": el, "moving": False})
        if not out:
            e = _plate_entry_from_dict(obj)
            if e:
                out.append(e)
        return out

    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
        # Prefer a top-level JSON array slice before a single-object slice — using
        # first "{" .. last "}" on array text breaks multi-plate payloads.
        lb, rb = blob.find("["), blob.rfind("]")
        if lb >= 0 and rb > lb:
            try:
                data = json.loads(blob[lb : rb + 1])
            except json.JSONDecodeError:
                data = None
        if data is None:
            lo, hi = blob.find("{"), blob.rfind("}") + 1
            if lo >= 0 and hi > lo:
                try:
                    data = json.loads(blob[lo:hi])
                except json.JSONDecodeError:
                    return []
            else:
                return []

    if isinstance(data, list):
        plates: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                e = _plate_entry_from_dict(item)
                if e:
                    plates.append(e)
            elif isinstance(item, str):
                plates.append({"plate": item, "moving": False})
        return plates
    if isinstance(data, dict):
        return collect_from_obj(data)
    return []


def _sanitize_live_plate_text(plate: Any) -> str | None:
    """
    Enforce Live plate constraints:
    - exactly 3 Arabic letters
    - exactly 4 digits
    - normalize common over-expanded letter names (e.g. عين/عن -> ع)
    """
    if plate is None:
        return None
    raw = str(plate).strip()
    if not raw:
        return None

    t = raw
    # Normalize common letter-name expansions to single letters.
    t = re.sub(r"(?:\b|^)(عين|عاين|عن)(?:\b|$)", "ع", t)
    t = re.sub(r"(?:\b|^)(غين|غاين)(?:\b|$)", "غ", t)
    t = re.sub(r"(?:\b|^)(حاء|حا|حه)(?:\b|$)", "ح", t)
    t = re.sub(r"(?:\b|^)(هاء|ها|هه)(?:\b|$)", "ه", t)

    letters = "".join(re.findall(r"[\u0621-\u064A]", t))
    digits = "".join(re.findall(r"\d", t))

    if not letters or not digits:
        return None
    if len(letters) != 3 or len(digits) != 4:
        return None

    return f"{letters} {digits}"


def _plate_value_from_entry(entry: Any) -> Any:
    if isinstance(entry, dict):
        return entry.get("plate")
    return entry


async def _lookup_plate_outcome(
    session: SessionState, raw: str
) -> tuple[bool | None, dict[str, Any], str, str]:
    """Sanitized plate text -> (found, safe_row, matched_column, hit_sheet)."""
    if session.check_temp_enabled:
        found = await asyncio.to_thread(
            temp_plate_exists_sync,
            session.check_temp_dsn,
            session.user_id,
            session.is_admin,
            session_token=session.check_temp_session_token,
            plate_text=raw,
        )
        return (bool(found), {}, session.excel_plate_column or "", "postgres_temp")
    if session.excel_loaded:
        if not (session.excel_plate_column or "").strip():
            return (None, {}, "", "")
        found, row_data = lookup_plate(session.excel_plates, raw)
        safe_row = {
            k: (str(v) if v is not None else None)
            for k, v in row_data.items()
            if not str(k).startswith("_")
        }
        matched = row_data.get("_matched_column", "") if found else ""
        hit_sheet = row_data.get("_sheet", "") if found else ""
        return (bool(found), safe_row, matched, hit_sheet)
    return (None, {}, "", "")


def _dedupe_sync_items_last_wins(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per normalized plate; later entries win (authoritative final pass)."""
    merged: dict[str, dict[str, Any]] = {}
    for it in items:
        p = str(it.get("plate") or "").strip()
        nk = normalize_plate(p) if p else ""
        if not nk:
            continue
        merged[nk] = it
    return list(merged.values())


async def _emit_check_session_sync(websocket: WebSocket, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    await _send(
        websocket,
        {"type": "check_session_sync", "data": {"items": items}},
    )


async def _emit_plate_result(
    websocket: WebSocket,
    session: SessionState,
    plate: Any,
    *,
    moving: bool = False,
) -> None:
    pv = _plate_value_from_entry(plate)
    normalized_plate = _sanitize_live_plate_text(pv)
    if not normalized_plate:
        await _send(websocket, {"type": "no_plate", "data": {}})
        return
    raw = normalized_plate
    plate_str = format_plate_display(raw) or raw
    if session.check_temp_enabled:
        found, safe_row, matched, hit_sheet = await _lookup_plate_outcome(session, raw)
        await _send(
            websocket,
            {
                "type": "plate_result",
                "data": {
                    "plate": plate_str,
                    "found": found,
                    "moving": bool(moving),
                    "details": safe_row,
                    "compare_column": matched,
                    "sheet": hit_sheet,
                    "index_count": 0,
                },
            },
        )
    elif session.excel_loaded:
        if not (session.excel_plate_column or "").strip():
            await _send(
                websocket,
                {
                    "type": "plate_result",
                    "data": {
                        "plate": plate_str,
                        "found": None,
                        "moving": bool(moving),
                        "details": {},
                        "compare_column": "",
                        "sheet": "",
                        "index_count": 0,
                        "needs_plate_column": True,
                    },
                },
            )
            return
        found, safe_row, matched, hit_sheet = await _lookup_plate_outcome(session, raw)
        await _send(
            websocket,
            {
                "type": "plate_result",
                "data": {
                    "plate": plate_str,
                    "found": found,
                    "moving": bool(moving),
                    "details": safe_row,
                    "compare_column": matched,
                    "sheet": hit_sheet,
                    "index_count": len(session.excel_plates),
                },
            },
        )
    else:
        await _send(
            websocket,
            {
                "type": "plate_result",
                "data": {
                    "plate": plate_str,
                    "found": None,
                    "moving": bool(moving),
                    "details": {},
                    "compare_column": "",
                    "sheet": "",
                    "needs_plate_column": False,
                },
            },
        )


async def _emit_model_plate_if_new(websocket: WebSocket, session: SessionState, plate: Any) -> bool:
    """Emit model plate result once per normalized plate within a turn."""
    moving = _entry_moving(plate) if isinstance(plate, dict) else False
    pv = _plate_value_from_entry(plate)
    normalized_plate = _sanitize_live_plate_text(pv)
    if not normalized_plate:
        return False
    key = normalize_plate(normalized_plate)
    if key:
        if key in session.model_plate_norm_keys:
            return False
        session.model_plate_norm_keys.add(key)
    await _emit_plate_result(websocket, session, normalized_plate, moving=moving)
    return True


async def _process_plate_text(websocket: WebSocket, session: SessionState, text: str) -> None:
    text = text.strip()
    logger.info("Gemini text: %s", text[:500])
    blob = _strip_markdown_json_fence(text)
    plates = _parse_plate_payload(blob)
    if not plates:
        logger.warning("No plates parsed from Gemini: %s", text[:500])
        await _send(websocket, {"type": "raw_text", "data": text})
        return

    valid = [p for p in plates if p.get("plate") is not None and str(p.get("plate") or "").strip()]
    if not valid:
        await _send(websocket, {"type": "no_plate", "data": {}})
        return

    sync_items: list[dict[str, Any]] = []

    for entry in valid:
        raw = _sanitize_live_plate_text(_plate_value_from_entry(entry))
        if not raw:
            continue
        plate_str = format_plate_display(raw) or raw
        moving = _entry_moving(entry)
        found, _, _, _ = await _lookup_plate_outcome(session, raw)
        sync_items.append({"plate": plate_str, "found": found, "moving": moving})
        await _emit_model_plate_if_new(websocket, session, entry)

    if sync_items:
        await _emit_check_session_sync(websocket, _dedupe_sync_items_last_wins(sync_items))


async def handle_client_messages(
    websocket: WebSocket, session: SessionState, session_key: str
) -> None:
    try:
        while True:
            raw = await websocket.receive_text()
            touch_session(session_key)
            try:
                data = json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "excel_upload_ref":
                    logger.info("Excel upload reference received")
                    tmp_path = ""
                    try:
                        pw = (data.get("password") or "").strip()
                        upload_token = (data.get("upload_token") or "").strip()
                        tmp_path = pop_upload_path(upload_token) or ""
                        if not tmp_path:
                            await _send_error(
                                websocket,
                                "انتهت صلاحية مرجع ملف Excel — أعد الرفع.",
                                "excel_error",
                            )
                            continue
                        sheets_map, sheet_names = parse_excel_workbook_from_path(tmp_path, pw)
                        if not sheet_names:
                            await _send_error(
                                websocket,
                                "الملف لا يحتوي صفحات.",
                                "excel_error",
                            )
                            continue
                        session.excel_sheets = sheets_map
                        session.excel_loaded = True
                        union_headers = union_column_headers(sheets_map)
                        logger.info(
                            "Excel parsed from temp file: %s sheets, %s union column titles",
                            len(sheet_names),
                            len(union_headers),
                        )
                        session.excel_plates = {}
                        session.excel_columns = union_headers
                        session.excel_rows = []
                        session.excel_plate_column = ""
                        session.excel_active_sheet = ""
                        session.last_live_check_key = ""
                        session.transcript_turn_anchor = 0
                        session.model_plate_norm_keys.clear()
                        columns_by_sheet = {n: sheets_map[n][1] for n in sheet_names}
                        await _send(
                            websocket,
                            {
                                "type": "excel_loaded",
                                "data": {
                                    "columns": union_headers,
                                    "columns_by_sheet": columns_by_sheet,
                                    "sheets_scanned": sheet_names,
                                    "count": 0,
                                    "needs_plate_column": True,
                                },
                            },
                        )
                    except Exception:
                        logger.exception("Live WS excel upload failed")
                        await _send_error(
                            websocket,
                            "حدث خطأ أثناء تحميل الملف. حاول مرة أخرى.",
                            "excel_error",
                        )
                    finally:
                        if tmp_path:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass

                elif msg_type == "set_plate_column":
                    col = (data.get("column") or "").strip()
                    if not session.excel_loaded or not session.excel_sheets:
                        await _send_error(websocket, "ارفع ملف Excel أولاً.", "excel_error")
                        continue
                    sheets_map = session.excel_sheets
                    if not col or not any(col in sheets_map[n][1] for n in sheets_map):
                        await _send_error(
                            websocket,
                            "اختر عنوان عمود موجود في الملف.",
                            "excel_error",
                        )
                        continue
                    try:
                        merged = merge_workbook_plate_column(sheets_map, col)
                        session.excel_plates = merged
                        session.excel_plate_column = col
                        session.last_live_check_key = ""
                        session.transcript_turn_anchor = 0
                        session.model_plate_norm_keys.clear()
                        await _send(
                            websocket,
                            {
                                "type": "plate_column_ready",
                                "data": {
                                    "plate_column": col,
                                    "count": len(merged),
                                },
                            },
                        )
                    except ValueError as e:
                        await _send_error(websocket, str(e), "excel_error")

                elif msg_type == "audio":
                    if session.genai_session:
                        b64 = data.get("data", "") or ""
                        if isinstance(b64, str) and b64:
                            session.live_audio_b64_chars += len(b64)
                        await session.genai_session.send_audio(b64)

                elif msg_type == "end_of_turn":
                    if session.genai_session:
                        await session.genai_session.send_end_of_turn()

                elif msg_type == "text":
                    if session.genai_session:
                        await session.genai_session.send_text(data.get("data", ""))
                elif msg_type == "ping":
                    await _send(websocket, {"type": "pong", "data": {}})

            except Exception as e:
                logger.error("Client message error: %s\n%s", e, traceback.format_exc())
    except WebSocketDisconnect:
        raise


async def handle_gemini_responses(websocket: WebSocket, session: SessionState) -> None:
    if not session.genai_session:
        return
    try:
        async for msg in session.genai_session:
            try:
                if isinstance(msg, dict):
                    _merge_live_usage_metadata(session, msg)

                if msg.get("serverContent", {}).get("interrupted"):
                    await _send(websocket, {"type": "interrupted", "data": {}})
                    session.text_buffer = ""
                    session.input_transcript = ""
                    session.transcript_turn_anchor = 0
                    session.last_live_check_key = ""
                    session.model_plate_norm_keys.clear()
                    continue

                sc = msg.get("serverContent", {})
                it = sc.get("inputTranscription") or {}
                if isinstance(it, dict) and it.get("text"):
                    full_text = it["text"]
                    session.input_transcript = full_text
                    if session.transcript_turn_anchor > len(full_text):
                        session.transcript_turn_anchor = 0
                    await _send(
                        websocket,
                        {
                            "type": "live_transcript",
                            "data": session.input_transcript,
                        },
                    )
                    await _maybe_live_sheet_check(websocket, session)

                ot = sc.get("outputTranscription") or {}
                if isinstance(ot, dict) and ot.get("text"):
                    session.text_buffer += ot["text"]
                model_turn = sc.get("modelTurn", {})
                for part in model_turn.get("parts", []):
                    if "text" in part:
                        session.text_buffer += part["text"]

                # Low latency: emit as soon as the buffer parses (may duplicate turnComplete pass;
                # client merges hits by plate; check_session_sync is de-duped on the server).
                if session.text_buffer.strip():
                    blob_now = _strip_markdown_json_fence(session.text_buffer)
                    partial_plates = _parse_plate_payload(blob_now)
                    for plate in partial_plates:
                        await _emit_model_plate_if_new(websocket, session, plate)

                if sc.get("turnComplete"):
                    if session.text_buffer.strip():
                        await _process_plate_text(websocket, session, session.text_buffer)
                    # Slice future STT at end of this turn so cumulative transcripts
                    # cannot re-match a plate from a previous user utterance.
                    session.transcript_turn_anchor = len(session.input_transcript or "")
                    # Start next model turn from a clean slate.
                    session.text_buffer = ""
                    session.input_transcript = ""
                    session.last_live_check_key = ""
                    session.model_plate_norm_keys.clear()
                    await _send(websocket, {"type": "turn_complete"})

            except Exception as e:
                logger.error("Gemini response error: %s\n%s", e, traceback.format_exc())
    except Exception as e:
        if "connection closed" not in str(e).lower():
            logger.error("Gemini receive error: %s", e)
        raise


async def cleanup_session(session: Optional[SessionState], session_id: str) -> None:
    try:
        if session and session.genai_session:
            await session.genai_session.close()
        remove_session(session_id)
        logger.info("Session %s cleaned up", session_id)
    except Exception as e:
        logger.error("Cleanup error: %s", e)


async def handle_plate_checker_client(
    websocket: WebSocket, user_id: int, is_admin: bool = False
) -> None:
    session_key = ""
    session: SessionState | None = None
    logger.info("New Live check connection user_id=%s", user_id)

    try:
        raw_init = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
        init = json.loads(raw_init)
        if init.get("type") != "init":
            await _send_error(
                websocket,
                "الرسالة الأولى يجب أن تكون init (مع client_id و live_model).",
                "general",
            )
            if session:
                await cleanup_session(session, session_key or str(user_id))
            return
        client_id = (init.get("client_id") or "").strip()
        if not client_id:
            await _send_error(
                websocket,
                "Missing client_id",
                "general",
            )
            if session:
                await cleanup_session(session, session_key or str(user_id))
            return
        session_key = f"{user_id}:{client_id}"
        _cancel_cleanup_task(session_key)
        session = get_or_create_session(session_key)
        session.connected = True
        session.user_id = int(user_id)
        session.is_admin = bool(is_admin)
        temp_session_token = (init.get("temp_session_token") or "").strip()
        dsn_pg = (settings.check_postgres_dsn or "").strip()
        session.check_temp_enabled = bool(temp_session_token and dsn_pg)
        session.check_temp_session_token = temp_session_token if session.check_temp_enabled else ""
        session.check_temp_dsn = dsn_pg if session.check_temp_enabled else ""
        touch_session(session_key)
        live_model_req = (init.get("live_model") or "").strip()
        try:
            with SessionLocal() as db:
                db_user = db.get(User, int(user_id))
                if db_user is None:
                    raise GeminiPolicyError("المستخدم غير موجود.")
                ensure_user_can_start_request(db, db_user)
                # Non-admin users cannot override admin-assigned model from client payload.
                request_model = live_model_req if is_admin else ""
                live_model = resolve_model_for_user(db, db_user, "live", request_model)
        except GeminiPolicyError as e:
            await _send_error(websocket, str(e), "general")
            session.connected = False
            _live_cleanup_tasks[session_key] = asyncio.create_task(
                _schedule_idle_cleanup(session_key)
            )
            return
        if not await asyncio.to_thread(is_gemini_model_allowed_sync, "live", live_model):
            await _send_error(
                websocket,
                "موديل Live غير مسموح أو غير مفعّل.",
                "general",
            )
            session.connected = False
            _live_cleanup_tasks[session_key] = asyncio.create_task(
                _schedule_idle_cleanup(session_key)
            )
            return
    except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect) as e:
        logger.info("Live check init failed: %s", e)
        try:
            await websocket.close(code=4408)
        except Exception:
            pass
        if session_key and session:
            session.connected = False
            _live_cleanup_tasks[session_key] = asyncio.create_task(
                _schedule_idle_cleanup(session_key)
            )
        return

    try:
        async with _live_sem:
            if session is None:
                raise RuntimeError("Session not initialized")
            connected_live = False
            r = get_sync_redis()
            if r:
                promote_parked_keys(r, "gemini")
                for key_id, api_key in iter_round_robin(r, "gemini"):
                    try:
                        async with create_gemini_session(
                            api_key, live_model=live_model
                        ) as gemini_session:
                            session.genai_session = gemini_session
                            session.gemini_redis_key_id = key_id
                            session.gemini_live_model = live_model
                            session.live_connected_at = _utc_now()
                            session.live_usage_prompt_tokens = 0
                            session.live_usage_output_tokens = 0
                            session.live_usage_total_tokens = 0
                            session.live_audio_b64_chars = 0
                            await _send(websocket, {"type": "ready"})
                            connected_live = True

                            client_task = asyncio.create_task(
                                handle_client_messages(websocket, session, session_key)
                            )
                            gemini_task = asyncio.create_task(
                                handle_gemini_responses(websocket, session)
                            )

                            done, pending = await asyncio.wait(
                                [client_task, gemini_task],
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            for t in pending:
                                t.cancel()
                                try:
                                    await t
                                except asyncio.CancelledError:
                                    pass
                            for t in done:
                                exc = t.exception()
                                if exc is not None and not isinstance(
                                    exc, (WebSocketDisconnect, asyncio.CancelledError)
                                ):
                                    logger.error("Live check task ended with error: %s", exc)
                        break
                    except Exception as e:
                        bucket = classify_gemini_error(e)
                        if bucket == "quota":
                            park_until_midnight_utc(r, "gemini", key_id)
                            logger.warning("Live Gemini quota; parked key %s", key_id[:8])
                            continue
                        if bucket == "invalid":
                            delete_key_forever(r, "gemini", key_id)
                            logger.warning("Live Gemini invalid key removed %s", key_id[:8])
                            continue
                        logger.warning("Live Gemini connect/session failed: %s", e)
                        continue
            if not connected_live:
                await _send_error(
                    websocket,
                    "خدمة التشيك المباشر غير متاحة مؤقتاً (503).",
                    "general",
                )

    except Exception as e:
        err = str(e)
        if "Quota" in err:
            await _send_error(websocket, "تم تجاوز الحصة، انتظر قليلاً.", "quota_exceeded")
        elif "connection closed" not in err.lower():
            logger.error("Session error: %s\n%s", e, traceback.format_exc())
            await _send_error(websocket, "حدث خطأ، حاول مرة أخرى.", "general")
    finally:
        if session_key and session:
            kid = (session.gemini_redis_key_id or "").strip()
            if kid:
                try:
                    await asyncio.to_thread(
                        flush_live_gemini_usage_for_session_sync,
                        session,
                        session_key,
                    )
                except Exception:
                    logger.exception("Live Gemini usage flush failed for %s", session_key)
            session.gemini_redis_key_id = ""
            session.gemini_live_model = ""
            session.live_connected_at = None
            session.live_usage_prompt_tokens = 0
            session.live_usage_output_tokens = 0
            session.live_usage_total_tokens = 0
            session.live_audio_b64_chars = 0
            session.connected = False
            session.genai_session = None
            touch_session(session_key)
            _cancel_cleanup_task(session_key)
            _live_cleanup_tasks[session_key] = asyncio.create_task(
                _schedule_idle_cleanup(session_key)
            )
