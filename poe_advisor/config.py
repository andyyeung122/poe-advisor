"""Settings loaded from environment variables / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Claude
    model: str = field(default_factory=lambda: os.getenv("POE_ADVISOR_MODEL", "claude-opus-5-5"))
    effort: str = field(default_factory=lambda: os.getenv("POE_ADVISOR_EFFORT", "medium"))
    web_search: bool = field(default_factory=lambda: _bool("POE_ADVISOR_WEB_SEARCH", True))
    refusal_fallbacks: bool = field(default_factory=lambda: _bool("POE_ADVISOR_FALLBACKS", True))

    # Path of Exile
    league: str = field(default_factory=lambda: os.getenv("POE_LEAGUE", ""))  # empty = current challenge league
    realm: str = field(default_factory=lambda: os.getenv("POE_REALM", "pc"))
    account: str = field(default_factory=lambda: os.getenv("POE_ACCOUNT", ""))
    character: str = field(default_factory=lambda: os.getenv("POE_CHARACTER", ""))
    poesessid: str = field(default_factory=lambda: os.getenv("POESESSID", ""))
    # GGG asks API consumers to identify themselves with a contact address.
    contact: str = field(default_factory=lambda: os.getenv("POE_ADVISOR_CONTACT", "unset"))

    # Turso: stores past-league price history (empty = past leagues are fetched from poe.ninja and cached on disk)
    turso_url: str = field(default_factory=lambda: os.getenv("TURSO_DATABASE_URL", ""))
    turso_token: str = field(default_factory=lambda: os.getenv("TURSO_AUTH_TOKEN", ""))

    # Local storage
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("POE_ADVISOR_DATA", PROJECT_ROOT / "data")))

    @property
    def user_agent(self) -> str:
        return f"poe-advisor/0.1 (contact: {self.contact})"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def knowledge_path(self) -> Path:
        return self.data_dir / "knowledge.jsonl"


settings = Settings()
