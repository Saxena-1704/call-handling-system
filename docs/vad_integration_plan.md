# VAD Integration Plan — Unified Server-Side VAD

## Goal

Replace the current **push-to-talk** interaction with **always-on microphone + server-side Voice Activity Detection (VAD)**. The same VAD logic will power both the browser and future telephony (Twilio/Telnyx), so we build it once.

---

## Current State

| Component | How it works now |
|---|---|
| **Frontend** (`app/templates/index.html`) | Button hold → opens mic → streams int16 PCM at 16 kHz via AudioWorklet → releases button → sends `end_of_speech` signal |
| **Backend** (`app/main.py`) | WebSocket `/ws/audio` receives PCM chunks + `end_of_speech` control message → STT → LLM → TTS → sends audio back |
| **STT** (`app/services/stt_service.py`) | Deepgram WebSocket streaming. Uses `FINALIZE` sentinel (triggered by button release) to flush buffer. Also has `endpointing=300` and `utterance_end_ms=1000` for natural speech detection |
| **VAD** (`app/infrastructure/audio_stream_handler.py`) | `VADProcessor` (Silero VAD) + `AudioStreamHandler` (barge-in, speech buffering) already built, but hardcoded to 8 kHz telephony. **Not wired into the browser pipeline** |

---

## Key Design Decision: Server-Side VAD for Both Browser & Telephony

Instead of running VAD in the browser (client-side), we run it **on the server** using the existing `VADProcessor` (Silero). This means:

- **One VAD implementation** — tune thresholds in one place, works for browser and phone calls
- **Browser streams all audio** (including silence) to the server — slightly more bandwidth, but the server decides when speech starts/stops
- **`VADProcessor` needs one change** — make sample rate configurable (currently hardcoded to 8 kHz for Twilio, browser sends 16 kHz)

```
Browser/Phone → continuous audio → Server VAD → speech frames → Deepgram
                                             → silence → ignored
```

---

## What Needs to Change

### Phase 1: Make `VADProcessor` Sample-Rate Agnostic

**File:** `app/infrastructure/audio_stream_handler.py`

The `VADProcessor` and its constants are currently hardcoded to 8 kHz:

```python
SAMPLE_RATE = 8000
FRAME_DURATION_MS = 20
FRAME_SIZE = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 160 samples
```

Changes:
- Move `sample_rate` into `VADProcessor.__init__()` as a parameter.
- Compute `frame_size` dynamically from the sample rate.
- The Silero model itself supports both 8 kHz and 16 kHz — no model change needed.

```python
class VADProcessor:
    def __init__(self, sample_rate=16000, threshold=0.5, ...):
        self.sample_rate = sample_rate
        self.frame_size = sample_rate * FRAME_DURATION_MS // 1000
        ...
```

Usage:
- Browser sessions: `VADProcessor(sample_rate=16000)`
- Telephony sessions: `VADProcessor(sample_rate=8000)`

---

### Phase 2: Wire VAD into the Browser WebSocket Pipeline

**File:** `app/main.py`

Currently `websocket_audio()` has two tasks:
- `receive_from_browser()` — pushes raw audio + control messages into `audio_queue`
- `process_pipeline()` — consumes transcripts from Deepgram → LLM → TTS

We insert VAD between the WebSocket and the audio queue:

```
Browser → raw PCM → VADProcessor.process_frame()
                        ↓ speech detected?
                   YES: forward PCM to audio_queue → Deepgram
                   NO:  discard (don't send silence to Deepgram)

                        ↓ speech just ended?
                   YES: send FINALIZE to audio_queue (flush Deepgram buffer)
```

New flow inside `receive_from_browser()`:

```python
vad = VADProcessor(sample_rate=16000)
was_speaking = False

while True:
    pcm_bytes = await websocket.receive_bytes()
    is_speaking = vad.process_frame(pcm_bytes)

    if is_speaking:
        await audio_queue.put(pcm_bytes)

    # Speech just ended → flush Deepgram
    if was_speaking and not is_speaking:
        await audio_queue.put(FINALIZE)

    was_speaking = is_speaking
```

This replaces the old `end_of_speech` control message entirely. The button no longer decides when speech ends — the server VAD does.

---

### Phase 3: Frontend — Always-On Mic

**File:** `app/templates/index.html`

#### 3.1 Replace push-to-talk with mic toggle

- Remove button hold/release event listeners.
- Replace with a single **mic on/off toggle button**.
- On first click: request mic permission, open AudioContext, start streaming PCM continuously.
- The AudioWorklet keeps running and sends ALL audio frames (speech + silence) to the server.
- Remove the `end_of_speech` JSON message — the server handles this now.

#### 3.2 Server tells frontend about speech state

The server now knows when the user is speaking (via VAD). It sends state messages to the frontend:

```json
{"type": "vad_state", "speaking": true}
{"type": "vad_state", "speaking": false}
```

The frontend uses these to update the UI (listening indicator, waveform, etc.) instead of inferring state from button presses.

