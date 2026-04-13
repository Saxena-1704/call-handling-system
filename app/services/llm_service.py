"""LLM service using Groq via LangChain."""

import re
from typing import AsyncIterator

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from app.config import settings

_SYSTEM_PROMPT = (
    "You are a helpful voice assistant. "
    "Keep responses concise and conversational — 1 to 3 sentences max. "
    "You are speaking to someone on a call, not writing text, "
    "so avoid markdown, bullet points, or lists."
)

_llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    api_key=settings.groq_api_key,
    temperature=0.7,
)


async def chat(user_message: str, history: list[dict]) -> str:
    """
    Send a message to Gemini and return the response text.

    Args:
        user_message: The latest user utterance (transcript).
        history: Conversation history as list of {"role": "user"|"assistant", "content": str}.

    Returns:
        Assistant response text.
    """
    messages = [SystemMessage(content=_SYSTEM_PROMPT)]

    for turn in history:
        if turn["role"] == "user":
            messages.append(HumanMessage(content=turn["content"]))
        else:
            messages.append(AIMessage(content=turn["content"]))

    messages.append(HumanMessage(content=user_message))

    response = await _llm.ainvoke(messages)
    return response.content.strip()


async def stream_sentences(user_message: str, history: list[dict]) -> AsyncIterator[str]:
    """Stream LLM response one sentence at a time."""
    messages = [SystemMessage(content=_SYSTEM_PROMPT)]
    for turn in history:
        if turn["role"] == "user":
            messages.append(HumanMessage(content=turn["content"]))
        else:
            messages.append(AIMessage(content=turn["content"]))
    messages.append(HumanMessage(content=user_message))

    buffer = ""
    async for chunk in _llm.astream(messages):
        token = chunk.content
        if not token:
            continue
        buffer += token
        # Yield complete sentences as they accumulate
        parts = re.split(r'(?<=[.!?])\s+', buffer)
        for sentence in parts[:-1]:
            sentence = sentence.strip()
            if sentence:
                yield sentence
        buffer = parts[-1]

    if buffer.strip():
        yield buffer.strip()
