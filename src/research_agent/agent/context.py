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
    external_web_required: bool
    evidence_deadline: float
    settings: Settings
    database: Database
    paperlens: PaperLensClient
    web: WebResearchService
    cancel_event: asyncio.Event
    revision_mode: bool = False
    local_search_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def use_budget(self, key: str, limit: int) -> str | None:
        if self.cancel_event.is_set():
            raise asyncio.CancelledError
        if self.revision_mode:
            return "当前处于答案修订阶段，禁止再次检索。请仅使用已有来源完成回答。"
        if time.monotonic() >= self.evidence_deadline:
            return "检索阶段已到截止时间。不得继续调用工具，必须立即使用已有证据生成回答。"
        allowed, _ = self.database.claim_budget(self.run_id, key, limit)
        if not allowed:
            return f"{key} 已达到本轮硬上限 {limit}。不得重试，请使用已有证据完成回答。"
        return None

    def remaining_evidence_seconds(self) -> float:
        return max(0.0, self.evidence_deadline - time.monotonic())

    def web_route_error(self) -> str | None:
        if not self.use_web:
            return "本轮未允许补充公开网页。"
        if self.document_ids and not self.external_web_required:
            local_sources = self.database.count_sources(self.run_id, "local_paper")
            if local_sources > 0:
                return "已取得本地论文证据，且问题未要求最新进展或外部比较；网页检索已停止。请立即综合已有证据。"
            local_attempts = self.database.get_counter(self.run_id, "论文检索")
            paperlens_failed = self.database.get_counter(self.run_id, "paperlens_failures") > 0
            if not paperlens_failed and local_attempts < self.settings.max_local_searches:
                return "本轮问题应先使用所选论文；PaperLens 尚未失败，当前禁止并行网页检索。"
        return None


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
