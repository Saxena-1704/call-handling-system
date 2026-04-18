"""Text-to-Speech service using Sarvam AI (WebSocket streaming)."""

import base64
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import numpy as np
from sarvamai import AsyncSarvamAI, AudioOutput, EventResponse

from app.config import settings

logger = logging.getLogger(__name__)

_SPEAKER = "shubh"
_LANGUAGE = "en-IN"
_MODEL = "bulbul:v3"

# bulbul:v3 defaults to 24000 Hz for PCM output
_SARVAM_SAMPLE_RATE = 24000

# Must match the browser AudioContext (see index.html → enqueueAudio)
_TARGET_SAMPLE_RATE = 44100

_client = AsyncSarvamAI(api_subscription_key=settings.sarvam_api_key)


def _int16_to_f32le(pcm_int16: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Convert LINEAR16 PCM bytes → f32le PCM bytes, resampling if needed."""
    samples = np.frombuffer(pcm_int16, dtype=np.int16).astype(np.float32) / 32768.0

    if src_rate != dst_rate:
        ratio = dst_rate / src_rate
        n_out = int(len(samples) * ratio)
        indices = np.arange(n_out) / ratio
        indices = np.clip(indices, 0, len(samples) - 1)
        samples = np.interp(indices, np.arange(len(samples)), samples).astype(
            np.float32
        )

    return samples.tobytes()


@asynccontextmanager
async def session():
    """
    Returns a Sarvam TTS handle whose interface mirrors tts_service.session().

    Unlike Cartesia, Sarvam closes the WebSocket after each utterance's "final"
    event, so we open a fresh connection inside each stream() call instead of
    keeping one alive for the whole browser session.
    """
    logger.info("Sarvam TTS session created (model=%s, speaker=%s)", _MODEL, _SPEAKER)
    yield _SarvamTTSSession()


class _SarvamTTSSession:
    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """
        Synthesize *text*, yielding raw f32le PCM chunks at 44100 Hz.

        A new Sarvam WebSocket is opened per utterance (the server closes the
        connection after the final event).  Output format is identical to
        tts_service (Cartesia) so the browser playback code works unchanged.
        """
        try:
            async with _client.text_to_speech_streaming.connect(
                model=_MODEL, send_completion_event=True
            ) as ws:
                await ws.configure(
                    target_language_code=_LANGUAGE,
                    speaker=_SPEAKER,
                    output_audio_codec="linear16",
                )
                await ws.convert(text)
                await ws.flush()

                chunk_count = 0
                async for message in ws:
                    logger.info(
                        "Sarvam msg: type=%s, class=%s, repr=%.300s",
                        getattr(message, "type", "?"),
                        type(message).__name__,
                        repr(message)[:300],
                    )
                    if isinstance(message, AudioOutput):
                        pcm_data = base64.b64decode(message.data.audio)
                        chunk_count += 1
                        converted = _int16_to_f32le(
                            pcm_data, _SARVAM_SAMPLE_RATE, _TARGET_SAMPLE_RATE
                        )
                        logger.info(
                            "Sarvam chunk #%d: raw=%d → converted=%d bytes",
                            chunk_count, len(pcm_data), len(converted),
                        )
                        yield converted
                    elif isinstance(message, EventResponse):
                        logger.info("Sarvam event: %s", message.data.event_type)
                        if message.data.event_type == "final":
                            break
                logger.info("Sarvam stream done — %d chunks yielded", chunk_count)
        except Exception:
            logger.exception("Sarvam TTS error for text=%r", text[:60])
            raise
