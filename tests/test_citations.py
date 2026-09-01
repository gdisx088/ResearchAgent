from research_agent.models import SourceRecord, utc_now
from research_agent.services.citations import check_citations, sanitize_citations


def source(source_id: str) -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        run_id="run-1",
        kind="web",
        title="Official source",
        url="https://example.com",
        excerpt="Evidence",
        retrieved_at=utc_now(),
    )


def test_citation_validator_rejects_unknown_ids() -> None:
    check = check_citations("有效 [S1]，无效 [S9]", [source("S1")])
    assert check.citation_ids == ["S1"]
    assert check.unknown_ids == ["S9"]
    assert check.valid is False


def test_sanitizer_removes_unknown_and_adds_source_list() -> None:
    cleaned, check, limitations = sanitize_citations("没有有效引用 [S8]", [source("S1")])
    assert "[S8]" not in cleaned
    assert "[S1]" in cleaned
    assert check.valid is True
    assert limitations

