import asyncio
import json
import re
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Union
import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.core.config import settings
from app.core.exceptions import LLMNotConfiguredError
from app.core.ssrf import SSRFBlockedException, get_ssrf_safe_async_client
from app.llm.base import BaseLLMClient


def messages_to_openai_payload(
    messages: List[Union[Dict[str, Any], BaseMessage]]
) -> List[Dict[str, Any]]:
    """Convert a mixed sequence of BaseMessage / dict into OpenAI-compatible messages."""
    payload_messages: List[Dict[str, Any]] = []
    for m in messages:
        if isinstance(m, dict):
            payload_messages.append(m)
        elif isinstance(m, BaseMessage):
            role = "user"
            msg_type = getattr(m, "type", "human")
            if msg_type == "human":
                role = "user"
            elif msg_type == "ai":
                role = "assistant"
            elif msg_type == "system":
                role = "system"
            elif msg_type == "tool":
                role = "tool"

            entry: Dict[str, Any] = {"role": role}
            if role == "tool":
                entry["content"] = str(m.content)
                entry["tool_call_id"] = getattr(m, "tool_call_id", "")
            elif role == "assistant":
                entry["content"] = m.content if m.content else None
                tool_calls = getattr(m, "tool_calls", None)
                if tool_calls:
                    raw_tcs = []
                    for tc in tool_calls:
                        args = tc.get("args", {})
                        arg_str = (
                            json.dumps(args, ensure_ascii=False)
                            if isinstance(args, dict)
                            else str(args)
                        )
                        raw_tcs.append({
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": tc.get("name"),
                                "arguments": arg_str,
                            },
                        })
                    entry["tool_calls"] = raw_tcs
            else:
                entry["content"] = str(m.content)
            payload_messages.append(entry)
    return payload_messages


