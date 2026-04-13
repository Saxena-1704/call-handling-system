# LangGraph Workflow — Voice AI Orchestrator

## Overview

The graph follows a **Supervisor pattern**: a central Orchestrator node reads the current
state and decides which specialist agent to invoke next. Each agent does one job and
returns control to the Orchestrator after it finishes.

**The LangGraph graph only deals with text and state — it never touches audio or
telephony directly.** The telephony layer is infrastructure that wraps around the graph.

---

## Architecture Layers

```
┌─────────────────────────────────────────────────────────────────┐
│  TELEPHONY LAYER  (outside the graph)                           │
│                                                                 │
│  Twilio / Telnyx                                                │
│       │  WebSocket — bidirectional L16 PCM audio               │
│       ▼                                                         │
│  FastAPI WebSocket endpoint   (app/api/routes_telephony.py)     │
│  • accepts the call connection                                  │
│  • handles Twilio/Telnyx webhook events (call start/end)        │
│  • owns the WebSocket for the lifetime of the call              │
│       │                                                         │
│       ▼                                                         │
│  AudioStreamHandler           (app/infrastructure/)             │
│  • VAD — detects when caller starts/stops speaking              │
│  • barge-in detection — interrupts TTS if caller speaks         │
│  • inbound queue — speech PCM frames → STT service              │
│  • outbound send — TTS audio chunks → back to caller            │
└──────────────────────┬──────────────────────────┬──────────────┘
                       │ transcript (text)         │ audio chunks
                       ▼                           ▲
┌─────────────────────────────────────────────────────────────────┐
│  LANGGRAPH GRAPH  (text in, text out)                           │
│                                                                 │
│   stt_node ──► orchestrator_node ──► agent ──► tts_node        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

The FastAPI endpoint is the **glue** — it creates an `AudioStreamHandler`, starts
the LangGraph run, and wires the two together. When `stt_node` produces a transcript
it came from the handler's inbound queue. When `tts_node` produces audio it gets
sent back through the handler's `send_audio()`.

---

## High-Level Flow (Detailed)

```
Twilio / Telnyx (WebSocket)
        │  L16 PCM audio
        ▼
FastAPI WS endpoint  ──creates──►  AudioStreamHandler
        │                                  │
        │                        VAD per 20ms frame
        │                                  │
        │                    speech detected? ──no──► discard frame
        │                                  │
        │                                 yes
        │                                  │
        │                         push to STT queue
        │                                  │
        ▼                                  ▼
  ┌─────────────┐
  │  STT Node   │  ← pulls from queue, sends to STT service (streaming)
  └──────┬──────┘
         │ transcript
         ▼
  ┌─────────────────┐
  │  ORCHESTRATOR   │  ← reads state, classifies intent, routes to agent
  │   (Supervisor)  │
  └──────┬──────────┘
         │
         ├──────────────────────────────────────────────┐
         │                        │                     │
         ▼                        ▼                     ▼
  ┌─────────────┐        ┌──────────────┐      ┌──────────────┐
  │  Agent A    │        │   Agent B    │      │  Agent N...  │
  │ (e.g. FAQ)  │        │ (e.g. Book) │      │  (fallback)  │
  └──────┬──────┘        └──────┬───────┘      └──────┬───────┘
         │                      │                     │
         └──────────────────────┴─────────────────────┘
                                │
                         response text
                                │
                                ▼
                       ┌────────────────┐
                       │ Phonetic Norm  │  ← normalise IDs, numbers, codes
                       └───────┬────────┘
                               │
                               ▼
                       ┌────────────────┐
                       │   TTS Node     │  ← synthesise speech (streaming)
                       └───────┬────────┘
                               │
                               ▼
                    Audio back to Caller (WebSocket)
