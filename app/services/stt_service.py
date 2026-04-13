"""Speech-to-Text service using Deepgram WebSocket streaming API."""

import asyncio
import logging
from typing import AsyncIterator

from deepgram import AsyncDeepgramClient
from deepgram.listen.v1.types.listen_v1results import ListenV1Results

from app.config import settings

logger = logging.getLogger(__name__)

_client = AsyncDeepgramClient(api_key=settings.deepgram_api_key)

SAMPLE_RATE = 16000  # Hz — AudioContext on the frontend must match this

# Sentinels placed in audio_queue to signal control events
FINALIZE = object()    # button released — flush Deepgram buffer
CLOSE = object()       # session ending — close Deepgram WS


async def stream_transcribe(audio_queue: asyncio.Queue) -> AsyncIterator[str]:
    """
    Open one Deepgram WebSocket for the session lifetime.

    Consume items from audio_queue:
      - bytes  → forward as audio to Deepgram
      - FINALIZE → send Finalize (flush buffer after button release)
      - CLOSE    → send CloseStream and exit

    Yield each speech-final transcript as it arrives.
    """
    async with _client.listen.v1.connect(
        model="nova-3",
        language="en-US",
        smart_format="true",
        interim_results="true",
        utterance_end_ms=1000,
        endpointing=300,
        encoding="linear16",
        sample_rate=SAMPLE_RATE,
    ) as connection:

        transcript_queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def _send_audio() -> None:
            while True:
                try:
                    item = await asyncio.wait_for(audio_queue.get(), timeout=8.0)
                except asyncio.TimeoutError:
                    # No audio — send KeepAlive to prevent Deepgram idle timeout (~10s)
                    await connection.send_keep_alive()
                    continue

                if item is CLOSE:
                    await connection.send_close_stream()
                    break
                elif item is FINALIZE:
                    await connection.send_finalize()
                else:
                    await connection.send_media(item)

        async def _receive_messages() -> None:
            async for message in connection:
                if not isinstance(message, ListenV1Results):
                    continue
                try:
                    transcript = message.channel.alternatives[0].transcript
                except (AttributeError, IndexError):
                    continue
                if not transcript:
                    continue
                # Fire on natural speech end OR after an explicit Finalize flush
                is_done = (message.is_final and message.speech_final) or \
                          (message.is_final and message.from_finalize)
                if is_done:
                    logger.info("STT speech_final: %r", transcript)
                    await transcript_queue.put(transcript)
            await transcript_queue.put(None)  # connection closed

        send_task = asyncio.create_task(_send_audio())
        recv_task = asyncio.create_task(_receive_messages())

        while True:
            transcript = await transcript_queue.get()
            if transcript is None:
                break
            yield transcript

        await asyncio.gather(send_task, recv_task, return_exceptions=True)
