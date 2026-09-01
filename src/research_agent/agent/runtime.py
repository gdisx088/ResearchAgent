"""DeepAgents orchestration with a deterministic evidence-review boundary."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_openai import ChatOpenAI

from research_agent.agent.context import ToolContext, reset_tool_context, set_tool_context
from research_agent.agent.prompts import CRITIC_PROMPT, LOCAL_RESEARCHER_PROMPT, MAIN_PROMPT, WEB_RESEARCHER_PROMPT
from research_agent.agent.tools import fetch_web_page, search_local_papers, search_web
from research_agent.config import Settings
from research_agent.db import Database
from research_agent.models import ResearchAnswer
from research_agent.services.citations import check_citations, sanitize_citations
from research_agent.services.paperlens import PaperLensClient
from research_agent.services.web import WebResearchService


_EXTERNAL_WEB_CUES = re.compile(
    r"最新|近期|当前进展|研究进展|领域进展|发展趋势|现状|新闻|截至|"
    r"外部资料|公开资料|互联网|网页|其他论文|相关论文|文献综述|横向比较|对比其他|"
    r"\blatest\b|\brecent\b|\bcurrent\b|\btrend(?:s)?\b|\bnews\b|\bweb\b|"
    r"\binternet\b|state[ -]of[ -]the[ -]art|related (?:work|studies)|compare with other",
    flags=re.I,
)


def question_requires_external_web(question: str) -> bool:
    """Return whether the wording explicitly asks beyond the selected local papers."""
    return bool(_EXTERNAL_WEB_CUES.search(question))


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts).strip()
    return str(content).strip() if content is not None else ""


def _final_text(result: dict[str, Any]) -> str:
    messages = result.get("messages") or []
    for message in reversed(messages):
        text = _message_text(message)
        if text and getattr(message, "type", "") in {"ai", "assistant"}:
            return text
    return _message_text(messages[-1]) if messages else ""


def _parse_critic(text: str) -> tuple[str, list[str]]:
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return "revise", ["审查 Agent 未返回有效 JSON"]
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "revise", ["审查 Agent 返回的 JSON 无法解析"]
    verdict = "pass" if payload.get("verdict") == "pass" else "revise"
    issues = [str(item) for item in payload.get("issues", []) if str(item).strip()]
    return verdict, issues


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        paperlens: PaperLensClient,
        web: WebResearchService,
        checkpointer: Any,
    ) -> None:
        self.settings = settings
        self.database = database
        self.paperlens = paperlens
        self.web = web
        self.available = settings.model_configured
        self.main_agent = None
        self.critic_agent = None
        if not self.available:
            return
        model = ChatOpenAI(
            model=settings.model_name,
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            timeout=settings.http_timeout_seconds * 4,
            max_retries=2,
        )
        local_agent = {
            "name": "local-paper-researcher",
            "description": "从用户已选择的 PaperLens 本地论文中检索带页码的原文证据。",
            "system_prompt": LOCAL_RESEARCHER_PROMPT,
            "tools": [search_local_papers],
            "middleware": [
                ModelCallLimitMiddleware(run_limit=6, exit_behavior="end"),
                ToolCallLimitMiddleware(tool_name="search_local_papers", run_limit=4, exit_behavior="end"),
            ],
        }
        web_agent = {
            "name": "web-researcher",
            "description": "搜索并阅读公开网页，用于最新资料、官方信息和本地论文之外的背景。",
            "system_prompt": WEB_RESEARCHER_PROMPT,
            "tools": [search_web, fetch_web_page],
            "middleware": [
                ModelCallLimitMiddleware(run_limit=8, exit_behavior="end"),
                ToolCallLimitMiddleware(tool_name="search_web", run_limit=6, exit_behavior="end"),
                ToolCallLimitMiddleware(tool_name="fetch_web_page", run_limit=8, exit_behavior="end"),
            ],
        }
        self.main_agent = create_deep_agent(
            model=model,
            system_prompt=MAIN_PROMPT,
            subagents=[local_agent, web_agent],
            checkpointer=checkpointer,
            backend=StateBackend(),
            middleware=[
                ModelCallLimitMiddleware(run_limit=settings.max_model_calls, exit_behavior="end"),
                ToolCallLimitMiddleware(tool_name="task", run_limit=4, exit_behavior="end"),
            ],
        )
        self.critic_agent = create_deep_agent(
            model=model,
            system_prompt=CRITIC_PROMPT,
            backend=StateBackend(),
            middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior="end")],
        )

    async def run(
        self,
        *,
        run_id: str,
        thread_id: str,
        question: str,
        document_ids: list[str],
        use_web: bool,
        cancel_event: asyncio.Event,
    ) -> ResearchAnswer:
        if not self.available or self.main_agent is None or self.critic_agent is None:
            raise RuntimeError("回答模型未配置，请填写 OPENAI_COMPATIBLE_* 环境变量")
        runtime_started = time.monotonic()
        finalization_deadline = runtime_started + self.settings.run_timeout_seconds - 10.0
        external_web_required = bool(
            use_web and (not document_ids or question_requires_external_web(question))
        )
        evidence_window = min(
            self.settings.evidence_timeout_seconds,
            max(30.0, self.settings.run_timeout_seconds - 75.0),
        )
        context = ToolContext(
            run_id=run_id,
            thread_id=thread_id,
            question=question,
            document_ids=document_ids,
            use_web=use_web,
            external_web_required=external_web_required,
            evidence_deadline=time.monotonic() + evidence_window,
            settings=self.settings,
            database=self.database,
            paperlens=self.paperlens,
            web=self.web,
            cancel_event=cancel_event,
        )
        token = set_tool_context(context)
        config = {"configurable": {"thread_id": thread_id}}
        if document_ids and external_web_required:
            source_plan = f"已选择 {len(document_ids)} 篇本地论文；问题明确需要外部信息，使用本地论文和公开网页。"
            route_message = "来源路由：本地论文 + 公开网页（问题明确要求外部或时效信息）"
        elif document_ids:
            source_plan = (
                f"已选择 {len(document_ids)} 篇本地论文；只检索本地论文。"
                + ("仅当 PaperLens 失败或无结果时，才允许网页降级补充。" if use_web else "不得调用网页专家。")
            )
            route_message = "来源路由：本地论文优先；网页仅在 PaperLens 不可用或无结果时降级"
        elif use_web:
            source_plan = "未选择本地论文；使用公开网页。"
            route_message = "来源路由：公开网页（未选择本地论文）"
        else:
            source_plan = "未选择任何可用信息源；不得检索，并在回答中说明证据限制。"
            route_message = "来源路由：无可用信息源"
        user_prompt = f"研究问题：{question}\n\n本轮来源范围：{source_plan}\n请开始规划、检索并形成带引用的回答。"
        try:
            self.database.add_event(run_id, "plan", "planning", "正在制定研究计划", {
                "document_count": len(document_ids), "use_web": use_web,
                "external_web_required": external_web_required,
                "evidence_timeout_seconds": evidence_window,
            })
            self.database.add_event(run_id, "routing", "planning", route_message, {
                "local_enabled": bool(document_ids),
                "web_requested": use_web,
                "external_web_required": external_web_required,
            })
            result = await self.main_agent.ainvoke({"messages": [{"role": "user", "content": user_prompt}]}, config=config)
            draft = _final_text(result)
            if not draft:
                raise RuntimeError("主 Agent 未返回有效回答")
            sources = self.database.list_sources(run_id)
            bundle = [
                {
                    "source_id": source.source_id,
                    "kind": source.kind,
                    "title": source.title,
                    "page": source.page,
                    "url": source.url,
                    "excerpt": source.excerpt[:1600],
                }
                for source in sources
            ]
            remaining = finalization_deadline - time.monotonic()
            verdict, issues = "pass", []
            if remaining >= 5:
                self.database.add_event(run_id, "critic_start", "review", "证据审查 Agent 正在核验草稿")
                try:
                    critic_result = await asyncio.wait_for(
                        self.critic_agent.ainvoke({"messages": [{"role": "user", "content": json.dumps({
                            "draft": draft, "sources": bundle,
                        }, ensure_ascii=False)}]}),
                        timeout=min(45.0, remaining),
                    )
                    verdict, issues = _parse_critic(_final_text(critic_result))
                except TimeoutError:
                    self.database.add_event(
                        run_id,
                        "critic_timeout",
                        "review",
                        "证据审查超过时间预算，保留草稿并执行确定性引用校验",
                    )
            else:
                self.database.add_event(
                    run_id,
                    "critic_skipped",
                    "review",
                    "剩余时间不足，跳过语义审查并执行确定性引用校验",
                )
            deterministic = check_citations(draft, sources)
            if deterministic.unknown_ids:
                issues.append(f"存在未知引用：{', '.join(deterministic.unknown_ids)}")
            if deterministic.has_sources_without_citations:
                issues.append("草稿没有使用本轮来源编号")
            self.database.add_event(run_id, "critic_result", "review", f"证据审查结果：{verdict}", {
                "verdict": verdict, "issues": issues,
            })
            remaining = finalization_deadline - time.monotonic()
            if (verdict == "revise" or issues) and remaining >= 5:
                context.revision_mode = True
                revision_prompt = (
                    "请只修订上一份回答，禁止调用任何专家或检索工具。必须解决以下审查问题，"
                    "并且只能使用已经取得的来源编号：\n- "
                    + "\n- ".join(issues or ["提高证据与结论的对应关系"])
                    + "\n可用来源：" + ", ".join(source.source_id for source in sources)
                )
                try:
                    revised = await asyncio.wait_for(
                        self.main_agent.ainvoke(
                            {"messages": [{"role": "user", "content": revision_prompt}]}, config=config
                        ),
                        timeout=min(45.0, remaining),
                    )
                    draft = _final_text(revised) or draft
                except TimeoutError:
                    self.database.add_event(
                        run_id,
                        "revision_timeout",
                        "review",
                        "答案修订超过时间预算，保留已通过引用校验的原草稿",
                    )
            cleaned, check, citation_limitations = sanitize_citations(draft, sources)
            limitations = citation_limitations
            if not sources:
                limitations.append("本轮没有取得可持久化的外部或本地证据，回答不应视为已核验结论。")
            if not check.valid:
                limitations.append("部分引用未通过确定性校验。")
            return ResearchAnswer(markdown=cleaned, citation_ids=check.citation_ids, limitations=limitations)
        finally:
            reset_tool_context(token)