class OpenAILLMClient(BaseLLMClient):
    """
    OpenAI-compatible LLM Client (Supports DeepSeek, SiliconFlow, DashScope, OpenAI, Ollama).
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 60.0,
        allow_local: Optional[bool] = None,
        trusted_hosts: Optional[Set[str]] = None,
    ):
        self.api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
        self.base_url = (base_url or settings.OPENAI_BASE_URL).rstrip("/")
        self.model = model or settings.LLM_MODEL
        self.timeout = timeout
        self.last_usage: Optional[dict] = None
        self.allow_local = (settings.DEPLOYMENT_MODE == "local") if allow_local is None else allow_local
        self.trusted_hosts = trusted_hosts

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        # Check if API key is provided
        if not self.api_key and not self.base_url.startswith("http://localhost"):
            raise LLMNotConfiguredError(
                "LLM API key is not configured. Please set OPENAI_API_KEY in backend/.env"
            )

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else settings.LLM_TEMPERATURE,
            "max_tokens": max_tokens or settings.LLM_MAX_TOKENS,
        }

        try:
            async with get_ssrf_safe_async_client(
                timeout=self.timeout,
                allow_local=self.allow_local,
                trusted_hosts=self.trusted_hosts,
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"LLM API returned HTTP {resp.status_code}: {resp.text}")
                data = resp.json()
                self.last_usage = data.get("usage")
                return data["choices"][0]["message"]["content"]
        except SSRFBlockedException as exc:
            raise RuntimeError(f"SSRF 安全策略拦截: {exc.detail}") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(f"Failed to connect to LLM provider at {self.base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"LLM call timed out after {self.timeout}s: {exc}") from exc

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        if not self.api_key and not self.base_url.startswith("http://localhost"):
            raise LLMNotConfiguredError(
                "LLM API key is not configured. Please set OPENAI_API_KEY in backend/.env"
            )

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else settings.LLM_TEMPERATURE,
            "max_tokens": max_tokens or settings.LLM_MAX_TOKENS,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        self.last_usage = None

        try:
            async with get_ssrf_safe_async_client(
                timeout=self.timeout,
                allow_local=self.allow_local,
                trusted_hosts=self.trusted_hosts,
            ) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        err_text = await resp.aread()
                        raise RuntimeError(
                            f"LLM API returned HTTP {resp.status_code}: {err_text.decode('utf-8', errors='replace')}"
                        )
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        line = line.strip()
                        if line.startswith("data: "):
                            data_str = line[6:].strip()
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                                if "usage" in chunk and chunk["usage"]:
                                    self.last_usage = chunk["usage"]
                                choices = chunk.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content")
                                    if content:
                                        yield content
                            except json.JSONDecodeError:
                                continue
        except SSRFBlockedException as exc:
            raise RuntimeError(f"SSRF 安全策略拦截: {exc.detail}") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(f"Failed to connect to LLM provider at {self.base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"LLM streaming call timed out after {self.timeout}s: {exc}") from exc

    async def generate_with_tools(
        self,
        messages: List[Union[Dict[str, Any], BaseMessage]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Union[str, Dict[str, Any]] = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AIMessage:
        if not self.api_key and not self.base_url.startswith("http://localhost"):
            raise LLMNotConfiguredError(
                "LLM API key is not configured. Please set OPENAI_API_KEY in backend/.env"
            )

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        payload_messages = messages_to_openai_payload(messages)
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": temperature if temperature is not None else settings.LLM_TEMPERATURE,
            "max_tokens": max_tokens or settings.LLM_MAX_TOKENS,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        try:
            async with get_ssrf_safe_async_client(
                timeout=self.timeout,
                allow_local=self.allow_local,
                trusted_hosts=self.trusted_hosts,
            ) as client:
                resp = await client.post(url, json=payload, headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"LLM API returned HTTP {resp.status_code}: {resp.text}")
                data = resp.json()
                self.last_usage = data.get("usage")

                choice = data["choices"][0]
                msg_data = choice.get("message", {})
                content = msg_data.get("content") or ""
                finish_reason = choice.get("finish_reason", "stop")

                parsed_tool_calls: List[Dict[str, Any]] = []
                invalid_tool_calls: List[Dict[str, Any]] = []
                for tc in msg_data.get("tool_calls", []):
                    tc_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
                    fn = tc.get("function", {})
                    fn_name = fn.get("name", "")
                    raw_args = fn.get("arguments", "{}")
                    try:
                        parsed_args = (
                            json.loads(raw_args)
                            if isinstance(raw_args, str)
                            else raw_args
                        )
                        if not isinstance(parsed_args, dict):
                            parsed_args = {"_value": parsed_args}
                        parsed_tool_calls.append({
                            "name": fn_name,
                            "args": parsed_args,
                            "id": tc_id,
                            "type": "tool_call",
                        })
                    except Exception as json_err:
                        invalid_tool_calls.append({
                            "name": fn_name,
                            "args": raw_args,
                            "id": tc_id,
                            "error": str(json_err),
                        })

                return AIMessage(
                    content=content,
                    tool_calls=parsed_tool_calls,
                    invalid_tool_calls=invalid_tool_calls,
                    response_metadata={
                        "finish_reason": finish_reason,
                        "usage": self.last_usage,
                        "model": data.get("model", self.model),
                    },
                )
        except SSRFBlockedException as exc:
            raise RuntimeError(f"SSRF 安全策略拦截: {exc.detail}") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(f"Failed to connect to LLM provider at {self.base_url}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"LLM call timed out after {self.timeout}s: {exc}") from exc


class TestDoubleLLMClient(BaseLLMClient):
    """
    Deterministic test double for LLM in CI/unit tests or when running without API keys.
    Generates grounded answers based directly on provided prompt and context markers.
    Supports scripted responses or rule-based autonomous tool calling.
    """
    __test__ = False

    def __init__(self, model: Optional[str] = None, scripted_responses: Optional[List[Any]] = None):
        self.model: str = model or settings.LLM_MODEL
        self._scripted_responses: List[Union[AIMessage, Exception]] = list(scripted_responses) if scripted_responses else []

    def set_responses(self, responses: List[Union[AIMessage, Exception]]) -> None:
        """Queue scripted responses or exceptions for deterministic testing."""
        self._scripted_responses = list(responses)

    async def generate_with_tools(
        self,
        messages: List[Union[Dict[str, Any], BaseMessage]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Union[str, Dict[str, Any]] = "auto",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AIMessage:
        if self._scripted_responses:
            item = self._scripted_responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        # Default fallback logic for unscripted calls
        if not messages:
            return AIMessage(content="请提供提问内容。")

        last_msg = messages[-1]
        last_role = getattr(last_msg, "type", None) or (
            last_msg.get("role") if isinstance(last_msg, dict) else None
        )
        last_content = (
            getattr(last_msg, "content", "")
            if hasattr(last_msg, "content")
            else (last_msg.get("content", "") if isinstance(last_msg, dict) else "")
        )

        available_tools = [
            t.get("function", {}).get("name") for t in (tools or []) if isinstance(t, dict)
        ]

        # Extract user query from message history
        user_queries = [
            getattr(m, "content", "") for m in messages
            if (isinstance(m, HumanMessage) or (isinstance(m, dict) and m.get("role") == "user"))
        ]
        user_prompt = user_queries[-1] if user_queries else str(last_content)

        # Check prior tool calls executed in this run
        called_tools = []
        for m in messages:
            if isinstance(m, ToolMessage) or (isinstance(m, dict) and m.get("role") == "tool"):
                tool_name = getattr(m, "name", None) or (m.get("name") if isinstance(m, dict) else "")
                if tool_name:
                    called_tools.append(tool_name)

        # If last message is ToolMessage (Observation received)
        if last_role == "tool" or (
            isinstance(last_msg, dict) and last_msg.get("role") == "tool"
        ):
            content_str = str(last_content)

            # Check if this is a consecutive two-tool call request (e.g. cross-source comparison)
            is_combined_query = any(k in user_prompt for k in ["联合", "对比", "两个工具", "跨来源", "同时", "分别", "比较"])
            if is_combined_query:
                if (
                    "search_conversation_attachments" in called_tools
                    and "search_knowledge_base" not in called_tools
                    and "search_knowledge_base" in available_tools
                ):
                    return AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "search_knowledge_base",
                            "args": {
                                "knowledge_base_id": "test-kb-default",
                                "query": user_prompt,
                                "top_k": 4,
                            },
                            "id": "call_test_double_kb_02",
                            "type": "tool_call",
                        }],
                        response_metadata={"finish_reason": "tool_calls"},
                    )
                elif (
                    "search_knowledge_base" in called_tools
                    and "search_conversation_attachments" not in called_tools
                    and "search_conversation_attachments" in available_tools
                ):
                    return AIMessage(
                        content="",
                        tool_calls=[{
                            "name": "search_conversation_attachments",
                            "args": {
                                "query": user_prompt,
                                "top_k": 4,
                            },
                            "id": "call_test_double_att_02",
                            "type": "tool_call",
                        }],
                        response_metadata={"finish_reason": "tool_calls"},
                    )

            # Find all Ref-X in all tool messages in history
            all_tool_contents = [
                str(getattr(m, "content", "")) for m in messages
                if isinstance(m, ToolMessage) or (isinstance(m, dict) and m.get("role") == "tool")
            ]
            full_obs_text = " ".join(all_tool_contents)
            found_refs = sorted(list(set(re.findall(r"\[Ref-(\d+)\]", full_obs_text))), key=lambda x: int(x))

            if "error" in content_str.lower() and "security rejection" in content_str.lower():
                return AIMessage(
                    content=f"检索执行遇到权限拦截：{content_str}。无法越权访问其他会话附件。"
                )
            if "error" in content_str.lower() or "not found" in content_str.lower():
                return AIMessage(
                    content=f"检索执行遇到异常或未找到证据：{content_str}。无法基于检索给出确切结论。"
                )

            if found_refs:
                lines = [f"根据参考资料 [Ref-{found_refs[0]}]，已完成技术事实核验与分析。"]
                if len(found_refs) > 1:
                    lines.append(f"同时结合参考资料 [Ref-{found_refs[1]}]，进一步补充了上下文细节。")
                return AIMessage(content="\n\n".join(lines))

            return AIMessage(
                content="根据检索结果，已完成技术分析并形成结论。"
            )

        # If last message is Human / User
        prompt_text = str(last_content)
        if (
            "无需工具" in prompt_text
            or "直接回答" in prompt_text
            or "常识" in prompt_text
        ):
            return AIMessage(content="这是直接生成的最终回答，无需调用工具。")

        # Determine which tool to call based on user query and available tools
        is_kb_query = any(k in prompt_text for k in ["知识库", "企业知识", "业务知识", "规章制度", "公司知识", "wiki", "kb"])
        prefers_attachment = any(
            k in prompt_text.lower()
            for k in [
                "附件", "文件", "上传", ".pdf", ".docx", ".txt",
                ".md", "markdown", "白皮书", "金丝雀",
            ]
        )
        if is_kb_query and "search_knowledge_base" in available_tools:
            prefers_attachment = False

        if "search_conversation_attachments" in available_tools and (
            prefers_attachment or "search_knowledge_base" not in available_tools
        ):
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "search_conversation_attachments",
                    "args": {
                        "query": prompt_text,
                        "top_k": 4,
                    },
                    "id": "call_test_double_att_01",
                    "type": "tool_call",
                }],
                response_metadata={"finish_reason": "tool_calls"},
            )

        if "search_knowledge_base" in available_tools:
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "search_knowledge_base",
                    "args": {
                        "knowledge_base_id": "test-kb-default",
                        "query": prompt_text,
                        "top_k": 4,
                    },
                    "id": "call_test_double_kb_01",
                    "type": "tool_call",
                }],
                response_metadata={"finish_reason": "tool_calls"},
            )

        return AIMessage(content="系统已接收您的提问并完成分析。")

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        # Query Rewriter test double logic
        if "【查询重写任务】" in prompt or (system_prompt and "Query Rewriter" in system_prompt):
            if "测试非法JSON" in prompt:
                return "THIS_IS_NOT_VALID_JSON"
            if "测试超时" in prompt:
                await asyncio.sleep(15.0)
                return "{}"
            if "测试空结果" in prompt:
                return json.dumps({
                    "is_rewritten": True,
                    "rewritten_query": "",
                    "reason": "测试空结果",
                }, ensure_ascii=False)
            if "那最小是多少" in prompt:
                return json.dumps({
                    "is_rewritten": True,
                    "rewritten_query": "网关连接池的最小空闲连接数是多少？",
                    "reason": "补全主语'网关连接池'与宾语'最小空闲连接数'",
                }, ensure_ascii=False)
            if "它什么时候进入 OPEN 状态" in prompt:
                return json.dumps({
                    "is_rewritten": True,
                    "rewritten_query": "系统三态熔断器在什么条件下进入 OPEN 状态？",
                    "reason": "补全代词'它'为'系统三态熔断器'",
                }, ensure_ascii=False)
            if "第二种状态什么时候触发" in prompt:
                return json.dumps({
                    "is_rewritten": True,
                    "rewritten_query": "熔断器的 OPEN 状态在什么条件下触发？",
                    "reason": "将序号'第二种状态'解析为'OPEN 状态'",
                }, ensure_ascii=False)
            if "那和 BGE-M3 有什么区别" in prompt or "BGE-M3" in prompt:
                return json.dumps({
                    "is_rewritten": True,
                    "rewritten_query": "BGE-small-zh-v1.5 与 BAAI/BGE-M3 语义向量模型有什么区别？",
                    "reason": "结合多轮历史补充对比主体'BGE-small-zh-v1.5'",
                }, ensure_ascii=False)
            if "Python 的装饰器是什么" in prompt or "Python装饰器" in prompt or "独立问题" in prompt:
                return json.dumps({
                    "is_rewritten": False,
                    "rewritten_query": "Python 的装饰器是什么？",
                    "reason": "独立问题，无需重写",
                }, ensure_ascii=False)

            match = re.search(r"【当前用户问题】：\s*(.+?)\s*请严格按照", prompt, re.DOTALL)
            q = match.group(1).strip() if match else "测试问题"
            return json.dumps({
                "is_rewritten": False,
                "rewritten_query": q,
                "reason": "测试模式默认保留原问题",
            }, ensure_ascii=False)

        # Long-term Memory Extractor test double logic (P1.2 Stage 4)
        if "【长期记忆提取任务】" in prompt or (system_prompt and "长期记忆提取专家" in system_prompt):
            m = re.search(r"【用户提问】：\s*(.+?)\s*【助手回答】", prompt, re.DOTALL)
            user_text = m.group(1).strip() if m else prompt.strip()

            # Check greetings or simple general knowledge
            if any(k in user_text for k in ["你好", "早上好", "在吗", "谢谢", "再见", "什么是 C++ 多态", "什么是C++多态", "Python 装饰器"]):
                return "[]"

            # Check explicit correction
            if any(k in user_text for k in ["不再使用", "改用", "换成", "修正为", "把原来的架构换了"]):
                supersede_kw = "FastAPI" if "FastAPI" in prompt or "架构" in user_text else None
                return json.dumps([{
                    "content": user_text,
                    "memory_type": "background",
                    "action": "update",
                    "supersedes_keyword": supersede_kw,
                    "confidence": 0.95,
                }], ensure_ascii=False)

            # Check user preference / profile
            if any(k in user_text for k in ["偏好", "习惯", "我喜欢"]):
                return json.dumps([{
                    "content": user_text,
                    "memory_type": "profile",
                    "action": "create",
                    "confidence": 0.95,
                }], ensure_ascii=False)

            # Check direct rule
            if any(k in user_text for k in ["请记住", "规则", "必须", "规范", "不可违背"]):
                return json.dumps([{
                    "content": user_text,
                    "memory_type": "rule",
                    "action": "create",
                    "confidence": 0.95,
                }], ensure_ascii=False)

            # Check project background
            if any(k in user_text for k in ["项目", "正在开发", "系统架构", "技术栈", "金融信贷"]):
                return json.dumps([{
                    "content": user_text,
                    "memory_type": "background",
                    "action": "create",
                    "confidence": 0.95,
                }], ensure_ascii=False)

            # General fact fallback
            return json.dumps([{
                "content": user_text,
                "memory_type": "fact",
                "action": "create",
                "confidence": 0.90,
            }], ensure_ascii=False)

        if "未检索到" in prompt or "没有相关证据" in prompt or "无参考资料" in prompt:
            return "根据当前知识库检索结果，未找到与您提问相关的充分事实证据，无法给出确切回答。"

        # If prompt contains references like [Ref-1], summarize and cite them
        ref_count = prompt.count("[Ref-")
        if ref_count > 0:
            lines = [
                f"根据知识库参考资料 [Ref-1]，为您解答如下：",
                "系统相关说明已在检索片段中清晰阐述。以上事实依据均已严格标注来源。",
            ]
            if ref_count > 1:
                lines.append(f"同时结合资料 [Ref-2] 补充了进一步的上下文规范。")
            return "\n\n".join(lines)

        return "系统已接收您的提问并完成分析。"

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncIterator[str]:
        full_text = await self.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        chunk_size = 4
        for i in range(0, len(full_text), chunk_size):
            yield full_text[i : i + chunk_size]
            await asyncio.sleep(0.01)


_test_double_singleton: Optional[TestDoubleLLMClient] = None


def get_llm_client(provider: Optional[str] = None) -> BaseLLMClient:
    p = provider or settings.LLM_PROVIDER
    if p == "test_double":
        global _test_double_singleton
        if _test_double_singleton is None:
            _test_double_singleton = TestDoubleLLMClient()
        return _test_double_singleton
    elif p == "openai":
        return OpenAILLMClient()
    else:
        raise ValueError(f"Unknown LLM provider: {p}")
