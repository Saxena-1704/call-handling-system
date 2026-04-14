"""FastAPI app — serves the UI and handles the WebSocket voice pipeline."""

import asyncio
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from app.services import stt_service, llm_service, tts_service
from app.infrastructure.audio_stream_handler import VADProcessor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Voice Assistant")
templates = Jinja2Templates(directory="app/templates")

# Browser audio constants
BROWSER_SAMPLE_RATE = 16000
VAD_FRAME_MS = 32
VAD_FRAME_SAMPLES = BROWSER_SAMPLE_RATE * VAD_FRAME_MS // 1000   # 512 samples (Silero minimum at 16 kHz)
VAD_FRAME_BYTES = VAD_FRAME_SAMPLES * 2                           # 1024 bytes (int16)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.websocket("/ws/audio")
async def websocket_audio(websocket: WebSocket):
    await websocket.accept()
    logger.info("Client connected")

    history: list[dict] = []
    audio_queue: asyncio.Queue = asyncio.Queue()

    # Shared mutable state between the two coroutines
    state = {
        "is_playing_tts": False,
        "was_speaking": False,
        "tts_gen": 0,        # increments each TTS session; used to discard stale playback_ended
    }

    vad = VADProcessor(sample_rate=BROWSER_SAMPLE_RATE)
    # Load the Silero model in a thread pool — torch.hub.load() is blocking
    # and must not run on the async event loop
    await asyncio.to_thread(vad.load_model)
    frame_buffer = bytearray()

    # One Cartesia WebSocket for the entire browser session.
    # Re-opening the socket every utterance hits the connection rate limit.
    tts_session_ctx = tts_service.session()
    tts = await tts_session_ctx.__aenter__()

    async def receive_from_browser() -> None:
        """
        Read raw int16 PCM chunks from the browser, buffer them into
        20 ms VAD frames, run Silero VAD, and forward speech frames to
        the STT queue.  Barge-in is detected here too.
        """
        try:
            while True:
                message = await websocket.receive()

                if "bytes" not in message:
                    # Handle control messages from browser
                    if "text" in message:
                        try:
                            msg = json.loads(message["text"])
                            if msg.get("type") == "playback_ended":
                                gen = msg.get("gen", -1)
                                if gen == state["tts_gen"]:
                                    logger.info("Browser playback ended (gen=%d) — is_playing_tts=False", gen)
                                    state["is_playing_tts"] = False
                                else:
                                    logger.info("Ignoring stale playback_ended gen=%d (current=%d)", gen, state["tts_gen"])
                        except Exception:
                            pass
                    continue

                frame_buffer.extend(message["bytes"])

                # Process as many complete 20 ms frames as are available
                while len(frame_buffer) >= VAD_FRAME_BYTES:
                    frame = bytes(frame_buffer[:VAD_FRAME_BYTES])
                    del frame_buffer[:VAD_FRAME_BYTES]

                    is_speaking = vad.process_frame(frame)
                    was_speaking = state["was_speaking"]

                    # Notify frontend whenever VAD state changes
                    if is_speaking != was_speaking:
                        await websocket.send_text(json.dumps({
                            "type": "vad_state",
                            "speaking": is_speaking,
                        }))

                    # Barge-in: user speaks while TTS is playing
                    if is_speaking and state["is_playing_tts"]:
                        logger.info("BARGE-IN fired — is_playing_tts was True")
                        state["is_playing_tts"] = False
                        await websocket.send_text(json.dumps({"type": "barge_in"}))
                    elif is_speaking and not state["is_playing_tts"]:
                        logger.info("VAD: speech detected but is_playing_tts=False (no barge-in)")

                    # Forward speech frames to Deepgram
                    if is_speaking:
                        await audio_queue.put(frame)

                    # Speech just ended → flush Deepgram buffer
                    if was_speaking and not is_speaking:
                        await audio_queue.put(stt_service.FINALIZE)

                    state["was_speaking"] = is_speaking

        except WebSocketDisconnect:
            logger.info("Client disconnected")
        finally:
            await audio_queue.put(stt_service.CLOSE)

    async def process_pipeline() -> None:
        """Consume speech-final transcripts → LLM → TTS → stream audio back."""
        async for transcript in stt_service.stream_transcribe(audio_queue):
            logger.info("Transcript: %r", transcript)

            # Show transcript in UI
            await websocket.send_text(json.dumps({"type": "transcript", "text": transcript}))

            # LLM
            try:
                response_text = await llm_service.chat(transcript, history)
            except Exception as e:
                logger.exception("LLM error")
                await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
                continue
            logger.info("Response: %r", response_text)

            await websocket.send_text(json.dumps({"type": "response_text", "text": response_text}))
            history.append({"role": "user", "content": transcript})
            history.append({"role": "assistant", "content": response_text})

            # TTS — stream raw PCM chunks, stop on barge-in
            state["tts_gen"] += 1
            tts_gen = state["tts_gen"]
            logger.info("TTS starting — gen=%d, setting is_playing_tts=True", tts_gen)
            state["is_playing_tts"] = True
            tts_ok = True
            try:
                async for audio_chunk in tts.stream(response_text):
                    if not state["is_playing_tts"]:
                        logger.info("TTS stream cut short by barge-in")
                        break
                    await websocket.send_bytes(audio_chunk)
                    # Yield to the event loop so receive_from_browser can process
                    # incoming VAD frames. Without this, Cartesia chunks arrive
                    # faster than asyncio switches tasks and barge-in is never detected.
                    await asyncio.sleep(0)
            except Exception as e:
                logger.exception("TTS error")
                tts_ok = False
                state["is_playing_tts"] = False  # on error, don't wait for browser
                await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))
                continue  # skip tts_done — browser never started playback

            if tts_ok:
                # Tell browser all chunks have been sent.
                # is_playing_tts stays True until browser sends playback_ended with matching gen.
                await websocket.send_text(json.dumps({"type": "tts_done", "gen": tts_gen}))

    try:
        await asyncio.gather(receive_from_browser(), process_pipeline())
    finally:
        await tts_session_ctx.__aexit__(None, None, None)
