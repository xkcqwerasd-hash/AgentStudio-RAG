import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.logging import logger
from app.llm.base import BaseLLMClient
from app.llm.openai_client import get_llm_client

QUERY_REWRITER_SYSTEM_PROMPT = """你是一个专业的企业级知识库问答检索查询重写专家（Query Rewriter）。
你的任务是结合【对话历史】，分析用户的【当前问题】。如果当前问题存在省略信息、代词指代（如“它”、“这个”）、或序数词指代（如“第二种状态”），请将其重写为一个独立、完整、语义明确、适合进行密集向量检索的 Query。

【核心重写原则】
1. 省略信息补全：若当前问题缺少关键主体或属性限定词（如“那最小是多少？”），结合上下文补全主语和属性（如“网关连接池的最小空闲连接数是多少？”）。
2. 代词与序号指代消歧：若当前问题含有“它”、“该”、“第一种”、“第二项”等指代，必须替换为前文确定的具体实体或状态名。
3. 独立问题保持原样：若当前问题是与上下文无关的全新独立问题（如用户突然问“Python装饰器是什么？”），绝对不要强行拼接无关历史信息，保持原始问题不变，并将 is_rewritten 设为 false。
4. 意图与事实保真：
   - 绝对不要回答问题！你只是重写检索词，不是回答助手。
   - 绝对不要编造未出现的事实，不要擅自修改数字或特定参数约束。
   - 无法确定指代关系时，优先保留原始问题，不要主观臆测。
5. 纯净 JSON 输出：必须输出且仅输出如下 JSON 对象，严禁包含任何前缀、解释或额外文字：
{
  "is_rewritten": true,
  "rewritten_query": "重写后的独立完整检索问题",
  "reason": "重写简述"
}
若无需重写，输出：
{
  "is_rewritten": false,
  "rewritten_query": "原始问题",
  "reason": "独立问题，无需重写"
}
"""


class QueryRewriteResult(BaseModel):
    original_query: str = Field(..., description="用户原始问题")
    rewritten_query: str = Field(..., description="最终用于检索的问题")
    is_rewritten: bool = Field(default=False, description="是否触发了有效重写")
    duration_ms: float = Field(default=0.0, description="重写执行耗时(ms)")
    tokens_used: Optional[int] = Field(default=None, description="模型调用的Token数量估算")
    fallback: bool = Field(default=False, description="是否触发了异常回退")
    fallback_reason: Optional[str] = Field(default=None, description="回退具体原因")


