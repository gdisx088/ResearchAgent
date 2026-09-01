"""Deterministic final-answer citation validation and repair."""

from __future__ import annotations

import re
from dataclasses import dataclass

from research_agent.models import SourceRecord


CITATION_RE = re.compile(r"\[(S\d+)\]")


@dataclass(frozen=True, slots=True)
class CitationCheck:
    citation_ids: list[str]
    unknown_ids: list[str]
    has_sources_without_citations: bool

    @property
    def valid(self) -> bool:
        return not self.unknown_ids and not self.has_sources_without_citations


def check_citations(markdown: str, sources: list[SourceRecord]) -> CitationCheck:
    valid_ids = {source.source_id for source in sources}
    cited = list(dict.fromkeys(CITATION_RE.findall(markdown)))
    unknown = [source_id for source_id in cited if source_id not in valid_ids]
    return CitationCheck(
        citation_ids=[source_id for source_id in cited if source_id in valid_ids],
        unknown_ids=unknown,
        has_sources_without_citations=bool(sources) and not any(source_id in valid_ids for source_id in cited),
    )


def sanitize_citations(markdown: str, sources: list[SourceRecord]) -> tuple[str, CitationCheck, list[str]]:
    valid_ids = {source.source_id for source in sources}
    limitations: list[str] = []

    def replace(match: re.Match[str]) -> str:
        if match.group(1) in valid_ids:
            return match.group(0)
        limitations.append(f"已移除不存在的引用 {match.group(0)}")
        return ""

    cleaned = CITATION_RE.sub(replace, markdown).strip()
    check = check_citations(cleaned, sources)
    if check.has_sources_without_citations:
        limitations.append("模型未生成有效行内引用，系统已附上本轮实际使用的来源清单。")
        lines = ["", "### 本轮来源"]
        for source in sources:
            location = f"，第 {source.page} 页" if source.page else ""
            lines.append(f"- [{source.source_id}] {source.title}{location}")
        cleaned = cleaned + "\n" + "\n".join(lines)
        check = check_citations(cleaned, sources)
    return cleaned, check, list(dict.fromkeys(limitations))

