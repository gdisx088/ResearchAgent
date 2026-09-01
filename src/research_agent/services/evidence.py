"""Evidence quality, coverage summaries, and final-answer presentation cleanup."""

from __future__ import annotations

import re
from typing import Any

from research_agent.models import SourceRecord


_AUTHOR_INTENT = re.compile(r"作者|单位|机构|简介|履历|author|affiliation|biograph", re.I)
_FIGURE_INTENT = re.compile(r"图\s*\d*|框架图|架构图|可视化|figure|diagram|architecture", re.I)
_REFERENCE_INTENT = re.compile(r"参考文献|引用|相关工作|已有工作|related work|prior work|bibliograph", re.I)
_PORTRAIT_NOISE = re.compile(
    r"人物照片|成年男性|成年女性|穿着.{0,12}(上衣|西装)|聚焦于人物|面部表情|author photo",
    re.I,
)
_BIOGRAPHY_NOISE = re.compile(
    r"he is (currently|also)|she is (currently|also)|received the .*degree|"
    r"has authored or coauthored|member, ieee|distinguished professor|lecturer with",
    re.I,
)


def paper_explanation_requested(question: str) -> bool:
    return bool(re.search(r"讲解|解读|介绍|概述|概括|总结.{0,8}(论文|文章)|explain|overview", question, re.I))


def paperlens_rejection_reason(item: dict[str, Any], query: str) -> str | None:
    """Reject obviously non-semantic retrieval noise without second-guessing useful prose."""
    excerpt = str(item.get("excerpt") or "").strip()
    content_type = str(item.get("content_type") or "").lower()
    if not excerpt:
        return "empty"
    if _PORTRAIT_NOISE.search(excerpt):
        return "portrait"
    if content_type == "heading" and len(excerpt) < 60:
        return "short_heading"
    if content_type == "reference":
        if _BIOGRAPHY_NOISE.search(excerpt) and not _AUTHOR_INTENT.search(query):
            return "author_biography"
        if not (_AUTHOR_INTENT.search(query) or _REFERENCE_INTENT.search(query)):
            return "reference_section"
    if content_type == "figure" and not _FIGURE_INTENT.search(query):
        return "unrequested_figure"
    if len(excerpt) < 45 and content_type not in {"formula", "table", "figure_caption"}:
        return "too_short"
    return None


def evidence_coverage(items: list[dict[str, Any]]) -> dict[str, Any]:
    labels: set[str] = set()
    sections: list[str] = []
    for item in items:
        section = str(item.get("section") or "").strip()
        excerpt = str(item.get("excerpt") or "")
        text = f"{section} {excerpt}".lower()
        if section and section not in sections:
            sections.append(section)
        if re.search(r"abstract|摘要|overview", text):
            labels.add("overview")
        if re.search(r"introduction|背景|motivation|challenge|problem|adversarial attack", text):
            labels.add("problem")
        if re.search(r"method|methodology|framework|proposed|module|loss|algorithm|模型|方法|框架|损失", text):
            labels.add("method")
        if re.search(r"experiment|result|dataset|baseline|accuracy|table|实验|结果|数据集|基线", text):
            labels.add("experiments")
        if re.search(r"conclusion|limitation|future work|结论|局限|未来", text):
            labels.add("conclusion")
    return {"aspects": sorted(labels), "sections": sections[:12]}


def source_is_noisy(source: SourceRecord, question: str) -> bool:
    if source.kind != "local_paper":
        return False
    payload = {
        "excerpt": source.excerpt,
        "content_type": source.metadata.get("content_type"),
    }
    return paperlens_rejection_reason(payload, question) is not None


def select_sources_for_answer(
    sources: list[SourceRecord], question: str, *, character_budget: int = 90_000
) -> list[SourceRecord]:
    """Build a high-signal writer context while retaining stable source IDs."""
    useful = [source for source in sources if not source_is_noisy(source, question)]

    def priority(source: SourceRecord) -> tuple[int, int, int]:
        content_type = str(source.metadata.get("content_type") or "")
        semantic = content_type in {"text", "table", "formula", "figure_caption"}
        return (0 if semantic else 1, 0 if source.kind == "local_paper" else 1, int(source.source_id[1:]))

    selected: list[SourceRecord] = []
    used = 0
    for source in sorted(useful, key=priority):
        cost = min(len(source.excerpt), 4000) + 300
        if selected and used + cost > character_budget:
            continue
        selected.append(source)
        used += cost
    return sorted(selected, key=lambda source: int(source.source_id[1:]))


def normalize_visible_citations(markdown: str, valid_ids: set[str]) -> str:
    def joined(match: re.Match[str]) -> str:
        ids = re.findall(r"S\d+", match.group(0))
        if len(ids) < 2 or any(value not in valid_ids for value in ids):
            return match.group(0)
        return "".join(f"[{value}]" for value in ids)

    markdown = re.sub(r"(?<![\w\[])(?:S\d+){2,}(?![\w\]])", joined, markdown)
    markdown = re.sub(r"(?<![\w\[])(?:S\d+[、,，/ ]+){1,}S\d+(?![\w\]])", joined, markdown)
    for source_id in sorted(valid_ids, key=len, reverse=True):
        markdown = re.sub(
            rf"(?<![\w\[])({re.escape(source_id)})(?![\w\]])",
            rf"[\1]",
            markdown,
        )
    return markdown


def clean_final_answer(markdown: str, valid_ids: set[str]) -> str:
    """Remove orchestration leakage and normalize common citation formatting mistakes."""
    lines: list[str] = []
    skipping_revision_section = False
    for line in markdown.strip().splitlines():
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+.*修订说明", stripped):
            skipping_revision_section = True
            continue
        if skipping_revision_section and re.match(r"^#{1,6}\s+", stripped):
            skipping_revision_section = False
        if skipping_revision_section:
            continue
        if stripped.startswith(">") and re.search(r"修订|已取得的来源|未调用.*检索", stripped):
            continue
        if re.match(r"^(修订说明|说明)[:：]", stripped) and re.search(r"来源|检索|上一稿|本版", stripped):
            continue
        if re.search(r"未调用任何检索工具|仅依据已取得的来源编号|恢复.*检索配额", stripped):
            continue
        line = line.replace("（修订版）", "").replace("(修订版)", "").replace("修订版：", "")
        lines.append(line.rstrip())
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return normalize_visible_citations(cleaned, valid_ids)
