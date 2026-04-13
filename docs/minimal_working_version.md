# Minimal Working Version — Build Plan

## Goal

A localhost voice chat app: speak into browser mic, hear an AI response back. This validates the full STT → LLM → TTS pipeline before adding telephony (Twilio) later.

---

## Architecture

```
Browser (microphone)
    │  audio via WebSocket
    ▼
FastAPI WebSocket endpoint (/ws/audio)
    │
    ▼
Deepgram STT (streaming) → transcript text
    │
    ▼
Gemini LLM (Google AI Studio) → response text
    │
    ▼
Cartesia TTS → audio bytes
    │
    ▼
WebSocket → Browser (plays audio through speaker)
```

---

## API Keys Required

| Service | Purpose | Get it from |
|---|---|---|
| Deepgram | Speech-to-Text | https://console.deepgram.com (free tier) |
| Google AI Studio | Gemini LLM | https://aistudio.google.com/apikey |
| Cartesia | Text-to-Speech | https://play.cartesia.ai (free tier) |

Store in `.env` file (never commit):

```env
DEEPGRAM_API_KEY=your_key
GOOGLE_API_KEY=your_key
CARTESIA_API_KEY=your_key
```

---

## File Structure

```
app/
├── main.py                  # FastAPI app, WebSocket endpoint, serves UI
├── config.py                # Load env vars / API keys
├── services/
│   ├── stt_service.py       # Deepgram streaming STT
│   ├── llm_service.py       # Gemini chat (langchain + google-genai)
│   └── tts_service.py       # Cartesia TTS
└── templates/
    └── index.html           # Browser mic capture + audio playback UI
.env                         # API keys (gitignored)
requirements.txt             # Dependencies
```

---

## Build Steps

### Step 1: Project Setup

- [ ] Update `requirements.txt` with actual dependencies:
  - `fastapi`, `uvicorn[standard]`, `python-dotenv`
  - `deepgram-sdk`
  - `langchain`, `langchain-google-genai`
  - `cartesia`
  - `jinja2` (for HTML template serving)
- [ ] Create `.env` file with placeholder keys
- [ ] Create `app/config.py` — load env vars using `python-dotenv`, expose as a settings object
- [ ] Add `.env` to `.gitignore`

### Step 2: STT Service (Deepgram)

- [ ] Create `app/services/stt_service.py`
- [ ] Implement `transcribe(audio_bytes: bytes) -> str`
  - Takes raw audio bytes (WebM/opus from browser or PCM)
  - Sends to Deepgram's prerecorded or streaming API
  - Returns transcript text
- [ ] For v1, use **prerecorded** (simpler) — send complete utterance after silence detection
  - Browser handles silence detection via simple volume threshold
  - Sends complete audio chunk when user stops speaking
- [ ] Test standalone with a sample audio file

### Step 3: LLM Service (Gemini via LangChain)

- [ ] Create `app/services/llm_service.py`
- [ ] Implement `chat(user_message: str, history: list) -> str`
  - Uses `langchain-google-genai` with `ChatGoogleGenerativeAI`
  - Model: `gemini-2.0-flash` (fast, cheap)
  - System prompt: "You are a helpful voice assistant. Keep responses concise and conversational — 1-3 sentences max. You are speaking to someone on a call, not writing text."
  - Maintains conversation history per session (in-memory list)
- [ ] Test standalone with a text prompt

### Step 4: TTS Service (Cartesia)

- [ ] Create `app/services/tts_service.py`
- [ ] Implement `synthesize(text: str) -> bytes`
  - Uses Cartesia Python SDK
  - Returns raw audio bytes (PCM or WAV)
  - Pick a default voice ID from Cartesia's voice library
  - Output format: PCM 16-bit 24kHz (or whatever the browser can play easily — WAV is safest)
- [ ] Test standalone with a sample text string

### Step 5: Browser UI

- [ ] Create `app/templates/index.html`
- [ ] Minimal UI: a single "Hold to talk" button (or auto voice activity detection)
- [ ] JavaScript:
  - Open WebSocket to `ws://localhost:8000/ws/audio`
  - Capture mic audio using `MediaRecorder` API (output WebM/opus)
  - Simple silence detection: stop recording after N ms of silence, send audio blob
  - Receive audio response from server, play it using `AudioContext` or `<audio>` element
  - Show status: "Listening...", "Thinking...", "Speaking..."
- [ ] Keep it ugly but functional — no CSS framework needed

### Step 6: FastAPI Server + WebSocket Glue

- [ ] Create `app/main.py`
- [ ] `GET /` — serve `index.html`
- [ ] `WebSocket /ws/audio` — main endpoint:
  1. Accept connection
  2. Create per-session conversation history (list of messages)
  3. Loop:
     a. Receive audio bytes from browser
     b. Call `stt_service.transcribe(audio)` → transcript
     c. Call `llm_service.chat(transcript, history)` → response text
     d. Call `tts_service.synthesize(response)` → audio bytes
     e. Send audio bytes back to browser
     f. Append user message + assistant response to history
  4. On disconnect: clean up session
- [ ] Run with: `uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`

### Step 7: Integration Test

- [ ] Start server, open `http://localhost:8000` in browser
- [ ] Speak a question → verify you hear a spoken response
- [ ] Test multi-turn: ask follow-up questions, verify context is maintained
- [ ] Check latency — should be under 3-5 seconds end-to-end for v1

---

## Implementation Order

```
Step 1 (setup) → Step 3 (LLM, easiest to test) → Step 2 (STT) → Step 4 (TTS) → Step 5 (UI) → Step 6 (glue) → Step 7 (test)
```

Build and test each service independently before wiring them together.

---

## What This Version Does NOT Include

- No Twilio / phone calls (added next)
- No LangGraph orchestrator (direct function calls for now)
- No multi-agent routing
- No VAD on server side (browser handles it)
- No barge-in
- No RAG
- No streaming STT/TTS (full request-response for simplicity)
- No authentication or session persistence

---

## Next Steps After This Works

1. **Add Twilio** — sign up, get a number, add webhook + media stream WS endpoint
2. **Add LangGraph** — replace direct function calls with a simple graph (stt → llm → tts)
3. **Add streaming** — stream STT and TTS for lower latency
4. **Add server-side VAD** — use Silero VAD (already scaffolded in `audio_stream_handler.py`)
5. **Add multi-agent routing** — supervisor pattern from the design doc