#### 3.3 Update UI states

Current states: `idle → listening → thinking → speaking`

New states:
```
idle (mic off)
  → ready (mic on, streaming, waiting for speech)
  → listening (server VAD detected speech)
  → thinking (waiting for LLM response)
  → speaking (playing TTS audio)
  → ready (back to waiting — NOT idle)
```

The button becomes a **mic toggle**. A visual indicator (pulsing ring, colour change) shows the mic is live and whether VAD is detecting speech.

---

### Phase 4: Barge-In (Interruption Handling)

#### 4.1 Server-side barge-in detection

**File:** `app/main.py`

The server already knows:
- Whether TTS audio is being sent (`is_playing_tts` flag)
- Whether the user is speaking (from VAD)

When VAD detects speech while TTS is playing:
1. Set `is_playing_tts = False` — stop sending TTS chunks.
2. Send `{"type": "barge_in"}` to the frontend.
3. Continue processing the new speech as a fresh utterance.

This is the same logic already in `AudioStreamHandler._handle_barge_in()` — we're replicating it in the browser pipeline.

#### 4.2 Frontend barge-in response

**File:** `app/templates/index.html`

On receiving `{"type": "barge_in"}`:
1. Stop current audio playback immediately (disconnect `BufferSource`, clear `audioQueue`).
2. Switch UI to "listening" state.

The frontend doesn't need its own barge-in detection — the server tells it when to stop.

---

### Phase 5: Tuning & Edge Cases

#### 5.1 Parameters to tune

| Parameter | Where | Start value | What it controls |
|---|---|---|---|
| VAD threshold | `VADProcessor` (server) | 0.5 | Confidence needed to trigger speech |
| Min speech duration | `VADProcessor` (server) | 250ms | Ignore very short sounds (coughs, clicks) |
| Min silence duration | `VADProcessor` (server) | 500ms | How long to wait before declaring "speech ended" |
| Deepgram `endpointing` | `stt_service.py` | 300ms | Server-side silence detection (backup) |
| Deepgram `utterance_end_ms` | `stt_service.py` | 1000ms | Max silence before forced finalize |

All VAD tuning is in one place on the server — same thresholds apply whether the audio comes from a browser or a phone call.

#### 5.2 Edge cases to handle

1. **Echo cancellation** — The mic picks up the bot's TTS audio from speakers. For browser, rely on `getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } })`. For telephony, the carrier handles echo cancellation. Barge-in works because the server VAD sees the user's voice on top of the echo — Silero is trained to handle this reasonably well.

2. **Frame size mismatch** — The browser AudioWorklet sends 128-sample chunks (at 16 kHz = 8ms). VAD expects 20ms frames (320 samples at 16 kHz). Buffer incoming chunks on the server and feed VAD in 20ms frames.

3. **Double-fire** — VAD might flicker at speech boundaries. The `min_speech_frames` and `min_silence_frames` counters in `VADProcessor` already handle this (debouncing built in).

4. **Background noise** — Noisy environments may trigger false positives. Raise VAD threshold to 0.6–0.7. Browser-side `noiseSuppression: true` helps before audio even reaches the server.

5. **Bandwidth** — Browser now sends audio continuously instead of only while button is held. At 16 kHz × 16-bit = 32 KB/s, this is negligible on any modern connection. For very constrained networks, we could add client-side silence detection as an optimisation later.

---

## Implementation Order

```
Step 1  Backend:  Make VADProcessor sample-rate configurable
Step 2  Backend:  Wire VAD into main.py WebSocket handler
Step 3  Backend:  Add frame buffering (128-sample chunks → 20ms frames)
Step 4  Frontend: Always-on mic, remove push-to-talk
Step 5  Frontend: Handle vad_state messages, update UI
Step 6  Test:     Basic hands-free conversation
Step 7  Backend:  Barge-in detection (VAD + is_playing_tts)
Step 8  Frontend: Handle barge_in message, stop playback
Step 9  Test:     Interruption handling
Step 10 Tune:     VAD thresholds, echo, frame buffering
```

## Files That Will Change

| File | Change |
|---|---|
| `app/infrastructure/audio_stream_handler.py` | Make `VADProcessor` sample-rate configurable, extract it so `main.py` can import it independently |
| `app/main.py` | Major — add VAD processing, frame buffering, barge-in detection, new WebSocket messages |
| `app/templates/index.html` | Major — always-on mic, remove button hold logic, handle `vad_state` and `barge_in` messages |
| `app/services/stt_service.py` | Minor — possibly tune endpointing values |

## Dependencies

| Dependency | Where | Why |
|---|---|---|
| `torch` | Server (Python) | Already a dependency — Silero VAD uses it |
| `silero-vad` | Server (Python) | Downloaded via `torch.hub` on first run (already in `VADProcessor`) |

No new frontend dependencies. No new Python packages — Silero VAD is already used by `VADProcessor`.
