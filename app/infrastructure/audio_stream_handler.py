"""
WebSocket audio stream handler with Voice Activity Detection (VAD).

Manages bidirectional L16 linear PCM audio between the telephony provider
(Twilio / Telnyx) and the internal STT/TTS pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import AsyncGenerator, Callable, Awaitable

import numpy as np
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# ── Audio constants (Twilio media-stream defaults) ────────────────────
SAMPLE_RATE = 8000          # Hz  (Twilio L16)
SAMPLE_WIDTH = 2            # bytes (16-bit PCM)
FRAME_DURATION_MS = 32  # Silero VAD minimum: 256 samples at 8 kHz, 512 at 16 kHz      # ms per frame
FRAME_SIZE = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 160 samples


class VADProcessor:
    """
    Lightweight Voice Activity Detection using Silero VAD.

    Silero runs entirely on CPU in ~1 ms per frame, keeping
    the latency budget tight.
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 300,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_size = sample_rate * FRAME_DURATION_MS // 1000
        self.threshold = threshold
        self.min_speech_frames = min_speech_ms // FRAME_DURATION_MS
        self.min_silence_frames = min_silence_ms // FRAME_DURATION_MS

        self._speech_count = 0
        self._silence_count = 0
        self.is_speech = False

        # Lazy-load Silero model
        self._model = None

    # ── public ────────────────────────────────────────────────────
    def load_model(self) -> None:
        """Load Silero VAD model (call once at startup)."""
        import torch
        self._model, _ = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            trust_repo=True,
            onnx=True,
        )
        logger.info("Silero VAD model loaded")

    def process_frame(self, pcm_bytes: bytes) -> bool:
        """
        Feed a single L16 PCM frame and return True if speech is active.

        Args:
            pcm_bytes: Raw 16-bit PCM bytes for one frame.

        Returns:
            True while user is speaking, False otherwise.
        """
        import torch

        if self._model is None:
            self.load_model()

        # Convert L16 bytes → float32 tensor normalised to [-1, 1]
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(samples)

        confidence: float = self._model(tensor, self.sample_rate).item()

        if confidence >= self.threshold:
            self._speech_count += 1
            self._silence_count = 0
            if self._speech_count >= self.min_speech_frames:
                self.is_speech = True
        else:
            self._silence_count += 1
            self._speech_count = 0
            if self._silence_count >= self.min_silence_frames:
                self.is_speech = False

        return self.is_speech

    def reset(self) -> None:
        self._speech_count = 0
        self._silence_count = 0
        self.is_speech = False


