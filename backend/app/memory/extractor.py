import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import logger
from app.db.session import async_session_factory
from app.llm.base import BaseLLMClient
from app.llm.openai_client import get_llm_client
from app.memory.manager import MemoryManager
from app.models.memory import (
    Memory,
    MemoryExtractionLog,
    MemorySourceType,
    MemoryType,
)

EXTRACTION_SYSTEM_PROMPT = """你是一个长期记忆提取专家。你的职责是从用户对话中提取值得长期保存的信息。

【提取类型与定义】：
1. profile: 用户画像与稳定偏好（如编程语言偏好、排版习惯、工作流风格）。
2. background: 长期项目背景、学习或工作经历、正在研发的系统架构。
3. fact: 明确的技术事实与项目事实（如项目技术栈、版本约束、系统部署环境）。
4. rule: 用户明确要求遵守的长期约定与不可违背规则（如“所有响应必须包含 Trace-ID”）。

【严格提取原则】：
1. 真实来源：只能将用户真实明确表达的内容作为记忆来源，严禁将 AI 助手的推测、解释或补充作为用户事实！
2. 过滤临时信息：普通问候、一次性知识问答、临时调试问题、短期状态等绝不提取！
3. 过滤敏感信息：API Key、密码、Token、私钥等凭据绝不提取！
4. 修正处理：如果用户明确声明修改、推翻或不再使用先前的方案，请标明 action 为 "update" 并指出旧事实关键词。

【输出格式】：
严格返回 JSON 对象列表，格式如下：
[
  {
    "content": "高浓缩记忆事实描述",
    "memory_type": "profile" | "background" | "fact" | "rule",
    "action": "create" | "update",
    "supersedes_keyword": "如果是修正旧事实，提供旧事实关键词（可选）",
    "confidence": 0.95
  }
]
如果没有值得长期保存的信息，必须返回空列表 []。不要输出任何 JSON 之外的文字。"""


# Regex rules for early pre-filtering
GREETING_PATTERNS = [
    re.compile(r"^(你好|您好|hi|hello|hey|早上好|下午好|晚上好|在吗|在么|哈喽|嗨)[\s!！?？.~。]*$", re.IGNORECASE),
    re.compile(r"^(谢谢|感谢|多谢|thx|thanks|thank you)[\s!！?？.~。]*$", re.IGNORECASE),
    re.compile(r"^(再见|拜拜|bye|goodbye)[\s!！?？.~。]*$", re.IGNORECASE),
    re.compile(r"^(收到|好的|明白|ok|okay)[\s!！?？.~。]*$", re.IGNORECASE),
]

GENERAL_KNOWLEDGE_PATTERNS = [
    re.compile(r"^什么是[\u4e00-\u9fa5a-zA-Z0-9_\+\#\s]+[？?]?$"),
    re.compile(r"^解释一下[\u4e00-\u9fa5a-zA-Z0-9_\+\#\s]+$"),
    re.compile(r"^[\u4e00-\u9fa5a-zA-Z0-9_\+\#\s]+是什么(意思)?[？?]?$"),
    re.compile(r"^(请)?介绍一下[\u4e00-\u9fa5a-zA-Z0-9_\+\#\s]+$"),
    re.compile(r"^(如何理解|简述)[\u4e00-\u9fa5a-zA-Z0-9_\+\#\s]+$"),
    re.compile(r"^为什么[\u4e00-\u9fa5a-zA-Z0-9_\+\#\s]+[？?]?$"),
]

CANDIDATE_MEMORY_PATTERNS = [
    re.compile(r"(请记住|记住|以后请|务必|必须|设定规则|制定规则)", re.IGNORECASE),
    re.compile(r"(我正在|我们正在|我目前|我们目前|我的项目|我们项目|我的团队|我们团队)", re.IGNORECASE),
    re.compile(r"(我的技术栈|我们的技术栈|采用.*架构|使用.*开发|技术选型)", re.IGNORECASE),
    re.compile(r"(我的偏好|我偏好|我习惯|我喜欢|我的工作|我主要用|我平时用)", re.IGNORECASE),
    re.compile(r"(已经不使用|不再使用|改用|换成|更正一下|修正为|把.*替换为|不再采用)", re.IGNORECASE),
]

