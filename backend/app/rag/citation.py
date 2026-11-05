import re
from typing import List, Set
from app.retrieval.vector_store import ScoredChunk
from app.schemas.chat import Citation


class CitationExtractor:
    @staticmethod
    def extract_citations(
        answer_text: str,
        retrieved_chunks: List[ScoredChunk],
    ) -> List[Citation]:
        """
        Extract citations and strictly validate them against retrieved chunks.
        Rules:
        1. Only citations actually cited in answer_text as [Ref-X] are considered.
        2. [Ref-X] must satisfy 1 <= X <= len(retrieved_chunks).
        3. Hallucinated/fabricated reference numbers (e.g. [Ref-99]) are strictly filtered out.
        4. If answer_text contains no [Ref-X] tags, returns an empty list.
        5. Citations are deduplicated preserving the order of appearance in the text.
        6. Citation metadata (doc name, page number, similarity, content snippet)
           strictly corresponds to the database chunk.
        """
        if not answer_text or not retrieved_chunks:
            return []

        # Find all cited reference indices in answer text, e.g. [Ref-1], [Ref-2]
        pattern = r"\[Ref-(\d+)\]"
        matches = re.findall(pattern, answer_text)
        if not matches:
            return []

        citations: List[Citation] = []
        seen_indices: Set[int] = set()

        for match_str in matches:
            try:
                ref_idx = int(match_str)
            except ValueError:
                continue

            # Verify whether ref_idx is valid for this retrieval session
            if 1 <= ref_idx <= len(retrieved_chunks):
                if ref_idx in seen_indices:
                    continue
                seen_indices.add(ref_idx)

                chunk = retrieved_chunks[ref_idx - 1]
                snippet = chunk.content.strip()
                if len(snippet) > 200:
                    snippet = snippet[:197] + "..."

                src_type = getattr(chunk, "source_type", None)
                h_path = getattr(chunk, "heading_path", None)
                b_idx = getattr(chunk, "block_index", None)
                l_start = getattr(chunk, "line_start", None)
                l_end = getattr(chunk, "line_end", None)

                prov_label = None
                if src_type and src_type != "knowledge_base":
                    from app.attachments.budget import AttachmentBudgetManager
                    prov_label = AttachmentBudgetManager._format_chunk_provenance(chunk)
                else:
                    prov_label = f"第 {chunk.page_number} 页"

                citations.append(
                    Citation(
                        citation_index=ref_idx,
                        chunk_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        document_name=chunk.document_name,
                        page_number=chunk.page_number,
                        similarity_score=chunk.similarity_score,
                        content_snippet=snippet,
                        source_type=src_type,
                        heading_path=h_path,
                        block_index=b_idx,
                        line_start=l_start,
                        line_end=l_end,
                        provenance_label=prov_label,
                    )
                )

        return citations
