from research_agent.models import SourceRecord, utc_now
from research_agent.services.evidence import (
    clean_final_answer,
    evidence_coverage,
    paperlens_rejection_reason,
    select_sources_for_answer,
)


def _source(source_id: str, excerpt: str, content_type: str) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        run_id="run-1",
        kind="local_paper",
        title="Paper",
        document_id="paper-1",
        page=1,
        excerpt=excerpt,
        retrieved_at=utc_now(),
        metadata={"content_type": content_type},
    )


def test_paperlens_noise_filter_rejects_portraits_bios_and_short_headings() -> None:
    assert paperlens_rejection_reason({
        "content_type": "figure", "excerpt": "图表视觉分析：这是一张人物照片，显示了一位成年男性。"
    }, "讲解论文") == "portrait"
    assert paperlens_rejection_reason({
        "content_type": "reference", "excerpt": "He is currently a Lecturer with the College of Sciences. He has authored or coauthored papers."
    }, "讲解论文") == "author_biography"
    assert paperlens_rejection_reason({
        "content_type": "heading", "excerpt": "I. INTRODUCTION"
    }, "讲解论文") == "short_heading"
    assert paperlens_rejection_reason({
        "content_type": "reference", "excerpt": "[41] A prior graph learning paper with full bibliographic metadata."
    }, "讲解论文") == "reference_section"


def test_coverage_reports_method_and_experiments() -> None:
    coverage = evidence_coverage([
        {"section": "Methodology", "excerpt": "The proposed framework optimizes a contrastive loss."},
        {"section": "Experiments", "excerpt": "Results on three datasets improve node classification accuracy."},
    ])
    assert {"method", "experiments"}.issubset(set(coverage["aspects"]))


def test_writer_context_removes_noise_but_keeps_semantic_text() -> None:
    sources = [
        _source("S1", "图表视觉分析：这是一张人物照片，显示了一位成年男性。", "figure"),
        _source("S2", "The proposed framework combines adaptive graph augmentation with contrastive learning to improve robustness.", "text"),
    ]
    assert [item.source_id for item in select_sources_for_answer(sources, "讲解论文")] == ["S2"]


def test_final_cleanup_removes_revision_leakage_and_normalizes_citations() -> None:
    raw = """# 论文讲解（修订版）

> 修订说明：本版仅依据已取得的来源编号 S1–S2，未调用任何检索工具。

方法由两部分组成 S1S2。

## 结论

模型提升了鲁棒性 S2。
"""
    cleaned = clean_final_answer(raw, {"S1", "S2"})
    assert "修订" not in cleaned
    assert "检索工具" not in cleaned
    assert "[S1][S2]" in cleaned
    assert "[S2]" in cleaned