```

---

## Node Definitions

### Entry & Exit

| Node | Role |
|---|---|
| `__start__` | Graph entry — triggered when a call connects |
| `__end__` | Graph exit — triggered when call ends or Orchestrator says DONE |

---

### Core Pipeline Nodes

#### `stt_node`
- **Input**: raw PCM bytes from `AudioStreamHandler` (pushed into state)
- **Action**: sends audio to STT service, waits for final transcript
- **Output**: writes `current_transcript` to state
- **Next**: always → `orchestrator_node`

#### `orchestrator_node`  _(the Supervisor)_
- **Input**: full conversation `messages` + `current_transcript` + `caller` profile + `phase`
- **Action**: calls the LLM with a router prompt; classifies the user's intent
- **Output**: sets `current_agent` (which agent to call next) and `phase`
- **Next**: conditional edge → whichever agent was selected, or `tts_node` if already has a response, or `__end__` if call should end

#### `tts_node`
- **Input**: final response text from whichever agent ran
- **Action**: sends text to TTS service, streams audio chunks back via `AudioStreamHandler`
- **Output**: updates `tts_latency_ms`, sets `tts_interrupted` flag if barge-in occurred
- **Next**: always → `stt_node` (listen for next utterance) or `__end__`

---

### Specialist Agent Nodes

These are placeholders — add or rename as the product requirements clarify.

#### `greeting_agent`
- Handles the opening of the call
- Introduces the AI, asks how it can help
- Transitions phase: `GREETING → IDENTIFICATION`

#### `identification_agent`
- Collects caller ID, account number, or other verification info
- May query the database for caller profile
- Transitions phase: `IDENTIFICATION → ROUTING`

#### `faq_agent`
- Answers common/scripted questions from a knowledge base
- Uses RAG to retrieve relevant documents
- Stays in phase: `HANDLING`

#### `booking_agent`
- Handles appointment scheduling, rescheduling, cancellations
- May call external calendar APIs
- Stays in phase: `HANDLING`

#### `escalation_agent`
- Detects frustration, complexity beyond AI capability
- Initiates warm transfer to a human agent
- Transitions phase: `HANDLING → WRAP_UP`

#### `wrap_up_agent`
- Confirms what was done, asks if anything else is needed
- Ends the call gracefully
- Transitions phase: `WRAP_UP → ENDED`

#### `fallback_agent`
- Catches anything the Orchestrator cannot confidently classify
- Asks a clarifying question and re-routes
- Stays in phase: `HANDLING`

---

## Conditional Routing Logic (Orchestrator)

```
orchestrator_node
    │
    ├── intent == "greeting"          → greeting_agent
    ├── intent == "identify"          → identification_agent
    ├── intent == "faq"               → faq_agent
    ├── intent == "booking"           → booking_agent
    ├── intent == "escalate"          → escalation_agent
    ├── intent == "wrap_up"           → wrap_up_agent
    ├── intent == "unknown"           → fallback_agent
    └── call_phase == ENDED           → __end__
```

The Orchestrator uses a **structured output** call to the LLM — it returns a JSON object
like `{ "next_agent": "booking_agent", "reason": "user asked to reschedule" }` rather than
free text. This makes routing deterministic and traceable.

---

## Barge-In Handling (Outside the Graph)

Barge-in is handled **at the infrastructure layer** (AudioStreamHandler), not inside the
graph itself. When VAD detects genuine speech while TTS is playing:

1. `AudioStreamHandler` sets `tts_interrupted = True` in state
2. TTS streaming stops immediately
3. The current `stt_node` continues receiving the new utterance
4. Graph resumes from `orchestrator_node` with the new transcript

---

## Parallel Async Work (Outside Nodes)

Some tasks run **in parallel** to the main graph loop to save latency:

| Task | When triggered | What it does |
|---|---|---|
| **Predictive RAG** | On partial STT transcript | Pre-fetches documents before utterance ends |
| **Caller lookup** | On call connect | Pulls caller profile from DB in the background |
| **TTS pre-warm** | When LLM starts streaming | Sends first tokens to TTS before full response is ready |

---

## State Transitions (Call Phases)

```
GREETING → IDENTIFICATION → ROUTING → HANDLING → WRAP_UP → ENDED
                                          ▲              │
                                          └── (loop if   │
                                               more       │
                                               topics)    │
                                                          ▼
                                                        __end__
```

---

## Full Graph Definition (Pseudocode)

```python
from langgraph.graph import StateGraph, START, END
from app.core.state import OrchestratorState

graph = StateGraph(OrchestratorState)

