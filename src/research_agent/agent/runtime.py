"""DeepAgents orchestration with adaptive research and a dedicated writing boundary."""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_openai import ChatOpenAI

from research_agent.agent.context import ToolContext, reset_tool_context, set_tool_context
from research_agent.agent.prompts import (
    COVERAGE_PROMPT,
    CRITIC_PROMPT,
    LOCAL_RESEARCHER_PROMPT,
    MAIN_PROMPT,
    WEB_RESEARCHER_PROMPT,
    WRITER_PROMPT,
)
from research_agent.agent.tools import fetch_web_page, search_local_papers, search_web
from research_agent.config import Settings
from research_agent.db import Database
from research_agent.models import ResearchAnswer, SourceRecord
from research_agent.services.citations import check_citations, sanitize_citations
from research_agent.services.evidence import (
    clean_final_answer,
    paper_explanation_requested,
    select_sources_for_answer,
)
from research_agent.services.paperlens import PaperLensClient
from research_agent.services.web import WebResearchService


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


def _json_object(text: str) -> dict[str, Any] | None:
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _parse_critic(text: str) -> tuple[str, list[str]]:
    payload = _json_object(text)
    if payload is None:
        return "revise", ["审查 Agent 未返回有效 JSON"]
    verdict = "pass" if payload.get("verdict") == "pass" else "revise"
    issues = [str(item) for item in payload.get("issues", []) if str(item).strip()]
    return verdict, issues


def _parse_coverage(text: str) -> dict[str, Any]:
    payload = _json_object(text) or {}
    return {
        "sufficient": payload.get("sufficient") is True,
        "missing_aspects": [str(item) for item in payload.get("missing_aspects", []) if str(item).strip()],
        "suggested_queries": [str(item) for item in payload.get("suggested_queries", []) if str(item).strip()],
        "web_would_help": payload.get("web_would_help") is True,
        "reason": str(payload.get("reason") or "覆盖评估未给出明确原因"),
    }


def _source_bundle(sources: list[SourceRecord], *, excerpt_limit: int = 3500) -> list[dict[str, Any]]:
    return [
        {
            "source_id": source.source_id,
            "kind": source.kind,
            "title": source.title,
            "document_id": source.document_id,
            "page": source.page,
            "page_end": source.page_end,
            "section": source.section,
            "url": source.url,
            "content_type": source.metadata.get("content_type"),
            "excerpt": source.excerpt[:excerpt_limit],
        }
        for source in sources
    ]


