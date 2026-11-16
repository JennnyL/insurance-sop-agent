import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=False)


def api_key():
    path = os.getenv("MODEL_API_KEY_FILE")
    return (
        Path(path).read_text().strip()
        if path
        else (os.getenv("MODEL_API_KEY") or os.getenv("OPENAI_API_KEY", ""))
    )


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", str(ROOT / "data"))))
    model_mode: str = field(default_factory=lambda: os.getenv("MODEL_MODE", "auto"))
    model_name: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "gpt-4.1-mini"))
    model_base_url: str = field(
        default_factory=lambda: os.getenv("MODEL_BASE_URL", "https://api.openai.com/v1")
    )
    model_api_key: str = field(default_factory=api_key, repr=False)
    support_mode: str = "mock"  # Asynchronous simulated handling; no online staff service.
    demo_as_of: str = field(default_factory=lambda: os.getenv("DEMO_AS_OF_DATE", ""))
    secure_cookie: bool = field(default_factory=lambda: os.getenv("COOKIE_SECURE", "false").lower() == "true")
    auth_idle_seconds: int = 1800
    auth_max_seconds: int = 7200
    session_seconds: int = 86400
    offer_seconds: int = 600
    consent_timeout_seconds: int = 120
    consent_scenario: str = "default"
    turn_timeout: float = 60
    lease_seconds: int = 75
    email_delay: float = 3
    max_model_concurrency: int = 4
    # Per-conversation ceiling on physical model calls. A turn uses about 2-5 calls, so the
    # default covers 60+ turns; beyond it replies use the built-in scripted assistant.
    model_max_calls_per_conversation: int = field(
        default_factory=lambda: int(os.getenv("MODEL_MAX_CALLS_PER_CONVERSATION", "300"))
    )

    @property
    def mode(self):
        if self.model_mode not in {"auto", "offline", "openai"}:
            raise ValueError("MODEL_MODE must be auto, offline, or openai")
        if self.model_mode == "openai" and not self.model_api_key:
            raise ValueError("MODEL_API_KEY or OPENAI_API_KEY is required for openai mode")
        return (
            "openai"
            if self.model_mode == "openai" or (self.model_mode == "auto" and self.model_api_key)
            else "offline"
        )

    @property
    def business_date(self):
        return date.fromisoformat(self.demo_as_of) if self.demo_as_of else date.today()
