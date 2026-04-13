"""LangGraph State schema for the Voice AI orchestrator."""

from __future__ import annotations

import enum
from typing import Annotated

from langgraph.graph import MessagesState
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, Field
from operator import add


class CallPhase(str, enum.Enum):
    GREETING = "greeting"
    IDENTIFICATION = "identification"
    ROUTING = "routing"
    HANDLING = "handling"
    WRAP_UP = "wrap_up"
    ENDED = "ended"


class AgentType(str, enum.Enum):
    SUPERVISOR = "supervisor"
    TECHNICAL_SUPPORT = "technical_support"
    SCHEDULING = "scheduling"
    FALLBACK = "fallback"


class BargeInVerdict(str, enum.Enum):
    GENUINE_INTERRUPT = "genuine_interrupt"
    BACKCHANNEL = "backchannel"        # e.g. "uh-huh", "yeah"
    BACKGROUND_NOISE = "background_noise"
    SILENCE = "silence"


class CallerProfile(BaseModel):
    """Identified caller metadata."""
    caller_id: str | None = None
    name: str | None = None
    account_id: str | None = None
    language: str = "en-US"
    sentiment: float = 0.0  # -1.0 (angry) to 1.0 (happy)


class RAGContext(BaseModel):
    """Prefetched context from Predictive RAG."""
    documents: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    predicted_intent: str | None = None


class OrchestratorState(MessagesState):
    """
    Central state flowing through the LangGraph graph.

    Extends MessagesState so `messages` (list[BaseMessage]) is built-in
    with automatic append-reducer semantics.
    """

    # ── Call metadata ──────────────────────────────────────────────
    call_sid: str = ""                          # Telephony call ID
    caller: CallerProfile = Field(default_factory=CallerProfile)
    phase: CallPhase = CallPhase.GREETING

    # ── Routing ────────────────────────────────────────────────────
    current_agent: AgentType = AgentType.SUPERVISOR
    handoff_reason: str = ""

    # ── Audio pipeline state ───────────────────────────────────────
    is_user_speaking: bool = False
    barge_in_verdict: BargeInVerdict = BargeInVerdict.SILENCE
    current_transcript: str = ""               # Running STT buffer
    tts_interrupted: bool = False              # True if TTS was cut mid-stream

    # ── Predictive RAG ─────────────────────────────────────────────
    rag_context: RAGContext = Field(default_factory=RAGContext)

    # ── Latency telemetry (ms) ─────────────────────────────────────
    stt_latency_ms: float = 0.0
    llm_latency_ms: float = 0.0
    tts_latency_ms: float = 0.0

    # ── Turn tracking (append-only via reducer) ────────────────────
    turn_transcripts: Annotated[list[str], add] = Field(default_factory=list)