class AudioStreamHandler:
    """
    Manages one bidirectional WebSocket audio session.

    Responsibilities:
      1. Accept raw L16 PCM from the telephony WS.
      2. Run VAD on each frame.
      3. Forward speech frames to the STT service via an async queue.
      4. Accept synthesised TTS audio and stream it back to the caller.
      5. Handle barge-in by flushing the outbound TTS buffer.
    """

    def __init__(
        self,
        websocket: WebSocket,
        on_speech_start: Callable[[], Awaitable[None]] | None = None,
        on_speech_end: Callable[[bytes], Awaitable[None]] | None = None,
        on_barge_in: Callable[[], Awaitable[None]] | None = None,
        vad_threshold: float = 0.5,
    ) -> None:
        self.ws = websocket
        self._on_speech_start = on_speech_start
        self._on_speech_end = on_speech_end
        self._on_barge_in = on_barge_in

        self.vad = VADProcessor(sample_rate=SAMPLE_RATE, threshold=vad_threshold)
        self.call_sid: str = ""

        # Queues
        self._inbound_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._outbound_queue: asyncio.Queue[bytes] = asyncio.Queue()

        # Internal state
        self._speech_buffer = bytearray()
        self._is_playing_tts = False
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────

    async def accept(self) -> None:
        """Accept the WebSocket connection and begin processing."""
        await self.ws.accept()
        self._running = True
        logger.info("WebSocket accepted for call %s", self.call_sid)

    async def run(self) -> None:
        """
        Main loop — run as a task. Reads inbound audio,
        applies VAD, and dispatches events.
        """
        was_speaking = False
        try:
            while self._running:
                raw = await self.ws.receive_bytes()

                # Parse Twilio / Telnyx media message and extract PCM payload
                pcm_data = self._extract_pcm(raw)
                if pcm_data is None:
                    continue

                is_speaking = self.vad.process_frame(pcm_data)

                # ── Speech start edge ──────────────────────────
                if is_speaking and not was_speaking:
                    logger.debug("Speech start detected")
                    self._speech_buffer.clear()

                    # If TTS is playing → barge-in
                    if self._is_playing_tts:
                        await self._handle_barge_in()

                    if self._on_speech_start:
                        await self._on_speech_start()

                # ── Accumulate speech frames ───────────────────
                if is_speaking:
                    self._speech_buffer.extend(pcm_data)
                    await self._inbound_queue.put(pcm_data)

                # ── Speech end edge ────────────────────────────
                if not is_speaking and was_speaking:
                    logger.debug("Speech end detected — %d bytes buffered",
                                 len(self._speech_buffer))
                    if self._on_speech_end:
                        await self._on_speech_end(bytes(self._speech_buffer))
                    self._speech_buffer.clear()

                was_speaking = is_speaking

        except WebSocketDisconnect:
            logger.info("WebSocket disconnected for call %s", self.call_sid)
        finally:
            self._running = False

    # ── Outbound TTS streaming ────────────────────────────────────

    async def send_audio(self, pcm_chunk: bytes) -> None:
        """Send a TTS audio chunk back to the caller over WS."""
        if not self._running:
            return
        self._is_playing_tts = True
        await self.ws.send_bytes(self._wrap_pcm(pcm_chunk))

    async def finish_playback(self) -> None:
        """Signal that TTS playback is complete."""
        self._is_playing_tts = False

    async def stream_tts_audio(self, chunks: AsyncGenerator[bytes, None]) -> None:
        """Stream TTS chunks to the caller, respecting barge-in."""
        self._is_playing_tts = True
        try:
            async for chunk in chunks:
                if not self._is_playing_tts:
                    logger.debug("TTS stream interrupted by barge-in")
                    break
                await self.ws.send_bytes(self._wrap_pcm(chunk))
        finally:
            self._is_playing_tts = False

    # ── Inbound speech access (for STT service) ──────────────────

    async def get_speech_frame(self) -> bytes:
        """Get the next speech frame from the inbound queue."""
        return await self._inbound_queue.get()

    # ── Barge-in ──────────────────────────────────────────────────

    async def _handle_barge_in(self) -> None:
        """Interrupt outbound TTS playback when user starts speaking."""
        logger.info("Barge-in triggered — flushing TTS buffer")
        self._is_playing_tts = False

        # Drain server-side outbound queue (stops new chunks being sent)
        while not self._outbound_queue.empty():
            try:
                self._outbound_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # TODO (telephony): send Twilio/Telnyx a "clear" event here to discard
        # audio already buffered on the carrier side:
        #   await self.ws.send_json({"event": "clear", "streamSid": self.call_sid})
        # Without this, the caller hears a brief continuation after barge-in.

        if self._on_barge_in:
            await self._on_barge_in()

    # ── Cleanup ───────────────────────────────────────────────────

    async def close(self) -> None:
        """Gracefully close the WebSocket."""
        self._running = False
        self.vad.reset()
        try:
            await self.ws.close()
        except Exception:
            pass
        logger.info("AudioStreamHandler closed for call %s", self.call_sid)

    # ── Private helpers ───────────────────────────────────────────

    @staticmethod
    def _extract_pcm(raw: bytes) -> bytes | None:
        """
        Extract L16 PCM payload from the telephony provider's
        WebSocket message.

        Override or extend for Twilio vs Telnyx framing differences.
        For Twilio media-streams the payload arrives as base64 inside
        a JSON envelope — that parsing is done here.
        """
        import json
        import base64

        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Already raw PCM bytes (e.g. Telnyx binary mode)
            return raw if len(raw) == FRAME_SIZE * SAMPLE_WIDTH else None

        event = msg.get("event")
        if event == "media":
            payload = msg["media"].get("payload", "")
            return base64.b64decode(payload)
        if event == "start":
            logger.info("Stream started: %s", msg.get("start", {}).get("callSid"))
        if event == "stop":
            logger.info("Stream stopped")
        return None

    @staticmethod
    def _wrap_pcm(pcm: bytes) -> bytes:
        """
        Wrap outbound PCM into the format expected by the telephony WS.

        For Twilio this would be a JSON media message with base64 payload.
        """
        import json
        import base64

        return json.dumps({
            "event": "media",
            "media": {
                "payload": base64.b64encode(pcm).decode("ascii"),
            },
        }).encode()