# Register nodes
graph.add_node("stt_node",            stt_node)
graph.add_node("orchestrator_node",   orchestrator_node)
graph.add_node("greeting_agent",      greeting_agent)
graph.add_node("identification_agent",identification_agent)
graph.add_node("faq_agent",           faq_agent)
graph.add_node("booking_agent",       booking_agent)
graph.add_node("escalation_agent",    escalation_agent)
graph.add_node("wrap_up_agent",       wrap_up_agent)
graph.add_node("fallback_agent",      fallback_agent)
graph.add_node("tts_node",            tts_node)

# Fixed edges
graph.add_edge(START,                 "stt_node")
graph.add_edge("stt_node",            "orchestrator_node")

# Conditional routing from Orchestrator → Agent
graph.add_conditional_edges(
    "orchestrator_node",
    route_to_agent,          # function that reads state.current_agent
    {
        "greeting_agent":       "greeting_agent",
        "identification_agent": "identification_agent",
        "faq_agent":            "faq_agent",
        "booking_agent":        "booking_agent",
        "escalation_agent":     "escalation_agent",
        "wrap_up_agent":        "wrap_up_agent",
        "fallback_agent":       "fallback_agent",
        "tts_node":             "tts_node",    # already has response, skip agent
        END:                    END,
    }
)

# All agents → TTS
for agent in [
    "greeting_agent", "identification_agent", "faq_agent",
    "booking_agent", "escalation_agent", "wrap_up_agent", "fallback_agent"
]:
    graph.add_edge(agent, "tts_node")

# TTS → back to listening, or end
graph.add_conditional_edges(
    "tts_node",
    route_after_tts,         # checks if phase == ENDED
    {
        "continue": "stt_node",
        "end":      END,
    }
)

app = graph.compile()
```

---

## Technology Slots (To Be Decided)

| Slot | Placeholder | Options to evaluate |
|---|---|---|
| STT | `stt_service` | Deepgram, Azure Speech, Whisper |
| LLM (Orchestrator) | `llm_router` | Claude, GPT-4o, Gemini |
| LLM (Agents) | `llm_agent` | Same or smaller/cheaper model |
| TTS | `tts_service` | ElevenLabs, Cartesia, Azure Neural |
| Telephony | `telephony_client` | Twilio, Telnyx |
| Vector DB (RAG) | `vector_store` | Pinecone, Qdrant, pgvector |

All service calls are wrapped behind interfaces — swapping providers requires changing
only the implementation file, not the graph.

---

## Files This Maps To

```
app/
├── api/
│   └── routes_telephony.py    ← FastAPI WS endpoint, Twilio/Telnyx webhooks
│                                 (TELEPHONY LAYER — entry point for every call)
│
├── infrastructure/
│   ├── audio_stream_handler.py ← WebSocket + VAD + barge-in (already built)
│   └── telephony_client.py    ← Twilio / Telnyx SDK wrapper (call control:
│                                 answer, hangup, hold, transfer)
│
├── core/
│   ├── orchestrator.py        ← graph definition + compile()
│   ├── state.py               ← OrchestratorState schema (already built)
│   └── agents/
│       ├── greeting.py
│       ├── identification.py
│       ├── faq.py
│       ├── booking.py
│       ├── escalation.py
│       ├── wrap_up.py
│       └── fallback.py
│
└── services/
    ├── stt_service.py         ← STT interface + implementation
    ├── tts_service.py         ← TTS interface + implementation
    └── llm_service.py         ← LLM call wrapper
```

### How a call flows through these files

```
1. Twilio hits POST /webhook/call-status  →  routes_telephony.py
   (call connected event, Twilio sends TwiML back to open media stream)

2. Twilio opens WebSocket to GET /ws/audio/{call_sid}  →  routes_telephony.py
   (endpoint creates AudioStreamHandler, starts LangGraph run)

3. Audio frames arrive on the WS  →  AudioStreamHandler (VAD, barge-in)

4. Speech frames enqueued  →  stt_node pulls them, sends to STT service

5. Transcript ready  →  orchestrator_node routes to correct agent

6. Agent generates response text  →  tts_node sends to TTS service

7. TTS audio chunks stream back  →  AudioStreamHandler.send_audio()  →  WebSocket  →  Twilio  →  Caller

8. Caller hangs up  →  Twilio hits POST /webhook/call-status (completed)
   →  routes_telephony.py tears down handler + graph run
```
