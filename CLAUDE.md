# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.


This is a framework of a call handling system. AI handles multiple calls that are made to a number. for now telephony is not added and it is a bare working version working on browser.

## Commands

```bash
# Setup
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# Run the dev server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Open the UI
# http://localhost:8000
```

**Required `.env` file** (never commit):
```
DEEPGRAM_API_KEY=...
GOOGLE_API_KEY=...
CARTESIA_API_KEY=...
```

## Architecture

This is a Voice AI system built around a "Sandwich" pipeline: **STT → LLM → TTS**.

### Two-layer design

**Layer 1 — Current working version** (`app/main.py`):  
A minimal FastAPI app with a single WebSocket endpoint (`/ws/audio`). The browser sends WebM/opus audio, the server transcribes it with Deepgram, generates a response with Gemini, synthesizes it with Cartesia, and sends WAV bytes back. Conversation history is kept per-session in memory.

**Layer 2 — Target architecture** (partially scaffolded, documented in `docs/`):  
Wraps Layer 1 with a LangGraph Supervisor graph and a telephony layer (Twilio/Telnyx). The graph deals only with text and state; audio/telephony is infrastructure that wraps around the graph.

### Key files

| File | Role |
|---|---|
| `app/main.py` | FastAPI app — current working WebSocket pipeline |
| `app/config.py` | Loads API keys from `.env` via `Settings` class |
| `app/services/stt_service.py` | Deepgram Nova-3 prerecorded transcription |
| `app/services/llm_service.py` | Gemini 2.0 Flash via LangChain, voice-optimised system prompt |
| `app/services/tts_service.py` | Cartesia Sonic-2 TTS → WAV bytes (PCM f32le, 44100 Hz) |
| `app/core/state.py` | `OrchestratorState` — the LangGraph state schema (future use) |
| `app/infrastructure/audio_stream_handler.py` | WebSocket handler for telephony L16 PCM + Silero VAD + barge-in |

### LangGraph target architecture (see `docs/langgraph_workflow.md`)

When the telephony layer is added, calls flow:
1. Twilio/Telnyx WebSocket → `AudioStreamHandler` (VAD, barge-in detection)
2. Speech PCM frames → `stt_node` → `orchestrator_node` (Supervisor LLM router)
3. Orchestrator picks a specialist agent (`greeting`, `identification`, `faq`, `booking`, `escalation`, `wrap_up`, `fallback`)
4. Agent response → `tts_node` → audio chunks → back through `AudioStreamHandler` → caller

The `OrchestratorState` in `app/core/state.py` tracks call phase (`GREETING → IDENTIFICATION → ROUTING → HANDLING → WRAP_UP → ENDED`), current agent, barge-in verdict, latency telemetry, and RAG context.

### Planned files not yet created

Per `docs/langgraph_workflow.md`, the full architecture expects:
- `app/api/routes_telephony.py` — Twilio/Telnyx webhook + WS endpoint
- `app/infrastructure/telephony_client.py` — SDK wrapper for call control
- `app/core/orchestrator.py` — LangGraph graph definition and `compile()`
- `app/core/agents/*.py` — one file per specialist agent

### Notable design decisions

- **Barge-in** is handled at the infrastructure layer (`AudioStreamHandler`), not inside the LangGraph graph. When VAD detects speech during TTS playback, `_is_playing_tts` is set to `False`, draining the outbound queue immediately.
- **`AudioStreamHandler._extract_pcm`** handles both Twilio (JSON envelope with base64 payload) and Telnyx (raw binary PCM) framing — extend this when adding telephony.
- **LLM responses are intentionally short** (1–3 sentences) because the system prompt is tuned for voice: no markdown, no lists.
- Services are swappable — all provider calls are isolated in `app/services/`; the graph never calls a provider directly.
