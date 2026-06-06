from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE lines without requiring python-dotenv."""
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    tushare_token: str | None
    demo_publish_url: str | None
    demo_api_key: str | None
    enable_scheduler: bool
    daily_provider: str
    openai_api_key: str | None
    openai_model: str
    signal_track_api_key: str | None
    auto_publish_on_update: bool = True
    openai_web_research: bool = False
    openai_web_search_context_size: str = "medium"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            db_path=Path(os.getenv("SIGNAL_TRACK_DB_PATH", "data/signal_track.sqlite3")),
            tushare_token=os.getenv("TUSHARE_TOKEN") or None,
            demo_publish_url=os.getenv("GO_SITES_DEMO_PUBLISH_URL") or None,
            demo_api_key=os.getenv("GO_SITES_DEMO_API_KEY") or None,
            enable_scheduler=parse_bool(os.getenv("SIGNAL_TRACK_ENABLE_SCHEDULER"), default=False),
            daily_provider=os.getenv("SIGNAL_TRACK_DAILY_PROVIDER") or "auto",
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            openai_model=os.getenv("SIGNAL_TRACK_OPENAI_MODEL", "gpt-4o-mini"),
            signal_track_api_key=os.getenv("SIGNAL_TRACK_API_KEY") or None,
            auto_publish_on_update=parse_bool(os.getenv("SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE"), default=True),
            openai_web_research=parse_bool(os.getenv("SIGNAL_TRACK_OPENAI_WEB_RESEARCH"), default=False),
            openai_web_search_context_size=normalize_search_context_size(
                os.getenv("SIGNAL_TRACK_OPENAI_WEB_SEARCH_CONTEXT_SIZE")
            ),
        )


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_search_context_size(value: str | None) -> str:
    size = (value or "medium").strip().lower()
    if size in {"low", "medium", "high"}:
        return size
    return "medium"
