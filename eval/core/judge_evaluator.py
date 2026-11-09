import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

from app.core.logging import logger
from app.retrieval.vector_store import ScoredChunk
from app.schemas.chat import Citation


class JudgeEvaluationResult(BaseModel):
    """
    Standard evaluation schema produced by LLM-as-a-Judge for a single sample.
    """
    sample_id: str
    status: str = "success"  # "success" | "error"
    error_message: Optional[str] = None

    # 4 Core Dimension Scores (1-5 Integer Scale)
    correctness_score: Optional[int] = Field(default=None, description="Answer Correctness (1-5)")
    correctness_reason: Optional[str] = Field(default=None, description="Reasoning for correctness score")

    faithfulness_score: Optional[int] = Field(default=None, description="Faithfulness / Groundedness (1-5)")
    faithfulness_reason: Optional[str] = Field(default=None, description="Reasoning for faithfulness score")

    citation_accuracy_score: Optional[int] = Field(default=None, description="Citation Accuracy (1-5)")
    citation_reason: Optional[str] = Field(default=None, description="Reasoning for citation accuracy score")

    relevancy_score: Optional[int] = Field(default=None, description="Answer Relevancy (1-5)")
    relevancy_reason: Optional[str] = Field(default=None, description="Reasoning for relevancy score")

    # Programmatic Citation Checks
    programmatic_citation_valid: bool = True
    citation_details: Optional[Dict[str, Any]] = None

    # Rejection Evaluation (for unanswerable questions)
    rejection_detected: Optional[bool] = None
    rejection_reason: Optional[str] = None

    # Overall Aggregate Score
    overall_score: Optional[float] = None

    # Observability Metadata
    judge_model: str = ""
    judge_model_returned: Optional[str] = None
    judge_input_tokens: Optional[int] = None
    judge_output_tokens: Optional[int] = None
    judge_cache_hit_tokens: Optional[int] = None
    judge_cache_miss_tokens: Optional[int] = None
    raw_judge_response: str = ""
    duration_ms: float = 0.0


JUDGE_SYSTEM_PROMPT = """你是一个专门评测检索增强生成 (RAG) 系统质量的高级技术评审专家。
你需要对 RAG 系统的回答进行客观、严谨、多维度的评估。

评估标准 (1~5 分量表):
1. Answer Correctness (答案正确性):
   - 5分: 回答完全准确，包含全部核心事实，与参考答案完全一致。技术术语、参数名称、等级名称严格一致。
   - 4分: 主要事实准确，技术术语基本一致，仅遗漏不影响主结论的极其微小修饰信息。
   - 3分: 核心事实部分正确，但遗漏重要限定条件、关键数值，或存在未经验证的术语替换。
   - 2分: 存在严重事实错误、关键数值偏差或核心结论倒置。
   - 1分: 完全错误、答非所问或内容与事实完全相反。

2. Faithfulness (上下文忠实度 / 幻觉检验):
   - 5分: 回答中的每一个声明均能严格从提供的上下文 (Context) 中找到证据支持，零脑补、零幻觉。
   - 4分: 绝大部分有直接依据，仅包含极少量合理的常识性逻辑连词。
   - 3分: 核心结论有依据，但混入了上下文未提及的技术细节、假设或未经证据支持的同义词转换。
   - 2分: 包含多处明显脱离上下文的事实性陈述。
   - 1分: 严重幻觉，回答内容基本无法在上下文中找到依据。

3. Citation Accuracy (引用准确性):
   - 5分: 引用的角标 ([Ref-X]) 精确对应支持该结论的分块，无虚假引用，无错引。
   - 4分: 引用的分块相关，但支持力度为间接背景证据。
   - 3分: 部分陈述有正确引用，部分关键事实缺少引用，或存在 1 处错引。
   - 2分: 引用的分块与被标注陈述基本无关（形式上有角标但内容张冠李戴）。
   - 1分: 虚假引用（如引用不存在的分块编号）或通篇无任何引用。

4. Answer Relevancy (回答切题度):
   - 5分: 直击提问核心，结构清晰，逻辑严密，无冗余废话。
   - 4分: 回答切题，但稍有多余的背景铺垫。
   - 3分: 基本切题，但有一定程度的跑题或泛泛而谈。
   - 2分: 严重偏题，仅碰触个别边缘关键词。
   - 1分: 完全答非所问。

不可回答问题 (Unanswerable) 拒答判定:
- 若问题在上下文中无依据 (answerable=false)，系统若明确说明“未找到依据”或拒绝回答，则判定为正确拒答 (rejection_detected: true)；
- 若系统面对无依据问题却捏造内容，则判定为错误拒答 (rejection_detected: false)。

评审原则与输出要求：
1. 评分必须严格客观，绝不可为了提高整体得分放宽标准，绝不允许迁就模型给出虚高分数。
2. 对技术术语、参数、等级名称增加严格一致性要求：特别是例如 Restricted 与「绝密级」/「受限级」的区别，不能未经上下文与证据支持就主观视为完全同义。若存在差异且无直接证据，必须如实扣分。
3. 若回答中混入未在检索上下文提及的技术细节、过度推论或主观脑补，Faithfulness 必须严格降分（严禁给满分）。
4. 若回答引入与问题核心无关的冗余事实（即便其确实在上下文中出现），Answer Relevancy 必须如实降分。
5. 每项评分理由必须简短、明确、可核验（1~2 句话，直指具体事实差异、术语差异与证据编号）。
6. 必须输出合法的 JSON 格式，不要包含任何 markdown 代码块以外的闲聊文字。JSON 格式如下：
{
  "correctness": {
    "score": 5,
    "reason": "简短事实核验理由..."
  },
  "faithfulness": {
    "score": 5,
    "reason": "简短上下文依据核验理由..."
  },
  "citation_accuracy": {
    "score": 5,
    "reason": "简短引用角标与证据对应理由..."
  },
  "relevancy": {
    "score": 5,
    "reason": "简短切题与冗余核验理由..."
  },
  "rejection": {
    "detected": false,
    "reason": "简短拒答核验理由..."
  }
}
"""


