import re
from typing import Dict, List, Optional

from app.core.logging import logger
from app.routing.schemas import ExecutionPath, RouteDecision, UserIntent


class IntentRouter:
    """
    High-performance, rule-and-context-based Intent Router for AgentStudio-RAG.

    Core Principles:
    1. Conservative Routing:
       Resource availability (e.g. KB selected or attachments uploaded) does NOT mandate resource usage.
       External retrieval is only triggered when the user explicitly or contextually requires external data.
    2. Sub-millisecond Latency:
       Pure regex and heuristic rule matching with 0 external LLM classification calls (0 token cost).
    3. Clean Intent & Execution Decoupling:
       User intent (DIRECT_CHAT, KB, ATTACHMENT, HYBRID, AGENT) and execution mode (workflow, agentic)
       are cleanly decoupled. A generic question asked in Agent mode still routes to DIRECT_LLM.
    """

    # 1. Chitchat, greetings, self-introduction, courtesy
    CHITCHAT_PATTERNS = [
        re.compile(r"^(你好|您好|哈喽|嗨|hello|hi|hey|good\s+morning|good\s+afternoon|good\s+evening)[啊呀吧]?[！!。.\s]*$", re.IGNORECASE),
        re.compile(r"^(早上好|下午好|晚上好|早安|午安|晚安)[！!。.\s]*$", re.IGNORECASE),
        re.compile(r"^(谢谢|多谢|感谢|thx|thanks|thank\s+you)[您你]?[！!。.\s]*$", re.IGNORECASE),
        re.compile(r"^(你是谁|介绍一下你自己|你叫什么名字|你的自我介绍|介绍下自己)[？?！!。.\s]*$", re.IGNORECASE),
        re.compile(r"^(再见|拜拜|bye|goodbye|see\s+you)[！!。.\s]*$", re.IGNORECASE),
        re.compile(r"^(测试|test|ping)[！!。.\s]*$", re.IGNORECASE),
    ]

    # 2. Conversational Followup referencing prior turns
    FOLLOWUP_PATTERNS = [
        re.compile(r"(你)?(刚才|刚刚|上面|前边|前面|上文)(说|提到|写|讲|回答|给出)的", re.IGNORECASE),
        re.compile(r"^(继续|接着)(说|讲|解释|写|分析|回答|往下说)?([！!。.\s]*)$", re.IGNORECASE),
        re.compile(r"(我)?(上一轮|上一条|上一句|上一问)(问了什么|说了什么)", re.IGNORECASE),
        re.compile(r"(详细|展开)(说说|讲讲|解释一下)?(刚才|上面|前面|第[一二三四两12345\d]+[点个种条项])", re.IGNORECASE),
        re.compile(r"上面提到的第[一二三四两12345\d]+[种点个条项]", re.IGNORECASE),
        re.compile(r"^第[一二三四两12345\d]+[种点个条项](呢|是什么|怎么实现|怎么配置)", re.IGNORECASE),
        re.compile(r"(换一种说法|再详细点|太简略了|能不能更具体点|换个思路)", re.IGNORECASE),
        re.compile(r"^(继续|然后呢|接着呢|还有吗|展开讲讲)[？?！!。.\s]*$", re.IGNORECASE),
    ]

    # 3. Generic programming, open-source technology, standard CS concepts & algorithms
    GENERIC_TECH_PATTERNS = [
        re.compile(r"(python|java|javascript|typescript|golang|c\+\+|rust|php|ruby|swift|kotlin|sql|html|css|docker|kubernetes|k8s|linux|git|react|vue|node\.?js|useeffect|usestate)", re.IGNORECASE),
        re.compile(r"(装饰器|闭包|生成器|迭代器|元组|列表|字典|类|多继承|反射|异步编程|asyncio|多线程|多进程|协程|gil|死锁|互斥锁|并发|内存泄漏|垃圾回收)", re.IGNORECASE),
        re.compile(r"(transformer|self-attention|自注意力|cnn|rnn|lstm|bert|gpt|注意力机制|激活函数|梯度下降|反向传播|过拟合|欠拟合)", re.IGNORECASE),
        re.compile(r"(快速排序|冒泡排序|归并排序|二分查找|二叉树|红黑树|平衡树|动态规划|贪心算法|广度优先|深度优先|链表|堆栈|队列|哈希表|复杂度)", re.IGNORECASE),
        re.compile(r"(tcp|udp|三次握手|四次挥手|http|https|websocket|restful|dns|osi七层|socket|状态码|cookie|session|jwt)", re.IGNORECASE),
        re.compile(r"^(写一段|写一个|帮我写|编写|实现).*(代码|函数|脚本|算法|排序|demo|示例)", re.IGNORECASE),
    ]

    # 4. Explicit Knowledge Base and Enterprise Document markers
    KB_PATTERNS = [
        re.compile(r"(企业知识库|知识库|内部文档|内部规范|架构规范|技术规范|设计规范|内部标准|公司规章|业务知识|\bwiki\b|\bkb\b)", re.IGNORECASE),
        re.compile(r"(根据|按照|参考|对照|查一下|查看|检索|查询|搜索)?.*(企业规范|内部规范|公司规范|团队规范|架构标准)", re.IGNORECASE),
        re.compile(r"(企业|内部|系统|平台).*(网关超时|三态熔断|连接池|链路追踪|鉴权规范|脱敏规则|限流配置)", re.IGNORECASE),
        re.compile(r"\b(err-[a-zA-Z0-9]+-\d+)\b", re.IGNORECASE),
    ]

    # 5. Explicit Attachment and User Upload markers
    ATT_PATTERNS = [
        re.compile(r"(附件|文件|文档|材料|代码|日志).*(中|里|内|提取|总结|概括|参数|错误码)", re.IGNORECASE),
        re.compile(r"(我上传的|刚才上传的|上传的|已上传的|当前上传的).*(文件|文档|pdf|docx|txt|md|压缩包)?", re.IGNORECASE),
        re.compile(r"(这个|这份|当前|那份).*(文件|文档|pdf|docx|txt|附件)", re.IGNORECASE),
        re.compile(r"\b(pdf|docx|markdown)\b", re.IGNORECASE),
    ]

    # 6. Cross-Source Hybrid markers
    HYBRID_PATTERNS = [
        re.compile(r"(对比|比较|对照|结合|核对).*(上传|附件|文件).*(知识库|内部规范|企业标准)", re.IGNORECASE),
        re.compile(r"(对比|比较|对照|结合|核对).*(知识库|内部规范|企业标准).*(上传|附件|文件)", re.IGNORECASE),
        re.compile(r"(同时|分别).*(查|检索|参考).*(知识库|规范).*(和|与|以及).*(附件|文件)", re.IGNORECASE),
    ]

    # 7. Complex Agent Task markers
    AGENT_TASK_PATTERNS = [
        re.compile(r"(分步骤|多步|逐步|综合规划|迁移方案|迁移计划|深度调研|调研报告|多工具|跨系统综合)", re.IGNORECASE),
        re.compile(r"(自动调用工具|多步分析并制定|全流程排查|端到端综合评估)", re.IGNORECASE),
    ]

    def route(
        self,
        query: str,
        has_knowledge_base: bool = False,
        has_completed_attachments: bool = False,
        history_messages: Optional[List[Dict[str, str]]] = None,
        requested_mode: Optional[str] = "workflow",
    ) -> RouteDecision:
        """
        Evaluate user intent and return a RouteDecision.
        """
        q = (query or "").strip()
        q_lower = q.lower()

        # ----------------------------------------------------------------------
        # 0. Explicit Direct Chat Mode -> DIRECT_LLM
        # ----------------------------------------------------------------------
        if requested_mode == "direct":
            return RouteDecision(
                intent=UserIntent.DIRECT_CHAT,
                execution_path=ExecutionPath.DIRECT_LLM,
                required_sources=[],
                reason="用户显式选择直接对话模式 (mode='direct')，无需外部检索",
                should_retrieve=False,
                should_use_agent=False,
                confidence=1.0,
            )

        # Check explicit indicators first
        explicit_kb = any(p.search(q_lower) for p in self.KB_PATTERNS)
        explicit_att = any(p.search(q_lower) for p in self.ATT_PATTERNS)
        explicit_hybrid = any(p.search(q_lower) for p in self.HYBRID_PATTERNS) or (explicit_kb and explicit_att)

        # ----------------------------------------------------------------------
        # 1. Chitchat & Greetings -> DIRECT_LLM
        # ----------------------------------------------------------------------
        for p in self.CHITCHAT_PATTERNS:
            if p.search(q_lower):
                return RouteDecision(
                    intent=UserIntent.DIRECT_CHAT,
                    execution_path=ExecutionPath.DIRECT_LLM,
                    required_sources=[],
                    reason="用户问候/礼貌性交流/自我介绍，无需外部检索",
                    should_retrieve=False,
                    should_use_agent=False,
                    confidence=1.0,
                )

        # ----------------------------------------------------------------------
        # 2. Conversational Follow-up -> DIRECT_LLM (Preserves history)
        # ----------------------------------------------------------------------
        is_followup = any(p.search(q_lower) for p in self.FOLLOWUP_PATTERNS)
        if is_followup and not explicit_kb and not explicit_att:
            return RouteDecision(
                intent=UserIntent.CONVERSATION_FOLLOWUP,
                execution_path=ExecutionPath.DIRECT_LLM,
                required_sources=[],
                reason="当前会话上下文追问/指代前文，利用会话历史直接由大模型回答，无需外部检索",
                should_retrieve=False,
                should_use_agent=False,
                confidence=0.95,
            )

        # ----------------------------------------------------------------------
        # 3. Missing Attachment Degrade -> DIRECT_LLM (Only when no KB is bound)
        # ----------------------------------------------------------------------
        if not has_knowledge_base and explicit_att and not has_completed_attachments:
            return RouteDecision(
                intent=UserIntent.DIRECT_CHAT,
                execution_path=ExecutionPath.DIRECT_LLM,
                required_sources=[],
                reason="用户提问提及附件但当前会话未上传可用附件，请先上传附件后再针对附件提问，避免直接编造答案",
                should_retrieve=False,
                should_use_agent=False,
                confidence=0.90,
            )

        # ----------------------------------------------------------------------
        # 4. Pure Generic Technical Concept / Programming -> DIRECT_LLM
        # (Only applied when NO knowledge base is bound. When KB is bound, default to RAG)
        # ----------------------------------------------------------------------
        if not has_knowledge_base:
            is_generic_tech = any(p.search(q_lower) for p in self.GENERIC_TECH_PATTERNS)
            if is_generic_tech and not explicit_kb and not explicit_att:
                return RouteDecision(
                    intent=UserIntent.DIRECT_CHAT,
                    execution_path=ExecutionPath.DIRECT_LLM,
                    required_sources=[],
                    reason="未选择知识库且属于通用技术概念/编程语言/基础算法常识问答，无需外部检索，直接由大模型回答",
                    should_retrieve=False,
                    should_use_agent=False,
                    confidence=0.95,
                )

        # ----------------------------------------------------------------------
        # 5. Complex Agent Task (When requested_mode == 'agentic')
        # ----------------------------------------------------------------------
        is_agent_task = any(p.search(q_lower) for p in self.AGENT_TASK_PATTERNS)
        if requested_mode == "agentic" and is_agent_task:
            sources = []
            if has_knowledge_base:
                sources.append("knowledge_base")
            if has_completed_attachments:
                sources.append("attachments")
            return RouteDecision(
                intent=UserIntent.COMPLEX_AGENT,
                execution_path=ExecutionPath.AGENT,
                required_sources=sources,
                reason="用户任务需要多步规划与综合调研分析，进入 LangGraph Agent Runtime",
                should_retrieve=bool(sources),
                should_use_agent=True,
                confidence=0.92,
            )

        # ----------------------------------------------------------------------
        # 6. Cross-Source Hybrid (KB + Attachment)
        # ----------------------------------------------------------------------
        if has_knowledge_base and has_completed_attachments:
            if explicit_att and not explicit_kb:
                if requested_mode == "agentic":
                    return RouteDecision(
                        intent=UserIntent.ATTACHMENT,
                        execution_path=ExecutionPath.AGENT,
                        required_sources=["attachments"],
                        reason="用户提问明确针对当前会话附件，在 Agent 模式下启用附件检索工具",
                        should_retrieve=True,
                        should_use_agent=True,
                        confidence=0.95,
                    )
                return RouteDecision(
                    intent=UserIntent.ATTACHMENT,
                    execution_path=ExecutionPath.ATTACHMENT,
                    required_sources=["attachments"],
                    reason="用户提问明确针对当前会话附件，调用会话附件检索服务",
                    should_retrieve=True,
                    should_use_agent=False,
                    confidence=0.95,
                )

            if explicit_kb and not explicit_att:
                if requested_mode == "agentic":
                    return RouteDecision(
                        intent=UserIntent.KNOWLEDGE_BASE,
                        execution_path=ExecutionPath.AGENT,
                        required_sources=["knowledge_base"],
                        reason="用户明确需要企业知识库/内部技术规范，在 Agent 模式下启用知识库检索工具",
                        should_retrieve=True,
                        should_use_agent=True,
                        confidence=0.95,
                    )
                return RouteDecision(
                    intent=UserIntent.KNOWLEDGE_BASE,
                    execution_path=ExecutionPath.KNOWLEDGE_BASE,
                    required_sources=["knowledge_base"],
                    reason="用户明确需要企业知识库/内部技术规范，调用 Workflow RAG 检索链路",
                    should_retrieve=True,
                    should_use_agent=False,
                    confidence=0.95,
                )

            # Both resources available and question is domain-specific
            if requested_mode == "agentic":
                return RouteDecision(
                    intent=UserIntent.HYBRID,
                    execution_path=ExecutionPath.AGENT,
                    required_sources=["knowledge_base", "attachments"],
                    reason="当前会话同时具备知识库与附件且处于 Agent 模式，启用双工具协同推理",
                    should_retrieve=True,
                    should_use_agent=True,
                    confidence=0.95,
                )
            return RouteDecision(
                intent=UserIntent.HYBRID,
                execution_path=ExecutionPath.HYBRID,
                required_sources=["knowledge_base", "attachments"],
                reason="当前会话同时具备知识库与附件，执行双源联合检索",
                should_retrieve=True,
                should_use_agent=False,
                confidence=0.95,
            )

        # ----------------------------------------------------------------------
        # 7. Attachment Only
        # ----------------------------------------------------------------------
        if has_completed_attachments:
            if requested_mode == "agentic":
                return RouteDecision(
                    intent=UserIntent.ATTACHMENT,
                    execution_path=ExecutionPath.AGENT,
                    required_sources=["attachments"],
                    reason="当前会话具备可用附件，在 Agent 模式下启用附件检索工具",
                    should_retrieve=True,
                    should_use_agent=True,
                    confidence=0.95,
                )
            return RouteDecision(
                intent=UserIntent.ATTACHMENT,
                execution_path=ExecutionPath.ATTACHMENT,
                required_sources=["attachments"],
                reason="当前会话具备可用附件，调用会话附件检索服务",
                should_retrieve=True,
                should_use_agent=False,
                confidence=0.95,
            )

        # ----------------------------------------------------------------------
        # 8. Knowledge Base Only
        # ----------------------------------------------------------------------
        if has_knowledge_base:
            if requested_mode == "agentic":
                return RouteDecision(
                    intent=UserIntent.KNOWLEDGE_BASE,
                    execution_path=ExecutionPath.AGENT,
                    required_sources=["knowledge_base"],
                    reason="当前已选择企业知识库，在 Agent 模式下启用知识库检索工具",
                    should_retrieve=True,
                    should_use_agent=True,
                    confidence=0.95,
                )
            return RouteDecision(
                intent=UserIntent.KNOWLEDGE_BASE,
                execution_path=ExecutionPath.KNOWLEDGE_BASE,
                required_sources=["knowledge_base"],
                reason="当前已选择企业知识库，调用 Workflow RAG 检索链路",
                should_retrieve=True,
                should_use_agent=False,
                confidence=0.95,
            )

        # ----------------------------------------------------------------------
        # 9. No Knowledge Base & No Attachments
        # ----------------------------------------------------------------------
        if requested_mode == "workflow":
            # In workflow mode without resources, route to WorkflowRAGEngine to emit polite guidance
            return RouteDecision(
                intent=UserIntent.KNOWLEDGE_BASE,
                execution_path=ExecutionPath.KNOWLEDGE_BASE,
                required_sources=[],
                reason="工作流模式下未指定知识库且无可用附件，引导用户选择知识库或上传附件",
                should_retrieve=False,
                should_use_agent=False,
                confidence=0.90,
            )

        return RouteDecision(
            intent=UserIntent.DIRECT_CHAT,
            execution_path=ExecutionPath.DIRECT_LLM,
            required_sources=[],
            reason="问题未指定外部资料或属于通用问答，直接由大模型回答",
            should_retrieve=False,
            should_use_agent=False,
            confidence=0.90,
        )


_router_instance: Optional[IntentRouter] = None


def get_intent_router() -> IntentRouter:
    """Singleton getter for IntentRouter."""
    global _router_instance
    if _router_instance is None:
        _router_instance = IntentRouter()
    return _router_instance
