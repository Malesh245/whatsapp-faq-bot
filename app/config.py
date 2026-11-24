"""Settings loaded from environment variables (.env in development)."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # LLM (any OpenAI-compatible API: OpenAI, Gemini, Groq, Ollama, ...)
    llm_api_key: str = ""
    llm_base_url: str | None = None
    llm_model: str = "gpt-4o-mini"

    # Storage and business data
    database_path: str = str(BASE_DIR / "data" / "bot.db")
    faq_path: str = str(BASE_DIR / "data" / "faq.md")
    history_limit: int = 10  # how many recent messages the LLM sees

    # WhatsApp Cloud API (all optional for local simulation)
    whatsapp_verify_token: str = "change-me"
    whatsapp_app_secret: str = ""        # if set, X-Hub-Signature-256 is verified
    whatsapp_access_token: str = ""      # if set, replies are really sent
    whatsapp_phone_number_id: str = ""
    graph_api_version: str = "v21.0"

    # Protects the admin endpoints (/handoffs, /leads, ...) if set
    admin_api_key: str = ""


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / ".env")
    env = os.getenv
    return Settings(
        llm_api_key=env("LLM_API_KEY", ""),
        llm_base_url=env("LLM_BASE_URL") or None,
        llm_model=env("LLM_MODEL", "gpt-4o-mini"),
        database_path=env("DATABASE_PATH", Settings.database_path),
        faq_path=env("FAQ_PATH", Settings.faq_path),
        history_limit=int(env("HISTORY_LIMIT", "10")),
        whatsapp_verify_token=env("WHATSAPP_VERIFY_TOKEN", "change-me"),
        whatsapp_app_secret=env("WHATSAPP_APP_SECRET", ""),
        whatsapp_access_token=env("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_phone_number_id=env("WHATSAPP_PHONE_NUMBER_ID", ""),
        graph_api_version=env("GRAPH_API_VERSION", "v21.0"),
        admin_api_key=env("ADMIN_API_KEY", ""),
    )
