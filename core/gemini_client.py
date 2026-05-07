"""
Gemini Live API — direct WebSocket connection (no SDK).
Bypasses all SDK/version issues entirely.
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import websockets

logger = logging.getLogger(__name__)

# Live / Bidi API is documented on v1beta (v1alpha rejects current models with 1008).
# https://ai.google.dev/api/live
GEMINI_WS_URL = (
    "wss://generativelanguage.googleapis.com"
    "/ws/google.ai.generativelanguage.v1beta"
    ".GenerativeService.BidiGenerateContent"
)

# English-only instructions for the Live model (systemInstruction.parts).
SYSTEM_INSTRUCTION = (
   """ Extract Egyptian license plates from spoken Arabic audio.

Rules:
- Plate = exactly 3 Arabic letters + space + 4 digits (0-9).
- Convert spoken letter names to ONE Arabic letter (عين→ع، لام→ل، ألف→ا).
- Never output full letter names.
- Keep letters exactly as spoken without adding or merging.
- Preserve leading zeros.
- If unclear → return null.
- If "متحرك" → moving=true else false.
- If "تعديل" → ignore previous plate, keep only after it.
- Arabic letters only, no English.

Forbidden Letters:
- NEVER output any of these letters:
ت، ث، ج، خ، ذ، ز، ش، ض، ظ، غ، ف
- If any of these letters are detected or suspected → return null."""
    
)

USER_PROMPT = """Return JSON ONLY:
{"plate":"<letters> <digits>","moving":false}
OR
{"plates":[{"plate":"<letters> <digits>","moving":false}]}
OR
{"plate":null,"moving":false}

Rules:
- Letters must be exactly 3 Arabic letters.
- Digits must be exactly 4 numbers (0-9).
- Format: letters + space + digits.
- No extra text, no markdown, no explanations.
- Always include "moving"."""

# Live-capable model IDs come only from the admin Gemini catalog (channel=live);
# the WebSocket client sends the chosen model_id after /api/config/gemini-models.
_LIVE_VOICE = os.getenv("GEMINI_LIVE_VOICE", "Kore")


class GeminiLiveSession:
    """Thin wrapper around a raw WebSocket to Gemini Live."""

    def __init__(self, ws):
        self._ws = ws

    async def send_audio(self, base64_data: str, end_of_turn: bool = False):
        # Prefer `audio` blob (mediaChunks deprecated per Live API reference).
        payload = {
            "realtimeInput": {
                "audio": {
                    "data": base64_data,
                    "mimeType": "audio/pcm;rate=16000",
                }
            }
        }
        await self._ws.send(json.dumps(payload))
        if end_of_turn:
            await self._ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

    async def send_end_of_turn(self):
        # Empty clientContent.turnComplete alone is invalid on Gemini 3.1 Live (1007).
        await self._ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

    async def send_text(self, text: str):
        await self._ws.send(json.dumps({"realtimeInput": {"text": text}}))

    async def receive_one(self):
        raw = await self._ws.recv()
        return json.loads(raw)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive_one()
        except websockets.exceptions.ConnectionClosed:
            raise StopAsyncIteration

    async def close(self):
        try:
            await self._ws.close()
        except Exception:
            pass


async def _try_connect_one(api_key: str, model: str) -> GeminiLiveSession:
    url = f"{GEMINI_WS_URL}?key={api_key}"
    connect_kwargs = {
        "open_timeout": 15,
        "ping_interval": 20,
        "ping_timeout": 10,
    }
    try:
        # websockets>=13 prefers additional_headers.
        ws = await websockets.connect(
            url,
            additional_headers={"Content-Type": "application/json"},
            **connect_kwargs,
        )
    except TypeError as e:
        # Some environments still expose the older extra_headers name.
        if "unexpected keyword argument 'additional_headers'" not in str(e):
            raise
        ws = await websockets.connect(
            url,
            extra_headers={"Content-Type": "application/json"},
            **connect_kwargs,
        )
    setup = {
        "setup": {
            "model": model,
            "generationConfig": {
                # Keep AUDIO modality for protocol compatibility with native-audio model.
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {
                        "prebuiltVoiceConfig": {"voiceName": _LIVE_VOICE},
                    }
                },
            },
            # We parse JSON from transcription text; generated audio is ignored by frontend.
            "outputAudioTranscription": {},
            "inputAudioTranscription": {},
            "systemInstruction": {
                "parts": [
                    {"text": SYSTEM_INSTRUCTION},
                    {"text": USER_PROMPT},
                ]
            },
        }
    }
    try:
        await ws.send(json.dumps(setup))
        resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
        resp = json.loads(resp_raw)
        if "error" in resp:
            await ws.close()
            raise RuntimeError(f"Setup error: {resp['error']}")
        logger.info("Connected Live model: %s", model)
        return GeminiLiveSession(ws)
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
        raise


@asynccontextmanager
async def create_gemini_session(
    api_key: str | None = None,
    live_model: str | None = None,
):
    """Connect to Gemini Live using the single model id chosen by the client (admin catalog)."""
    key = (api_key or "").strip()
    if not key:
        raise ValueError("GEMINI API key missing")

    primary = (live_model or "").strip()
    if not primary:
        raise ValueError(
            "live_model is required — add enabled Live models in admin and pick one in the UI."
        )

    try:
        session = await _try_connect_one(key, primary)
    except Exception as e:
        logger.warning("Live connect failed model=%s: %s", primary, e)
        raise RuntimeError(
            f"Could not start Gemini Live session for model {primary!r}. Error: {e!r}"
        ) from e

    logger.info("Connected Live model=%s", primary)

    try:
        yield session
    finally:
        await session.close()
