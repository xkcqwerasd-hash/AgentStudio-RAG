import glob
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.core.exceptions import ContextBudgetExceededError, QueryTooLongError
from app.core.logging import logger
from app.retrieval.vector_store import ScoredChunk
from app.schemas.chat import ContextBudgetInfo


class TokenEstimator:
    """
    Robust multilingual token estimator supporting HuggingFace Tokenizer
    with conservative heuristic fallback for CJK and Latin text.
    """

    def __init__(self):
        self.tokenizer = None
        self.method = "conservative_heuristic"
        self._init_tokenizer()

    def _init_tokenizer(self):
        # 1. Try tiktoken if available
        try:
            import tiktoken
            self.tokenizer = tiktoken.get_encoding("cl100k_base")
            self.method = "tiktoken"
            return
        except Exception:
            pass

        # 2. Try fastembed / HuggingFace tokenizer in temp cache if available
        try:
            from tokenizers import Tokenizer
            cache_pattern = (
                os.environ.get("LOCALAPPDATA", "")
                + r"\Temp\fastembed_cache\**\tokenizer.json"
            )
            matches = glob.glob(cache_pattern, recursive=True)
            if matches:
                self.tokenizer = Tokenizer.from_file(matches[0])
                self.method = "tokenizers_hf"
                return
        except Exception:
            pass

        self.method = "conservative_heuristic"

    def estimate(self, text: str) -> int:
        if not text:
            return 0

        # Method 1: tiktoken
        if self.method == "tiktoken" and self.tokenizer is not None:
            try:
                return len(self.tokenizer.encode(text))
            except Exception:
                pass

        # Method 2: HuggingFace Tokenizer
        if self.method == "tokenizers_hf" and self.tokenizer is not None:
            try:
                enc = self.tokenizer.encode(text)
                return max(1, len(enc.ids))
            except Exception:
                pass

        # Method 3: High-accuracy conservative heuristic
        return self._heuristic_estimate(text)

    def _heuristic_estimate(self, text: str) -> int:
        """
        Conservative model-adapted BPE token count estimation:
        - CJK characters: 1 token each (conservative)
        - English/Latin words: ~1.2 tokens per word or ~1 token per 3.5 chars
        - Punctuation & symbols: 1 token each
        - Numbers: 1 token per 2 digits
        """
        if not text:
            return 0

        cjk_chars = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]", text))
        latin_words = re.findall(r"[a-zA-Z_]+", text)
        latin_tokens = sum(max(1, int(len(w) / 3.5 + 0.6)) for w in latin_words)
        numbers = re.findall(r"\d+", text)
        number_tokens = sum(max(1, int(len(n) / 2.0 + 0.5)) for n in numbers)
        symbols = len(re.findall(r"[^\w\s\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]", text))
        whitespace_tokens = len(re.findall(r"\n+", text))

        total = cjk_chars + latin_tokens + number_tokens + symbols + whitespace_tokens
        return max(1, total)


# Global estimator singleton
_estimator = TokenEstimator()


def estimate_tokens(text: str) -> int:
    return _estimator.estimate(text)


