"""Environment-backed settings for the local ResearchAgent service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _integer(name: str, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _float(name: str, default: float, minimum: float = 0.1) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    data_dir: Path
    app_database: Path
    checkpoint_database: Path
    paperlens_base_url: str
    paperlens_workspace_id: str
    paperlens_reranker_mode: str
    model_base_url: str
    model_api_key: str
    model_name: str
    cors_origins: tuple[str, ...]
    max_model_calls: int
    max_web_bytes: int
    http_timeout_seconds: float
    ddgs_timeout_seconds: float
    paperlens_timeout_seconds: float
    evidence_timeout_seconds: float
    run_timeout_seconds: float

    @property
    def model_configured(self) -> bool:
        return bool(self.model_base_url and self.model_api_key and self.model_name)

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)


def load_settings(*, data_dir: Path | None = None) -> Settings:
    configured_data_dir = data_dir or Path(
        os.getenv("RESEARCH_AGENT_DATA_DIR", str(PROJECT_ROOT / "data" / "runtime"))
    )
    if not configured_data_dir.is_absolute():
        configured_data_dir = PROJECT_ROOT / configured_data_dir
    resolved_data_dir = configured_data_dir.resolve()
    origins = tuple(
        value.strip()
        for value in os.getenv(
            "RESEARCH_AGENT_CORS_ORIGINS",
            "http://127.0.0.1:5174,http://localhost:5174",
        ).split(",")
        if value.strip()
    )
    paperlens_reranker_mode = os.getenv("PAPERLENS_RERANKER_MODE", "local").strip().lower()
    if paperlens_reranker_mode not in {"local", "off"}:
        raise ValueError("PAPERLENS_RERANKER_MODE must be 'local' or 'off'")
    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=resolved_data_dir,
        app_database=resolved_data_dir / "research_agent.sqlite3",
        checkpoint_database=resolved_data_dir / "checkpoints.sqlite3",
        paperlens_base_url=os.getenv("PAPERLENS_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        paperlens_workspace_id=os.getenv("PAPERLENS_WORKSPACE_ID", "research-agent").strip(),
        paperlens_reranker_mode=paperlens_reranker_mode,
        model_base_url=os.getenv("OPENAI_COMPATIBLE_BASE_URL", "").rstrip("/"),
        model_api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY", ""),
        model_name=os.getenv("OPENAI_COMPATIBLE_MODEL", "").strip(),
        cors_origins=origins,
        max_model_calls=_integer("RESEARCH_AGENT_MAX_MODEL_CALLS", 40, maximum=80),
        max_web_bytes=_integer("RESEARCH_AGENT_MAX_WEB_BYTES", 2_000_000, 1024, 10_000_000),
        http_timeout_seconds=_float("RESEARCH_AGENT_HTTP_TIMEOUT_SECONDS", 15),
        ddgs_timeout_seconds=_float("RESEARCH_AGENT_DDGS_TIMEOUT_SECONDS", 20, 3),
        paperlens_timeout_seconds=_float("PAPERLENS_TIMEOUT_SECONDS", 300),
        evidence_timeout_seconds=_float("RESEARCH_AGENT_EVIDENCE_TIMEOUT_SECONDS", 360, 30),
        run_timeout_seconds=_float("RESEARCH_AGENT_RUN_TIMEOUT_SECONDS", 600, 60),
    )