# Sensitive credentials that must NEVER be extracted into memory
SENSITIVE_PATTERNS = [
    re.compile(r"(?:sk-[a-zA-Z0-9_\-]{20,}|ghp_[a-zA-Z0-9]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z\-_]{35})"),
    re.compile(r"(?:Bearer\s+ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}|st_ey[A-Za-z0-9_\-]{10,})"),
    re.compile(r"(?i)(?:password|passwd|pwd|secret|私钥|密码)\s*[:=]\s*[^\s]{4,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


class MemoryExtractor:
    """
    Automatic Long-term Memory Extractor for AgentStudio-RAG (P1.2 Stage 4).

    Core features:
    1. Zero-cost heuristic pre-filtering (filters greetings, chitchat, and general knowledge questions).
    2. Sensitive credentials interception (API keys, tokens, passwords, private keys).
    3. Idempotent background task execution tied to Message.id.
    4. Safe deduplication & fact conflict resolution (superseding old memories on explicit user update).
    5. Tenant-scoped user control (auto-memory toggle per owner_id).
    """

    def __init__(
        self,
        llm_client: Optional[BaseLLMClient] = None,
    ):
        self.llm_client = llm_client

    @staticmethod
    def should_extract(query: str, answer: Optional[str] = None) -> bool:
        """
        Fast heuristic check to decide whether this conversation turn warrants
        calling the LLM memory extraction pipeline.
        Eliminates 100% of LLM calls for greetings and one-off knowledge questions.
        """
        q = (query or "").strip()
        if not q or len(q) < 2:
            return False

        # 1. Immediate rejection for greetings and chitchat
        for pattern in GREETING_PATTERNS:
            if pattern.search(q):
                return False

        # 2. Check candidate long-term memory signals
        has_candidate_signal = any(p.search(q) for p in CANDIDATE_MEMORY_PATTERNS)

        # 3. If it looks like a one-off general knowledge question and lacks self-referential context, skip
        for pattern in GENERAL_KNOWLEDGE_PATTERNS:
            if pattern.search(q) and not has_candidate_signal:
                return False

        # If query has explicit candidate signals or is substantive non-greeting statement, proceed
        return has_candidate_signal or len(q) >= 15

    @staticmethod
    def contains_sensitive_credential(text: str) -> bool:
        """
        Check whether the candidate text contains any sensitive credentials.
        """
        if not text:
            return False
        return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)

    @staticmethod
    def normalize_memory_type(raw_type: str) -> str:
        """
        Map candidate type string to canonical MemoryType enum.
        """
        t = (raw_type or "").strip().lower()
        if t in ("profile", "user_profile"):
            return MemoryType.USER_PROFILE
        if t in ("background", "project_context", "context"):
            return MemoryType.PROJECT_CONTEXT
        if t in ("rule", "direct_rule"):
            return MemoryType.DIRECT_RULE
        return MemoryType.IMPORTANT_FACT

    async def extract_from_conversation(
        self,
        user_id: str,
        query: str,
        answer: str,
        conversation_id: Optional[str],
        message_id: str,
        db: AsyncSession,
    ) -> List[Memory]:
        """
        Execute end-to-end memory extraction for a completed conversation turn.
        Guarantees idempotency via message_id logging and enforces tenant boundaries.
        """
        manager = MemoryManager(db)

        # 1. Check user memory settings (tenant-scoped)
        user_setting = await manager.get_user_setting(user_id)
        if not user_setting.auto_memory_enabled:
            logger.info(f"Auto-memory is disabled for user {user_id}. Skipping extraction.")
            log_entry = MemoryExtractionLog(
                message_id=message_id,
                conversation_id=conversation_id,
                owner_id=user_id,
                status="DISABLED",
                extracted_count=0,
                reason="auto_memory_disabled",
            )
            db.add(log_entry)
            await db.commit()
            return []

        # 2. Idempotency check: ensure this message_id hasn't been extracted already
        stmt = select(MemoryExtractionLog).where(MemoryExtractionLog.message_id == message_id)
        existing_log = (await db.execute(stmt)).scalar_one_or_none()
        if existing_log:
            logger.info(f"Extraction already executed for message {message_id} (status: {existing_log.status}). Skipping.")
            return []

        # 3. Pre-filtering: fast heuristic check
        if not self.should_extract(query, answer):
            logger.info(f"Message {message_id} skipped by heuristic pre-filter (greeting or general knowledge).")
            log_entry = MemoryExtractionLog(
                message_id=message_id,
                conversation_id=conversation_id,
                owner_id=user_id,
                status="SKIPPED",
                extracted_count=0,
                reason="no_candidate_memory_triggers",
            )
            db.add(log_entry)
            await db.commit()
            return []

        # 4. Invoke LLM to extract candidate memories
        llm = self.llm_client or get_llm_client()
        candidates_raw: List[Dict[str, Any]] = []

        prompt = f"""【长期记忆提取任务】
【用户提问】：
{query}

【助手回答】：
{answer}

请仅根据用户明确陈述的事实、偏好或规则进行提炼。如果没有，输出 []。输出 JSON："""

        try:
            raw_output = await llm.generate(
                prompt=prompt,
                system_prompt=EXTRACTION_SYSTEM_PROMPT,
                temperature=0.0,
            )
            # Clean possible markdown fence blocks ```json ... ```
            cleaned_json = raw_output.strip()
            if cleaned_json.startswith("```"):
                cleaned_json = re.sub(r"^```(?:json)?\s*", "", cleaned_json)
                cleaned_json = re.sub(r"\s*```$", "", cleaned_json)
            cleaned_json = cleaned_json.strip()

            if cleaned_json and cleaned_json != "[]":
                candidates_raw = json.loads(cleaned_json)
                if not isinstance(candidates_raw, list):
                    candidates_raw = [candidates_raw]
        except Exception as exc:
            logger.warning(f"LLM memory extraction call failed for message {message_id}: {exc}")
            log_entry = MemoryExtractionLog(
                message_id=message_id,
                conversation_id=conversation_id,
                owner_id=user_id,
                status="FAILED",
                extracted_count=0,
                reason=f"llm_error: {str(exc)[:100]}",
            )
            db.add(log_entry)
            await db.commit()
            return []

        # 5. Process candidate memories (Sensitive check, deduplication, conflict update)
        saved_memories: List[Memory] = []
        for cand in candidates_raw:
            content = str(cand.get("content", "")).strip()
            if not content or len(content) < 3:
                continue

            # Sensitive credential interception
            if self.contains_sensitive_credential(content) or self.contains_sensitive_credential(query):
                logger.warning(f"Security: Intercepted and rejected sensitive credential in candidate memory for user {user_id}.")
                continue

            mem_type = self.normalize_memory_type(str(cand.get("memory_type", "fact")))
            confidence = float(cand.get("confidence", 0.95))
            action = str(cand.get("action", "create")).lower()
            supersedes_keyword = cand.get("supersedes_keyword")

            # Check if this memory explicitly supersedes/corrects an existing memory
            superseded = False
            if action == "update" or supersedes_keyword:
                # Find candidate memory to supersede for this user
                keyword = supersedes_keyword or ""
                stmt_existing = select(Memory).where(
                    Memory.owner_id == user_id,
                    Memory.is_active == True,
                )
                existing_items = (await db.execute(stmt_existing)).scalars().all()
                for em in existing_items:
                    if keyword and keyword.lower() in em.content.lower():
                        await manager.supersede_memory(
                            user_id=user_id,
                            target_memory_id=em.id,
                            new_content=content,
                            confidence=confidence,
                        )
                        saved_memories.append(em)
                        superseded = True
                        logger.info(f"Superseded memory {em.id} with new fact for user {user_id}")
                        break

            if not superseded:
                mem = await manager.create_memory(
                    user_id=user_id,
                    content=content,
                    memory_type=mem_type,
                    source_type=MemorySourceType.AUTO_EXTRACTED,
                    source_conversation_id=conversation_id,
                    source_message_id=message_id,
                    confidence=confidence,
                )
                saved_memories.append(mem)

        # 6. Record successful extraction log
        log_entry = MemoryExtractionLog(
            message_id=message_id,
            conversation_id=conversation_id,
            owner_id=user_id,
            status="EXTRACTED",
            extracted_count=len(saved_memories),
            reason="success" if saved_memories else "no_facts_extracted",
        )
        db.add(log_entry)
        await db.commit()

        logger.info(
            f"Successfully completed memory extraction for message {message_id}: "
            f"{len(saved_memories)} memories created/updated for user {user_id}."
        )
        return saved_memories


# Asynchronous background runner for FastAPI BackgroundTasks
async def run_memory_extraction_task(
    user_id: str,
    query: str,
    answer: str,
    conversation_id: Optional[str],
    message_id: str,
    db_session: Optional[AsyncSession] = None,
) -> None:
    """
    Background worker executed asynchronously after message persistence.
    Uses an isolated DB session so failures never affect chat responses.
    """
    try:
        if db_session is not None:
            extractor = MemoryExtractor()
            await extractor.extract_from_conversation(
                user_id=user_id,
                query=query,
                answer=answer,
                conversation_id=conversation_id,
                message_id=message_id,
                db=db_session,
            )
        else:
            async with async_session_factory() as session:
                extractor = MemoryExtractor()
                await extractor.extract_from_conversation(
                    user_id=user_id,
                    query=query,
                    answer=answer,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    db=session,
                )
    except Exception as exc:
        logger.warning(
            f"Background memory extraction encountered unhandled error for message {message_id}: {exc}"
        )
