"""Application configuration — loads API keys from .env."""

import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    deepgram_api_key: str = os.environ["DEEPGRAM_API_KEY"]
    google_api_key: str = os.environ["GOOGLE_API_KEY"]
    cartesia_api_key: str = os.environ.get("CARTESIA_API_KEY", "")
    groq_api_key: str = os.environ["GROQ_API_KEY"]
    sarvam_api_key: str = os.environ.get("SARVAM_API_KEY", "")

    # Which TTS provider to use: "cartesia" or "sarvam"
    tts_provider: str = os.environ.get("TTS_PROVIDER", "cartesia")


settings = Settings()