class QueryRewriter:
    def __init__(
        self,
        llm_client: Optional[BaseLLMClient] = None,
        enabled: Optional[bool] = None,
        timeout: Optional[float] = None,
        max_history_turns: Optional[int] = None,
    ):
        self.llm_client = llm_client or get_llm_client()
        self.enabled = enabled if enabled is not None else settings.QUERY_REWRITING_ENABLED
        self.timeout = timeout if timeout is not None else settings.QUERY_REWRITING_TIMEOUT
        self.max_history_turns = (
            max_history_turns
            if max_history_turns is not None
            else settings.QUERY_REWRITING_MAX_HISTORY_TURNS
        )

    def format_history_context(self, history_messages: List[Dict[str, str]]) -> str:
        """Format recent history messages into clean prompt context."""
        if not history_messages:
            return ""

        # Limit to max_history_turns * 2 (user + assistant)
        selected = history_messages[-(self.max_history_turns * 2) :]
        lines = []
        for msg in selected:
            role = msg.get("role", "")
            raw_content = msg.get("content", "").strip()
            # Clean interruption markers
            cleaned = (
                raw_content.replace("\n\n[回答已被用户中止]", "")
                .replace("[回答已被用户中止]", "")
                .strip()
            )
            if not cleaned:
                continue
            role_label = "用户" if role == "user" else "AI助手"
            lines.append(f"{role_label}: {cleaned}")

        return "\n".join(lines)

    async def rewrite(
        self,
        query: str,
        history_messages: Optional[List[Dict[str, str]]] = None,
    ) -> QueryRewriteResult:
        """
        Rewrite user query based on multi-turn history.
        Always returns a valid QueryRewriteResult, safely falling back to original query on any failure.
        """
        start_time = time.perf_counter()
        clean_query = query.strip()

        # 1. Quick exit if disabled or empty query
        if not self.enabled or not clean_query:
            return QueryRewriteResult(
                original_query=query,
                rewritten_query=query,
                is_rewritten=False,
                duration_ms=0.0,
            )

        # 2. Quick exit if no conversation history
        history_text = self.format_history_context(history_messages or [])
        if not history_text:
            return QueryRewriteResult(
                original_query=query,
                rewritten_query=query,
                is_rewritten=False,
                duration_ms=0.0,
            )

        # 3. Assemble prompt
        user_prompt = (
            f"【查询重写任务】\n"
            f"【对话历史】：\n{history_text}\n\n"
            f"【当前用户问题】：\n{clean_query}\n\n"
            f"请严格按照要求输出包含 is_rewritten、rewritten_query 和 reason 的纯 JSON 对象："
        )

        try:
            # 4. Call LLM with timeout protection
            raw_resp = await asyncio.wait_for(
                self.llm_client.generate(
                    prompt=user_prompt,
                    system_prompt=QUERY_REWRITER_SYSTEM_PROMPT,
                    temperature=0.0,
                ),
                timeout=self.timeout,
            )

            duration = (time.perf_counter() - start_time) * 1000

            # 5. Parse JSON output
            parsed = self._parse_json_response(raw_resp)
            if not parsed:
                logger.warning(f"QueryRewriter returned unparseable response: {raw_resp[:200]}")
                return QueryRewriteResult(
                    original_query=query,
                    rewritten_query=query,
                    is_rewritten=False,
                    duration_ms=round(duration, 2),
                    fallback=True,
                    fallback_reason="invalid_json",
                )

            is_rewritten = bool(parsed.get("is_rewritten", False))
            rewritten_text = str(parsed.get("rewritten_query", "")).strip()

            # 6. Fallback if rewritten query is empty
            if not rewritten_text:
                logger.warning("QueryRewriter produced empty rewritten_query, falling back to original.")
                return QueryRewriteResult(
                    original_query=query,
                    rewritten_query=query,
                    is_rewritten=False,
                    duration_ms=round(duration, 2),
                    fallback=True,
                    fallback_reason="empty_rewritten_query",
                )

            # If identical, mark is_rewritten as False
            if rewritten_text == clean_query:
                is_rewritten = False

            # Rough estimate of tokens used
            approx_tokens = (len(user_prompt) + len(raw_resp)) // 2

            return QueryRewriteResult(
                original_query=query,
                rewritten_query=rewritten_text if is_rewritten else query,
                is_rewritten=is_rewritten,
                duration_ms=round(duration, 2),
                tokens_used=approx_tokens,
                fallback=False,
            )

        except asyncio.TimeoutError:
            duration = (time.perf_counter() - start_time) * 1000
            logger.warning(f"QueryRewriter timed out after {self.timeout}s, falling back to original query.")
            return QueryRewriteResult(
                original_query=query,
                rewritten_query=query,
                is_rewritten=False,
                duration_ms=round(duration, 2),
                fallback=True,
                fallback_reason="timeout",
            )
        except Exception as exc:
            duration = (time.perf_counter() - start_time) * 1000
            logger.error(f"QueryRewriter failed with exception: {exc}, falling back to original query.", exc_info=True)
            return QueryRewriteResult(
                original_query=query,
                rewritten_query=query,
                is_rewritten=False,
                duration_ms=round(duration, 2),
                fallback=True,
                fallback_reason=str(exc),
            )

    def _parse_json_response(self, text: str) -> Optional[Dict[str, Any]]:
        """Safely extract and parse JSON from LLM output (supporting markdown fences)."""
        clean = text.strip()
        # Strip markdown fences if present
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean, flags=re.IGNORECASE)
            clean = re.sub(r"\s*```$", "", clean)
            clean = clean.strip()

        try:
            return json.loads(clean)
        except json.JSONDecodeError:
            # Try to search for innermost {...}
            match = re.search(r"\{.*\}", clean, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
            return None
