"""Public API and persisted domain models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"]
SourceKind = Literal["local_paper", "web"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ThreadCreate(BaseModel):
    title: str = Field(default="新研究", min_length=1, max_length=100)


class ResearchRunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)
    document_ids: list[str] = Field(default_factory=list, max_length=100)
    use_web: bool = True


class PaperUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    enabled: bool | None = None


class SourceRecord(BaseModel):
    source_id: str
    run_id: str
    kind: SourceKind
    title: str
    url: str | None = None
    document_id: str | None = None
    block_id: str | None = None
    page: int | None = None
    page_end: int | None = None
    section: str | None = None
    excerpt: str
    retrieved_at: str
    status: str = "available"
    metadata: dict[str, Any] = Field(default_factory=dict)


class ResearchAnswer(BaseModel):
    markdown: str
    citation_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class ResearchEvent(BaseModel):
    id: int
    run_id: str
    type: str
    stage: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: str

