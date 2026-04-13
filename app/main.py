"""FastAPI app — serves the UI and handles the WebSocket voice pipeline."""

import asyncio
import json
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request

from app.services import stt_service, llm_service, tts_service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Voice Assistant")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.websocket("/ws/audio")
async def websocket_audio(websocket: WebSocket):
    await websocket.accept()
    logger.info("Client connected")

    history: list[dict] = []
    audio_queue: asyncio.Queue = asyncio.Queue()

    async def receive_from_browser() -> None:
        """Forward browser audio chunks and control messages into audio_queue."""
        try:
            while True:
                message = await websocket.receive()
                if "bytes" in message:
                    # Raw int16 PCM chunk from AudioWorklet
                    await audio_queue.put(message["bytes"])
                elif "text" in message:
                    msg = json.loads(message["text"])
                    if msg.get("type") == "end_of_speech":
                        # Button released — flush Deepgram buffer
                        await audio_queue.put(stt_service.FINALIZE)
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

            # TTS — stream raw PCM chunks
            try:
                async for audio_chunk in tts_service.stream_audio(response_text):
                    await websocket.send_bytes(audio_chunk)
            except Exception as e:
                logger.exception("TTS error")
                await websocket.send_text(json.dumps({"type": "error", "message": str(e)}))

            # Signal playback complete so frontend re-enables the button
            await websocket.send_text(json.dumps({"type": "tts_done"}))

    await asyncio.gather(receive_from_browser(), process_pipeline())