class BudgetPlan(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    kept_history: List[Dict[str, str]]
    kept_chunks: List[ScoredChunk]
    budget_info: ContextBudgetInfo
    memory_tokens: int = 0


class ContextBudgetManager:
    """
    Unified Token Budget Management for AgentStudio-RAG (M2.4).
    Controls context budget across:
    - Model Context Window
    - Safety Margin & Reserved Output Tokens
    - System Prompt
    - User Query (Highest Priority)
    - Dynamic Retrieval Chunks (Relevance Priority)
    - Dynamic Conversation History (Turn-Pair Priority)
    """

    def __init__(
        self,
        model_context_window: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        context_safety_margin: Optional[int] = None,
        system_prompt_budget: Optional[int] = None,
        history_token_budget: Optional[int] = None,
        retrieval_token_budget: Optional[int] = None,
        max_query_tokens: Optional[int] = None,
        memory_token_budget: Optional[int] = None,
    ):
        self.model_context_window = (
            model_context_window
            if model_context_window is not None
            else settings.MODEL_CONTEXT_WINDOW
        )
        self.max_output_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else settings.MAX_OUTPUT_TOKENS
        )
        self.context_safety_margin = (
            context_safety_margin
            if context_safety_margin is not None
            else settings.CONTEXT_SAFETY_MARGIN
        )
        self.system_prompt_budget = (
            system_prompt_budget
            if system_prompt_budget is not None
            else settings.SYSTEM_PROMPT_BUDGET
        )
        self.history_token_budget = (
            history_token_budget
            if history_token_budget is not None
            else settings.HISTORY_TOKEN_BUDGET
        )
        self.retrieval_token_budget = (
            retrieval_token_budget
            if retrieval_token_budget is not None
            else settings.RETRIEVAL_TOKEN_BUDGET
        )
        self.max_query_tokens = (
            max_query_tokens
            if max_query_tokens is not None
            else settings.MAX_QUERY_TOKENS
        )
        self.memory_token_budget = (
            memory_token_budget
            if memory_token_budget is not None
            else getattr(settings, "MEMORY_TOKEN_BUDGET", 512)
        )

        self.estimator = _estimator

    @property
    def available_input_budget(self) -> int:
        """Total usable tokens for input prompt after reserving output and safety margin."""
        net = self.model_context_window - self.max_output_tokens - self.context_safety_margin
        return max(512, net)

    def validate_user_query(self, query: str) -> int:
        """
        Ensure user query does not exceed safety threshold.
        Current user query has highest protection priority and must not be silently truncated.
        """
        query_tokens = self.estimator.estimate(query)
        if query_tokens > self.max_query_tokens:
            raise QueryTooLongError(query_tokens, self.max_query_tokens)
        return query_tokens

    def prune_history_for_rewriter(
        self,
        history_messages: List[Dict[str, str]],
        max_tokens: int = 2048,
        max_turns: int = 5,
    ) -> List[Dict[str, str]]:
        """
        Dedicated history pruning for QueryRewriter (M2.3).
        Preserves up to max_turns within max_tokens.
        """
        if not history_messages:
            return []

        # Clean interrupted markers
        cleaned_msgs = []
        for m in history_messages:
            role = m.get("role", "")
            raw_c = m.get("content", "").strip()
            c = (
                raw_c.replace("\n\n[回答已被用户中止]", "")
                .replace("[回答已被用户中止]", "")
                .strip()
            )
            if c:
                cleaned_msgs.append({"role": role, "content": c})

        # Group into turns
        turns = self._group_messages_into_turns(cleaned_msgs)
        kept_turns: List[List[Dict[str, str]]] = []
        accumulated_tokens = 0

        for turn in reversed(turns):
            if len(kept_turns) >= max_turns:
                break
            turn_text = " ".join(m["content"] for m in turn)
            t_tokens = self.estimator.estimate(turn_text)
            if accumulated_tokens + t_tokens <= max_tokens or not kept_turns:
                kept_turns.insert(0, turn)
                accumulated_tokens += t_tokens
            else:
                break

        flat: List[Dict[str, str]] = []
        for t in kept_turns:
            flat.extend(t)
        return flat

    def prune_history(
        self,
        history_messages: List[Dict[str, str]],
        budget: int,
    ) -> Tuple[List[Dict[str, str]], int, int, int]:
        """
        Prune historical conversation messages in turn-pair units:
        - Drops oldest turns first.
        - Preserves user+assistant pairs together.
        - Never splits a turn unless forced.
        Returns: (kept_messages, history_tokens, kept_count, pruned_count)
        """
        if not history_messages or budget <= 0:
            return [], 0, 0, len(history_messages)

        # 1. Clean interrupted markers
        cleaned_msgs = []
        for m in history_messages:
            role = m.get("role", "")
            raw_c = m.get("content", "").strip()
            c = (
                raw_c.replace("\n\n[回答已被用户中止]", "")
                .replace("[回答已被用户中止]", "")
                .strip()
            )
            if c:
                cleaned_msgs.append({"role": role, "content": c})

        if not cleaned_msgs:
            return [], 0, 0, len(history_messages)

        # 2. Group into complete turns (pairs of user + assistant)
        turns = self._group_messages_into_turns(cleaned_msgs)

        # 3. Add turns starting from the most recent backward
        kept_turns: List[List[Dict[str, str]]] = []
        accumulated_tokens = 0

        for turn in reversed(turns):
            turn_text = "\n".join(f"{m['role']}: {m['content']}" for m in turn)
            turn_tokens = self.estimator.estimate(turn_text)

            if accumulated_tokens + turn_tokens <= budget:
                kept_turns.insert(0, turn)
                accumulated_tokens += turn_tokens
            else:
                # If budget is too tight to fit even one turn, preserve at least 1 turn if possible
                if not kept_turns and budget >= 50:
                    kept_turns.insert(0, turn)
                    accumulated_tokens += turn_tokens
                break

        flat_kept: List[Dict[str, str]] = []
        for t in kept_turns:
            flat_kept.extend(t)

        pruned_count = len(history_messages) - len(flat_kept)
        return flat_kept, accumulated_tokens, len(flat_kept), max(0, pruned_count)

    def prune_retrieval_chunks(
        self,
        chunks: List[ScoredChunk],
        budget: int,
    ) -> Tuple[List[ScoredChunk], int, int, int]:
        """
        Prune retrieval chunks according to similarity score ranking and token budget:
        - Preserves highest scored chunks first.
        - Drops lower scored chunks when exceeding budget.
        - Avoids cutting chunks mid-sentence.
        - The resulting list defines the authoritative [Ref-X] citation mapping.
        Returns: (kept_chunks, retrieval_tokens, kept_count, pruned_count)
        """
        if not chunks or budget <= 0:
            return [], 0, 0, len(chunks)

        # Ensure chunks are sorted by similarity_score descending
        sorted_chunks = sorted(chunks, key=lambda c: c.similarity_score, reverse=True)

        kept: List[ScoredChunk] = []
        accumulated_tokens = 0

        for idx, chunk in enumerate(sorted_chunks, start=1):
            src_type = getattr(chunk, "source_type", None)
            if src_type and src_type != "knowledge_base":
                from app.attachments.budget import AttachmentBudgetManager
                prov = AttachmentBudgetManager._format_chunk_provenance(chunk)
            else:
                prov = f"页码: 第 {chunk.page_number} 页"

            # Estimate chunk formatted as it appears in prompt
            sample_block = (
                f"[Ref-{idx}] 来源文件: {chunk.document_name} | {prov} | 相关度: {chunk.similarity_score}\n"
                f"{chunk.content.strip()}"
            )
            block_tokens = self.estimator.estimate(sample_block)

            if accumulated_tokens + block_tokens <= budget:
                kept.append(chunk)
                accumulated_tokens += block_tokens
            else:
                # If not even 1 chunk fits into budget, keep at least the top-1 chunk if budget > 0
                if not kept and budget >= 50:
                    kept.append(chunk)
                    accumulated_tokens += block_tokens
                break

        pruned_count = len(chunks) - len(kept)
        return kept, accumulated_tokens, len(kept), pruned_count

    def allocate_and_assemble(
        self,
        query: str,
        system_prompt: str,
        raw_history_messages: List[Dict[str, str]],
        retrieved_chunks: List[ScoredChunk],
        memory_tokens: int = 0,
        memories_kept: Optional[int] = None,
        memories_pruned: Optional[int] = None,
    ) -> BudgetPlan:
        """
        Allocate unified token budget and assemble optimized context:
        1. Protect System Prompt, User Query, and Injected Memory.
        2. Dynamically allocate remaining input budget between Retrieval and History.
        3. Prune history (turn-pair preserving) and chunks (relevance preserving).
        4. Validate final prompt against input budget.
        """
        input_budget = self.available_input_budget

        # 1. Mandatory budgets
        system_prompt_tokens = self.estimator.estimate(system_prompt)
        query_tokens = self.validate_user_query(query)

        # Template overhead for formatting instructions
        prompt_template_overhead = 120

        # 2. Dynamic budget pool for context (retrieval + history)
        available_context_budget = (
            input_budget
            - system_prompt_tokens
            - query_tokens
            - memory_tokens
            - prompt_template_overhead
        )
        if available_context_budget < 256:
            raise ContextBudgetExceededError(
                total_tokens=system_prompt_tokens + query_tokens + memory_tokens + prompt_template_overhead,
                budget=input_budget,
            )

        # Target budgets from settings (capped by available pool)
        target_retrieval = min(self.retrieval_token_budget, int(available_context_budget * 0.65))
        target_history = min(self.history_token_budget, int(available_context_budget * 0.35))

        # First pass: prune retrieval chunks with target retrieval budget
        kept_chunks, ret_tokens, chunks_kept, chunks_pruned = self.prune_retrieval_chunks(
            retrieved_chunks, target_retrieval
        )

        # Slack transfer: if retrieval used less than target, give remainder to history
        slack_from_retrieval = max(0, target_retrieval - ret_tokens)
        effective_history_budget = min(
            target_history + slack_from_retrieval,
            available_context_budget - ret_tokens,
        )

        # Second pass: prune history with effective history budget
        kept_history, hist_tokens, msgs_kept, msgs_pruned = self.prune_history(
            raw_history_messages, effective_history_budget
        )

        # Total estimated input tokens
        total_estimated = (
            system_prompt_tokens
            + query_tokens
            + memory_tokens
            + ret_tokens
            + hist_tokens
            + prompt_template_overhead
        )

        budget_info = ContextBudgetInfo(
            model_context_window=self.model_context_window,
            input_budget=input_budget,
            estimated_input_tokens=total_estimated,
            actual_input_tokens=None,
            reserved_output_tokens=self.max_output_tokens,
            safety_margin=self.context_safety_margin,
            system_prompt_tokens=system_prompt_tokens,
            history_tokens=hist_tokens,
            retrieval_tokens=ret_tokens,
            query_tokens=query_tokens,
            memory_tokens=memory_tokens or 0,
            memories_kept=memories_kept or 0,
            memories_pruned=memories_pruned or 0,
            history_messages_kept=msgs_kept,
            history_messages_pruned=msgs_pruned,
            retrieval_chunks_kept=chunks_kept,
            retrieval_chunks_pruned=chunks_pruned,
            budget_estimation_method=self.estimator.method,
        )

        return BudgetPlan(
            kept_history=kept_history,
            kept_chunks=kept_chunks,
            budget_info=budget_info,
            memory_tokens=memory_tokens,
        )

    def _group_messages_into_turns(
        self, messages: List[Dict[str, str]]
    ) -> List[List[Dict[str, str]]]:
        """
        Group flat message list into logical conversation turns.
        A turn is typically [user_msg, assistant_msg] or a single message.
        """
        turns: List[List[Dict[str, str]]] = []
        current_turn: List[Dict[str, str]] = []

        for m in messages:
            role = m.get("role", "")
            if role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [m]
            elif role == "assistant":
                if current_turn and current_turn[0].get("role") == "user":
                    current_turn.append(m)
                    turns.append(current_turn)
                    current_turn = []
                else:
                    turns.append([m])
            else:
                if current_turn:
                    current_turn.append(m)
                else:
                    turns.append([m])

        if current_turn:
            turns.append(current_turn)

        return turns
