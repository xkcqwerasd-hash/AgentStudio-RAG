import pytest
from app.rag.citation import CitationExtractor
from app.retrieval.vector_store import ScoredChunk


@pytest.fixture
def mock_retrieved_chunks():
    return [
        ScoredChunk(
            chunk_id="chunk-uuid-001",
            document_id="doc-uuid-001",
            document_name="公司员工考勤制度.pdf",
            page_number=3,
            chunk_index=0,
            content="员工每日上午 09:00 至 10:00 之间到岗，并在每日工作满 8 小时后离岗打卡。",
            similarity_score=0.885,
        ),
        ScoredChunk(
            chunk_id="chunk-uuid-002",
            document_id="doc-uuid-001",
            document_name="公司员工考勤制度.pdf",
            page_number=4,
            chunk_index=1,
            content="每月事假上限为 3 天，超过需经部门主管及 HRBP 审批。",
            similarity_score=0.742,
        ),
        ScoredChunk(
            chunk_id="chunk-uuid-003",
            document_id="doc-uuid-002",
            document_name="薪酬福利体系规范.md",
            page_number=1,
            chunk_index=0,
            content="公积金缴纳比例为 12%，次月 10 日前统一完成扣缴与申报。",
            similarity_score=0.698,
        ),
    ]


def test_citation_normal_matching(mock_retrieved_chunks):
    """Test 1: Normal citations [Ref-1] and [Ref-2] are correctly extracted."""
    answer = "根据考勤规定，到岗时间为 9 点至 10 点 [Ref-1]。此外事假上限为 3 天 [Ref-2]。"
    citations = CitationExtractor.extract_citations(answer, mock_retrieved_chunks)

    assert len(citations) == 2
    assert citations[0].citation_index == 1
    assert citations[0].document_name == "公司员工考勤制度.pdf"
    assert citations[0].page_number == 3
    assert "09:00 至 10:00" in citations[0].content_snippet

    assert citations[1].citation_index == 2
    assert citations[1].document_name == "公司员工考勤制度.pdf"
    assert citations[1].page_number == 4
    assert "事假上限为 3 天" in citations[1].content_snippet


def test_citation_filters_fabricated_refs(mock_retrieved_chunks):
    """Test 2: Model hallucinates [Ref-99] and [Ref-5] which exceed retrieved count."""
    answer = "这是由大模型凭空编造的引用 [Ref-99]，以及超出数量的引用 [Ref-5]，还有有效引用 [Ref-1]。"
    citations = CitationExtractor.extract_citations(answer, mock_retrieved_chunks)

    # Only [Ref-1] is valid; [Ref-99] and [Ref-5] must be filtered out!
    assert len(citations) == 1
    assert citations[0].citation_index == 1
    assert citations[0].chunk_id == "chunk-uuid-001"


def test_citation_no_refs_in_answer(mock_retrieved_chunks):
    """Test 3: Model generates text without any [Ref-X] marks."""
    answer = "这是一个通用的总结回答，没有包含任何具体的引用标记。"
    citations = CitationExtractor.extract_citations(answer, mock_retrieved_chunks)

    assert len(citations) == 0


def test_citation_deduplication_and_order(mock_retrieved_chunks):
    """Test 4: Answer repeats [Ref-2] and cites [Ref-1] later; should deduplicate preserving first appearance."""
    answer = "根据请假规范 [Ref-2]，事假有限制。关于到岗要求参见 [Ref-1]，再次提醒请假事项 [Ref-2]。"
    citations = CitationExtractor.extract_citations(answer, mock_retrieved_chunks)

    assert len(citations) == 2
    # First cited was Ref-2
    assert citations[0].citation_index == 2
    assert citations[0].page_number == 4
    # Second cited was Ref-1
    assert citations[1].citation_index == 1
    assert citations[1].page_number == 3


def test_citation_empty_retrieved_chunks():
    """Test 5: Empty retrieved chunks should return empty citations."""
    answer = "虽然模型回答了 [Ref-1]，但本次检索没有召回任何片段。"
    citations = CitationExtractor.extract_citations(answer, [])
    assert len(citations) == 0


def test_citation_cross_document(mock_retrieved_chunks):
    """Test 6: Citing chunks from different documents preserves distinct doc names and pages."""
    answer = "考勤见 [Ref-1]，而公积金比例为 12% 详见 [Ref-3]。"
    citations = CitationExtractor.extract_citations(answer, mock_retrieved_chunks)

    assert len(citations) == 2
    assert citations[0].document_name == "公司员工考勤制度.pdf"
    assert citations[0].page_number == 3

    assert citations[1].document_name == "薪酬福利体系规范.md"
    assert citations[1].page_number == 1
    assert "公积金缴纳比例" in citations[1].content_snippet
