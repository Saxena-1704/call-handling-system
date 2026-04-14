"""Text-to-Speech service using Cartesia."""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from cartesia import AsyncCartesia

from app.config import settings

logger = logging.getLogger(__name__)

# Find voice IDs at: https://play.cartesia.ai/voices
_VOICE_ID = "e07c00bc-4134-4eae-9ea4-1a55fb45746b"
_MODEL_ID = "sonic-2"

# Raw PCM — no container overhead, each chunk is immediately playable
_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding": "pcm_f32le",
    "sample_rate": 44100,
}

_client = AsyncCartesia(api_key=settings.cartesia_api_key)


@asynccontextmanager
async def session():
    """
    Open one Cartesia WebSocket for the lifetime of a browser session.

    Usage (in websocket_audio):
        async with tts_service.session() as tts:
            ...
            async for chunk in tts.stream(text):
                ...

    Keeping the connection alive avoids the per-utterance WebSocket open/close
    cycle that triggers Cartesia's connection rate limit after 2–3 turns.
    """
    async with _client.tts.websocket_connect() as connection:
        yield _TTSSession(connection)


class _TTSSession:
    def __init__(self, connection):
        self._conn = connection

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """
        Synthesize *text* on the shared WebSocket, yielding raw PCM chunks.

        A new Cartesia context (unique context_id) is created for each utterance
        so that concurrent/sequential requests are multiplexed safely.
        Abandoning the loop mid-stream (barge-in) is safe — the context is
        isolated from the next utterance's context.
        """
        ctx = self._conn.context()
        try:
            await ctx.send(
                model_id=_MODEL_ID,
                transcript=text,
                voice={"mode": "id", "id": _VOICE_ID},
                output_format=_OUTPUT_FORMAT,
                continue_=False,
            )
            await ctx.no_more_inputs()

            async for response in ctx.receive():
                if response.type == "chunk" and response.audio:
                    yield response.audio
        except Exception:
            logger.exception("Cartesia TTS error for text=%r", text[:60])
            raise
