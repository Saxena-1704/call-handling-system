"""Text-to-Speech service using Cartesia."""

from typing import AsyncIterator

from cartesia import AsyncCartesia

from app.config import settings

# Find voice IDs at: https://play.cartesia.ai/voices
_VOICE_ID = "e07c00bc-4134-4eae-9ea4-1a55fb45746b"
_MODEL_ID = "sonic-2"

# Raw PCM — no container overhead, each chunk is immediately playable
_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding": "pcm_f32le",
    "sample_rate": 44100,
}


async def stream_audio(text: str) -> AsyncIterator[bytes]:
    """
    Stream TTS audio via Cartesia WebSocket, yielding raw PCM chunks as they arrive.

    Args:
        text: The text to synthesize.

    Yields:
        Raw PCM bytes (f32le, 44100 Hz, mono) ready to send to the browser.
    """
    async with AsyncCartesia(api_key=settings.cartesia_api_key) as client:
        async with client.tts.websocket_connect() as connection:
            ctx = connection.context()

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



