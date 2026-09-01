"""Run-scoped tool context carried safely through asynchronous agent calls."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

from research_agent.config import Settings
from research_agent.db import Database
from research_agent.services.paperlens import PaperLensClient
from research_agent.services.web import WebResearchService


@dataclass(slots=True)
class ToolContext:
    run_id: str
    thread_id: str
    question: str
    document_ids: list[str]
    use_web: bool
    evidence_deadline: float
    settings: Settings
    database: Database
    paperlens: PaperLensClient
    web: WebResearchService
    cancel_event: asyncio.Event
    revision_mode: bool = False
    local_search_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seen_local_queries: set[str] = field(default_factory=set)
    seen_web_queries: set[str] = field(default_factory=set)
    seen_web_urls: set[str] = field(default_factory=set)

    def retrieval_error(self) -> str | None:
        if self.cancel_event.is_set():
            raise asyncio.CancelledError
        if self.revision_mode:
            return "当前处于答案修订阶段，禁止再次检索。请仅使用已有来源完成回答。"
        if time.monotonic() >= self.evidence_deadline:
            return "检索阶段已到截止时间。不得继续调用工具，必须立即使用已有证据生成回答。"
        return None

    def remaining_evidence_seconds(self) -> float:
        return max(0.0, self.evidence_deadline - time.monotonic())

    def web_permission_error(self) -> str | None:
        if not self.use_web:
            return "用户未授权本轮访问公开网页。"
        return None

    @staticmethod
    def normalize_query(value: str) -> str:
        return " ".join(value.lower().split())


_context: ContextVar[ToolContext | None] = ContextVar("research_tool_context", default=None)


def set_tool_context(context: ToolContext) -> Token[ToolContext | None]:
    return _context.set(context)


def get_tool_context() -> ToolContext:
    context = _context.get()
    if context is None:
        raise RuntimeError("研究工具不在有效任务上下文中")
    return context


def reset_tool_context(token: Token[ToolContext | None]) -> None:
    _context.reset(token)
