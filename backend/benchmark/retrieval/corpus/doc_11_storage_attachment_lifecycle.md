# 会话临时附件存储、解析与物理生命周期规范

## 1. 临时附件定位与知识库解耦原则
在智能问答交互场景中，用户经常需要就特定的一份或多份临时文档（如突发生产故障排查报告、临时变更草案）发起即时问答。
系统确立了临时附件与企业持久化知识库彻底解耦的架构原则：临时附件绝不直接打入持久化向量数据库。
这一设计主要基于三个考量：
首先，临时附件具有明显的会话私有属性，将其持久化至共享知识库会严重污染企业公共检索索引；
其次，调用 GPU 或云端 API 对多兆字节的临时文件进行全量 Embedding 向量化会引入数十秒的不可容忍延迟，并消耗高额 Token 成本；
最后，通过会话级物理隔离，用户在关闭或删除会话时能够实现临时文件的安全物理擦除。

## 2. 文件上传白名单与物理存储路径
系统在附件上传网关层配置了严格的安全校验准入规则。
附件格式仅允许扩展名为 `.pdf`、`.docx`、`.txt`、`.md` 的常见文档类型，严格拦截包含可执行代码或宏脚本的危险文件（如 `.exe`、`.bat`、`.docm`）。
单文件物理大小上限严格限制为 20MB，单次对话会话允许上传挂载的有效附件数量上限配置为 10 个。
校验通过的物理文件统一落盘至持久化存储路径 `data/attachments/{conversation_id}/{attachment_id}.{ext}`，目录结构以会话 ID 为第一级命名空间，确保物理层面的天然隔离。

## 3. 双轨自适应上下文装配策略（Dual-Track）
在用户发起针对附件的提问时，附件检索服务 AttachmentRetrievalService 执行智能的双轨预算装配（Dual-Track Strategy）。
系统首先对已解析的附件文本计算总预估 Token 数：
当会话中所有附件的总 Token 数小于等于全文保留阈值 full_text_threshold_tokens（系统默认配置为 4000 tokens）且在当前模型可用输入预算内时，自动触发 Full-Text Track（全文保留轨）。
在全文保留轨下，系统不对文档进行截断或抽取，而是直接将附件完整正文格式化装入 Prompt，彻底消除跨段落上下文丢失；
反之，当附件总容量超过 4000 tokens 时，系统自动无缝降级切换至 BM25 Retrieval Track（切片检索轨），仅提取与当前提问最相关的 Top-K 切片装配上下文。

## 4. 内存级会话隔离倒排索引（EphemeralBM25Index）
在触发切片检索轨时，系统依赖自研的会话级内存倒排索引 EphemeralBM25Index 执行毫秒级关键词检索。
该索引直接在当前应用进程内存中基于 AttachmentChunker 切分后的内存切片动态构建，完全避开任何磁盘 I/O 与网络开销。
索引构建采用与知识库相同的 TechnicalBM25Tokenizer 进行技术分词，并使用 Lucene 平滑非负 IDF 公式计算相关性。
由于倒排表直接驻留于以 `conversation_id` 为键的内存管理器中，检索耗时通常在 2 毫秒以内，真正做到了“零向量化成本、秒级检索响应”。

## 5. 级联物理销毁与内存索引失效机制
为严格保障用户会话数据的隐私安全，系统建立了确定性的物理销毁与内存回收机制。
当用户在前端主动点击删除会话（`DELETE /api/v1/conversations/{id}`）时，系统在数据库事务中级联删除关联的 `ConversationAttachment` ORM 记录。
事务提交后，系统同步触发 `delete_conversation_physical_attachments`，在操作系统层强制递归删除 `data/attachments/{conversation_id}` 目录下的全部物理文件，杜绝残留孤儿文件；
同时，通知 `attachment_index_manager.invalidate_conversation(conversation_id)` 瞬间释放内存中的 EphemeralBM25Index 对象，彻底回收应用物理内存。
