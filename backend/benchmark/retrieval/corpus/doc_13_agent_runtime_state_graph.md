# LangGraph 智能体运行时与状态图决策规范

## 1. 深度分析模式定位与自主决策循环
AgentStudio-RAG 针对复杂企业知识排障场景提供了「🔬 深度分析」执行模式，其核心底层由 LangGraph 智能体框架驱动。
与「快速问答」所采用的固定前向工作流（Workflow）不同，深度分析模式赋予大模型自主分析、规划提问、调用外部工具并根据观察结果进行多步推理的能力。
系统将 Agent 执行建模为一个有向有环状态图（StateGraph）：
智能体在每一步思考中判断当前已有信息是否足以回答用户问题，若不足则自主输出工具调用指令；工具执行完毕后将观察结果反馈给智能体，形成“思考-行动-观察”（Think-Act-Observe）的自主决策循环。

## 2. AgentState 状态模式与消息累加器
LangGraph Agent 运行时的核心是强类型的统一状态模式 `AgentState`（定义于 `app/agent/runtime/state.py`）。
`AgentState` 严格管控以下 6 类关键上下文数据：
- `messages`：包含用户原始问题、模型思考决策（AIMessage）及工具返回结果（ToolMessage）的对话全序列，采用 LangGraph 的 `add_messages` 还原器进行安全追加；
- `run_id`、`conversation_id`、`knowledge_base_id`：执行生命周期与多租户边界标识；
- `evidence`：当前运行生命周期内收集的全部结构化客观事实切片；
- `execution_steps` 与 `tool_call_count`：步数与调用计数器；
- `status`：运行生命周期枚举（running, completed, failed, max_steps_reached）；
- `trace_events`：向前端推送的实时时间轴事件。

## 3. 双重熔断机制与防止死循环策略
在智能体自主推理过程中，大模型极易由于遇到歧义问题、幻觉或无有效信息的知识库而陷入反复调用相同工具的逻辑死锁（Dead Loop），无谓消耗用户 Token 与响应时间。
系统在条件路由函数 `route_agent_decision`（位于 `app/agent/runtime/routing.py`）中设计了严格的双重熔断守护：
- 步骤上限熔断：配置参数 `max_steps`（默认值为 5），一旦 Agent 循环执行步数达到 5 步，路由函数强制分流截断；
- 工具调用次数熔断：配置参数 `max_tool_calls`（默认值为 10），一旦累计发起的工具调用总数达到 10 次，立即阻断后续调用。
触发熔断后，流程自动流向 `force_stop_node`，强制注入总结指令引导大模型依据已有信息诚实给出最终结论，坚决杜绝无限循环。

## 4. 原生 Function Calling 协议适配
系统完全基于标准的 OpenAI Function Calling 规范设计工具交互协议，使智能体能够透明适配各类主流云端商业大模型（如 DeepSeek-V3/R1、GPT-4o）与本地开源模型。
每个工具类继承自 `BaseTool`，通过 Pydantic 规范自动导出包含 `name`、`description` 及 JSON Schema 格式的参数规格定义。
在 `agent_node` 执行时，系统动态抓取当前 Agent 处于启用状态的工具清单并序列化为 `tools` 参数注入大模型生成接口；
模型在生成文本的同时能够以结构化 JSON 的形式返回期望调用的工具名称与实参字典，规避了基于纯正则解析模型文本提示词的脆弱脆弱性。

## 5. 工具节点安全上下文覆盖（Security Context Override）
由于大模型本身具有不可预测的生成特性，外部攻击者可能通过构造带有恶意提示词注入（Prompt Injection）的提问，诱导模型在调用工具时篡改敏感参数（例如诱导模型查询其他租户的 knowledge_base_id）。
系统在 `tool_node`（位于 `app/agent/runtime/nodes.py`）层设计了严密的安全防线：
工具节点在真正派发执行前，首先执行工具名称白名单校验，拦截任何非配置工具；
随后，系统强制使用当前用户会话上下文中受信任的系统级 `knowledge_base_id` 和 `conversation_id` 物理覆盖模型生成的对应入参，彻底剥夺大模型自主越权访问非授权资源的可能性。