class JudgeEvaluator:
    """
    Independent LLM-as-a-Judge for evaluating RAG generation quality.
    Strictly isolated configuration; zero leakage of API keys.
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 60.0,
        test_mode: bool = False,
    ):
        self.model_name = (
            model_name
            or os.environ.get("EVAL_JUDGE_MODEL")
            or "deepseek-flash"
        )
        from app.core.config import settings
        self.api_key = (
            api_key
            or os.environ.get("EVAL_JUDGE_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or getattr(settings, "OPENAI_API_KEY", "")
        )
        self.base_url = (
            base_url
            or os.environ.get("EVAL_JUDGE_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or "https://api.deepseek.com"
        ).rstrip("/")
        self.timeout = timeout
        self.test_mode = test_mode

    @staticmethod
    def verify_programmatic_citations(
        answer_text: str,
        kept_chunks: List[Any],
        extracted_citations: List[Any],
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        Hard programmatic verification of [Ref-X] citations against kept_chunks.
        """
        pattern = r"\[Ref-(\d+)\]"
        cited_indices = [int(m) for m in re.findall(pattern, answer_text)]
        num_chunks = len(kept_chunks)

        valid_indices = []
        invalid_indices = []
        for idx in cited_indices:
            if 1 <= idx <= num_chunks:
                valid_indices.append(idx)
            else:
                invalid_indices.append(idx)

        is_valid = (len(invalid_indices) == 0)

        details = {
            "total_citation_tags": len(cited_indices),
            "unique_cited_indices": sorted(list(set(cited_indices))),
            "valid_indices": sorted(list(set(valid_indices))),
            "invalid_indices": sorted(list(set(invalid_indices))),
            "total_kept_chunks": num_chunks,
            "extracted_citations_count": len(extracted_citations),
        }
        return is_valid, details

    def build_judge_prompt(
        self,
        query: str,
        reference_answer: str,
        context_chunks: List[Any],
        generated_answer: str,
        answerable: bool,
    ) -> str:
        """Construct prompt for the LLM Judge."""
        # Format context chunks
        context_str = ""
        for idx, chunk in enumerate(context_chunks, start=1):
            c_text = getattr(chunk, "content", str(chunk))
            doc_name = getattr(chunk, "document_name", "未知文档")
            context_str += f"[Ref-{idx}] 文档: {doc_name}\n内容: {c_text}\n\n"

        prompt = f"""请对以下 RAG 系统的回答进行严格评分：

【用户提问】
{query}

【参考标准答案】 (answerable: {answerable})
{reference_answer}

【检索上下文 Context (送入大模型的全部候选证据)】
{context_str.strip() if context_str.strip() else "(无检索上下文 / 知识库无匹配事实)"}

【RAG 系统实际生成回答】
{generated_answer}

请严格按上述四项标准 (1~5 分) 及拒答判定输出 JSON 结果。
"""
        return prompt

    async def evaluate_sample(
        self,
        sample_id: str,
        query: str,
        reference_answer: str,
        kept_chunks: List[Any],
        generated_answer: str,
        extracted_citations: List[Any],
        answerable: bool = True,
    ) -> JudgeEvaluationResult:
        """
        Evaluate a single RAG generation sample end-to-end.
        """
        start_time = time.perf_counter()

        # 1. Programmatic citation check
        prog_valid, cit_details = self.verify_programmatic_citations(
            generated_answer, kept_chunks, extracted_citations
        )

        # 2. Test Double Mode (for offline unit tests without external API calls)
        if self.test_mode:
            duration = (time.perf_counter() - start_time) * 1000.0
            return JudgeEvaluationResult(
                sample_id=sample_id,
                status="success",
                correctness_score=5,
                correctness_reason="[Test Double] Correctness verified offline",
                faithfulness_score=5,
                faithfulness_reason="[Test Double] Context grounded offline",
                citation_accuracy_score=5 if prog_valid else 2,
                citation_reason="[Test Double] Programmatic check: " + ("Valid" if prog_valid else "Invalid"),
                relevancy_score=5,
                relevancy_reason="[Test Double] Query focused offline",
                programmatic_citation_valid=prog_valid,
                citation_details=cit_details,
                rejection_detected=(not answerable),
                rejection_reason="[Test Double] Rejection handled",
                overall_score=5.0,
                judge_model=f"mock-{self.model_name}",
                raw_judge_response='{"mock": true}',
                duration_ms=round(duration, 2),
            )

        # 3. Build Judge User Prompt
        user_prompt = self.build_judge_prompt(
            query=query,
            reference_answer=reference_answer,
            context_chunks=kept_chunks,
            generated_answer=generated_answer,
            answerable=answerable,
        )

        import httpx

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "thinking": {"type": "disabled"},
            "temperature": 0.0,
        }

        raw_text = ""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code != 200:
                    duration = (time.perf_counter() - start_time) * 1000.0
                    return JudgeEvaluationResult(
                        sample_id=sample_id,
                        status="error",
                        error_message=f"HTTP {resp.status_code}: {resp.text[:300]}",
                        programmatic_citation_valid=prog_valid,
                        citation_details=cit_details,
                        judge_model=self.model_name,
                        duration_ms=round(duration, 2),
                    )

                resp_data = resp.json()
                msg = resp_data["choices"][0]["message"]
                # Only read content, explicitly ignore and do not store reasoning_content
                raw_text = str(msg.get("content") or "").strip()
                resp_model = resp_data.get("model", self.model_name)
                usage = resp_data.get("usage", {})
                j_prompt_tokens = usage.get("prompt_tokens")
                j_completion_tokens = usage.get("completion_tokens")
                j_cache_hit = usage.get("prompt_cache_hit_tokens", 0)
                j_cache_miss = usage.get("prompt_cache_miss_tokens", 0)

            # Parse JSON from response
            cleaned_json = raw_text
            json_match = re.search(r"\{[\s\S]*\}", raw_text)
            if json_match:
                cleaned_json = json_match.group(0)

            parsed = json.loads(cleaned_json)

            c_score = int(parsed.get("correctness", {}).get("score", 0))
            c_reason = str(parsed.get("correctness", {}).get("reason", ""))

            f_score = int(parsed.get("faithfulness", {}).get("score", 0))
            f_reason = str(parsed.get("faithfulness", {}).get("reason", ""))

            ca_score = int(parsed.get("citation_accuracy", {}).get("score", 0))
            ca_reason = str(parsed.get("citation_accuracy", {}).get("reason", ""))

            # If programmatic check failed, cap citation_accuracy_score at 2
            if not prog_valid and ca_score > 2:
                ca_score = 2
                ca_reason = f"[程序化校验发现非法引用] {ca_reason}"

            r_score = int(parsed.get("relevancy", {}).get("score", 0))
            r_reason = str(parsed.get("relevancy", {}).get("reason", ""))

            rej_dict = parsed.get("rejection", {})
            rej_detected = bool(rej_dict.get("detected", False))
            rej_reason = str(rej_dict.get("reason", ""))

            # Clamp scores between 1 and 5
            c_score = max(1, min(5, c_score))
            f_score = max(1, min(5, f_score))
            ca_score = max(1, min(5, ca_score))
            r_score = max(1, min(5, r_score))

            overall = round((c_score + f_score + ca_score + r_score) / 4.0, 2)
            duration = (time.perf_counter() - start_time) * 1000.0

            return JudgeEvaluationResult(
                sample_id=sample_id,
                status="success",
                correctness_score=c_score,
                correctness_reason=c_reason,
                faithfulness_score=f_score,
                faithfulness_reason=f_reason,
                citation_accuracy_score=ca_score,
                citation_reason=ca_reason,
                relevancy_score=r_score,
                relevancy_reason=r_reason,
                programmatic_citation_valid=prog_valid,
                citation_details=cit_details,
                rejection_detected=rej_detected,
                rejection_reason=rej_reason,
                overall_score=overall,
                judge_model=self.model_name,
                judge_model_returned=resp_model,
                judge_input_tokens=j_prompt_tokens,
                judge_output_tokens=j_completion_tokens,
                judge_cache_hit_tokens=j_cache_hit,
                judge_cache_miss_tokens=j_cache_miss,
                raw_judge_response=raw_text,
                duration_ms=round(duration, 2),
            )

        except json.JSONDecodeError as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            return JudgeEvaluationResult(
                sample_id=sample_id,
                status="error",
                error_message=f"JSONDecodeError: {exc}. Raw: {raw_text[:200]}",
                programmatic_citation_valid=prog_valid,
                citation_details=cit_details,
                judge_model=self.model_name,
                raw_judge_response=raw_text,
                duration_ms=round(duration, 2),
            )
        except Exception as exc:
            duration = (time.perf_counter() - start_time) * 1000.0
            return JudgeEvaluationResult(
                sample_id=sample_id,
                status="error",
                error_message=f"Exception: {type(exc).__name__}: {str(exc)}",
                programmatic_citation_valid=prog_valid,
                citation_details=cit_details,
                judge_model=self.model_name,
                duration_ms=round(duration, 2),
            )