def _answer_guidance(question: str, document_ids: list[str]) -> str:
    if document_ids and paper_explanation_requested(question):
        return (
            "这是论文整体讲解。优先采用教学式结构：一句话概括；研究背景与核心问题；"
            "方法机制与各模块关系；实验设置和主要结果；贡献、局限与理解建议。"
            "根据实际证据合并或省略小节，不要写成检索报告。"
        )
    if len(document_ids) > 1 and re.search(r"比较|对比|区别|异同|compare|versus|vs\.?", question, re.I):
        return "这是跨论文比较。先给比较结论，再按统一维度对照方法、证据、优缺点和适用条件。"
    return "直接围绕用户问题组织结构；先给结论，再解释依据、机制、边界和必要背景。"


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
        self.coverage_agent = None
        self.writer_agent = None
        self.critic_agent = None
        if not self.available:
            return
        model = ChatOpenAI(
            model=settings.model_name,
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            timeout=settings.http_timeout_seconds * 6,
            max_retries=2,
        )
        local_agent = {
            "name": "local-paper-researcher",
            "description": "从所选 PaperLens 论文中自适应检索带页码原文，直到与问题相关的关键证据得到覆盖。",
            "system_prompt": LOCAL_RESEARCHER_PROMPT,
            "tools": [search_local_papers],
        }
        web_agent = {
            "name": "web-researcher",
            "description": "在确有外部信息缺口时搜索并阅读公开网页。是否调用由首席研究员判断。",
            "system_prompt": WEB_RESEARCHER_PROMPT,
            "tools": [search_web, fetch_web_page],
        }
        self.main_agent = create_deep_agent(
            model=model,
            system_prompt=MAIN_PROMPT,
            subagents=[local_agent, web_agent],
            checkpointer=checkpointer,
            backend=StateBackend(),
            middleware=[ModelCallLimitMiddleware(run_limit=settings.max_model_calls, exit_behavior="end")],
        )
        self.coverage_agent = create_deep_agent(
            model=model,
            system_prompt=COVERAGE_PROMPT,
            backend=StateBackend(),
            middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior="end")],
        )
        self.writer_agent = create_deep_agent(
            model=model,
            system_prompt=WRITER_PROMPT,
            backend=StateBackend(),
            middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior="end")],
        )
        self.critic_agent = create_deep_agent(
            model=model,
            system_prompt=CRITIC_PROMPT,
            backend=StateBackend(),
            middleware=[ModelCallLimitMiddleware(run_limit=3, exit_behavior="end")],
        )

    async def _research(
        self,
        *,
        context: ToolContext,
        question: str,
        document_ids: list[str],
        use_web: bool,
        config: dict[str, Any],
    ) -> None:
        assert self.main_agent is not None and self.coverage_agent is not None
        if document_ids and use_web:
            scope = (
                f"用户选择了 {len(document_ids)} 篇论文，并授权在确有必要时访问网页。"
                "请由你根据问题与证据缺口决定是否使用网页，不要机械调用。"
            )
        elif document_ids:
            scope = f"用户选择了 {len(document_ids)} 篇论文，未授权网页；只能使用本地论文。"
        elif use_web:
            scope = "用户没有选择论文，已授权网页研究。"
        else:
            scope = "用户没有选择论文，也未授权网页；不要调用检索工具。"
        prompt = (
            f"用户原始问题：{question}\n\n信息源权限：{scope}\n"
            "请先完成证据研究，最后返回研究备忘录，不要撰写面向用户的正式答案。"
        )
        remaining = context.remaining_evidence_seconds()
        if remaining <= 1:
            return
        try:
            await asyncio.wait_for(
                self.main_agent.ainvoke({"messages": [{"role": "user", "content": prompt}]}, config=config),
                timeout=remaining,
            )
        except TimeoutError:
            self.database.add_event(
                context.run_id,
                "evidence_deadline",
                "planning",
                "首轮研究已到证据时间预算，转入答案生成",
            )
            return

        previous_gap_signature: tuple[str, ...] | None = None
        while context.remaining_evidence_seconds() >= 35:
            all_sources = self.database.list_sources(context.run_id)
            review_sources = select_sources_for_answer(all_sources, question)
            coverage_input = {
                "question": question,
                "selected_document_count": len(document_ids),
                "web_permitted": use_web,
                "sources": _source_bundle(review_sources, excerpt_limit=1800),
            }
            try:
                coverage_result = await asyncio.wait_for(
                    self.coverage_agent.ainvoke({"messages": [{
                        "role": "user", "content": json.dumps(coverage_input, ensure_ascii=False)
                    }]}),
                    timeout=min(40.0, context.remaining_evidence_seconds() - 5),
                )
                coverage = _parse_coverage(_final_text(coverage_result))
            except TimeoutError:
                self.database.add_event(
                    context.run_id, "coverage_timeout", "review", "证据覆盖评估超时，转入答案生成"
                )
                break
            self.database.add_event(
                context.run_id,
                "coverage_result",
                "review",
                "证据覆盖充分" if coverage["sufficient"] else "证据覆盖仍有关键缺口",
                coverage,
            )
            if coverage["sufficient"]:
                break
            signature = tuple(sorted(coverage["missing_aspects"] + coverage["suggested_queries"]))
            if not signature or signature == previous_gap_signature:
                self.database.add_event(
                    context.run_id,
                    "research_converged",
                    "review",
                    "证据评估未产生新的可执行检索方向，停止研究并生成回答",
                )
                break
            previous_gap_signature = signature
            before = self.database.count_sources(context.run_id)
            continuation = {
                "question": question,
                "coverage_assessment": coverage,
                "instruction": (
                    "请根据评估结果自主判断是否继续论文检索、使用网页或接受当前边界。"
                    "只针对关键缺口研究；有新增证据后返回更新后的研究备忘录，不要写正式答案。"
                ),
            }
            try:
                await asyncio.wait_for(
                    self.main_agent.ainvoke({"messages": [{
                        "role": "user", "content": json.dumps(continuation, ensure_ascii=False)
                    }]}, config=config),
                    timeout=context.remaining_evidence_seconds(),
                )
            except TimeoutError:
                self.database.add_event(
                    context.run_id, "evidence_deadline", "planning", "补充研究到达时间预算，转入答案生成"
                )
                break
            after = self.database.count_sources(context.run_id)
            if after <= before:
                self.database.add_event(
                    context.run_id,
                    "research_converged",
                    "review",
                    "补充研究没有取得新来源，停止重复检索并生成回答",
                )
                break

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
        if not all((self.main_agent, self.coverage_agent, self.writer_agent, self.critic_agent)):
            raise RuntimeError("回答模型未配置，请填写 OPENAI_COMPATIBLE_* 环境变量")
        runtime_started = time.monotonic()
        finalization_deadline = runtime_started + self.settings.run_timeout_seconds - 10.0
        evidence_window = min(
            self.settings.evidence_timeout_seconds,
            max(60.0, self.settings.run_timeout_seconds - 180.0),
        )
        context = ToolContext(
            run_id=run_id,
            thread_id=thread_id,
            question=question,
            document_ids=document_ids,
            use_web=use_web,
            evidence_deadline=time.monotonic() + evidence_window,
            settings=self.settings,
            database=self.database,
            paperlens=self.paperlens,
            web=self.web,
            cancel_event=cancel_event,
        )
        token = set_tool_context(context)
        config = {"configurable": {"thread_id": thread_id}}
        try:
            self.database.add_event(run_id, "plan", "planning", "正在制定研究计划", {
                "document_count": len(document_ids),
                "web_permitted": use_web,
                "source_routing": "agent_decision",
                "evidence_timeout_seconds": evidence_window,
            })
            self.database.add_event(
                run_id,
                "routing",
                "planning",
                "信息源由 Agent 根据问题和证据缺口自主选择",
                {"local_available": bool(document_ids), "web_permitted": use_web},
            )
            await self._research(
                context=context,
                question=question,
                document_ids=document_ids,
                use_web=use_web,
                config=config,
            )
            context.revision_mode = True
            sources = self.database.list_sources(run_id)
            selected_sources = select_sources_for_answer(sources, question)
            bundle = _source_bundle(selected_sources)
            self.database.add_event(run_id, "writer_start", "writing", "正在组织结构并撰写最终回答", {
                "available_sources": len(sources), "selected_sources": len(selected_sources),
            })
            writer_input = {
                "question": question,
                "structure_guidance": _answer_guidance(question, document_ids),
                "sources": bundle,
            }
            remaining = finalization_deadline - time.monotonic()
            if remaining < 10:
                raise RuntimeError("证据研究占用了全部时间，无法生成最终回答")
            writer_result = await asyncio.wait_for(
                self.writer_agent.ainvoke({"messages": [{
                    "role": "user", "content": json.dumps(writer_input, ensure_ascii=False)
                }]}),
                timeout=min(90.0, remaining),
            )
            draft = _final_text(writer_result)
            if not draft:
                raise RuntimeError("写作 Agent 未返回有效回答")
            draft = clean_final_answer(draft, {source.source_id for source in selected_sources})

            remaining = finalization_deadline - time.monotonic()
            verdict, issues = "not_run", []
            if remaining >= 10:
                self.database.add_event(run_id, "critic_start", "review", "证据审查 Agent 正在核验回答")
                try:
                    critic_result = await asyncio.wait_for(
                        self.critic_agent.ainvoke({"messages": [{"role": "user", "content": json.dumps({
                            "question": question, "draft": draft, "sources": bundle,
                        }, ensure_ascii=False)}]}),
                        timeout=min(90.0, remaining),
                    )
                    verdict, issues = _parse_critic(_final_text(critic_result))
                except TimeoutError:
                    verdict = "timeout"
                    self.database.add_event(
                        run_id, "critic_timeout", "review", "语义审查超时，继续执行确定性引用校验"
                    )
            deterministic = check_citations(draft, selected_sources)
            if deterministic.unknown_ids:
                issues.append(f"删除未知引用：{', '.join(deterministic.unknown_ids)}")
            if deterministic.has_sources_without_citations:
                issues.append("回答没有使用本轮来源编号")
            self.database.add_event(run_id, "critic_result", "review", f"证据审查结果：{verdict}", {
                "verdict": verdict, "issues": issues,
            })

            remaining = finalization_deadline - time.monotonic()
            if (verdict == "revise" or issues) and remaining >= 10:
                revision_input = {
                    **writer_input,
                    "draft_to_revise": draft,
                    "review_issues": issues or ["提高结论、解释和证据之间的对应关系"],
                    "instruction": (
                        "输出修订后的完整最终正文。不得提及修订、审查、检索、来源配额或上一稿；"
                        "不要添加修订说明。"
                    ),
                }
                try:
                    revised = await asyncio.wait_for(
                        self.writer_agent.ainvoke({"messages": [{
                            "role": "user", "content": json.dumps(revision_input, ensure_ascii=False)
                        }]}),
                        timeout=min(90.0, remaining),
                    )
                    revised_text = _final_text(revised)
                    if revised_text:
                        draft = clean_final_answer(
                            revised_text, {source.source_id for source in selected_sources}
                        )
                except TimeoutError:
                    self.database.add_event(
                        run_id, "revision_timeout", "review", "答案修订超时，保留原回答"
                    )

            draft = clean_final_answer(draft, {source.source_id for source in selected_sources})
            cleaned, check, citation_limitations = sanitize_citations(draft, sources)
            limitations = list(citation_limitations)
            if not selected_sources:
                limitations.append("本轮没有取得可用于回答的高质量来源证据。")
            if not check.valid:
                limitations.append("部分引用未通过确定性校验。")
            return ResearchAnswer(markdown=cleaned, citation_ids=check.citation_ids, limitations=limitations)
        finally:
            reset_tool_context(token)
