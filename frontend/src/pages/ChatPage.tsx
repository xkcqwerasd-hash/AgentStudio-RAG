import React, { useEffect, useState, useRef, useMemo } from 'react';
import {
  Button,
  Select,
  Space,
  Typography,
  Tag,
  Avatar,
  message,
  Tooltip,
  Spin,
  Popconfirm,
  Segmented,
} from 'antd';
import {
  UserOutlined,
  RobotOutlined,
  FileTextOutlined,
  PlusOutlined,
  DeleteOutlined,
  ClockCircleOutlined,
  RightOutlined,
  PaperClipOutlined,
  MessageOutlined,
  DatabaseOutlined,
  BulbOutlined,
} from '@ant-design/icons';
import {
  getConversations,
  getConversationMessages,
  deleteConversation,
  createConversation,
  sendChatMessageStream,
  sendAgentRunStream,
  SSEChatEvent,
  SSEAgentRunEvent,
} from '../api/chat';
import { getKnowledgeBases } from '../api/knowledge';
import { getActiveModels } from '../api/models';
import {
  uploadAttachments,
  getAttachments,
  deleteAttachment,
} from '../api/attachment';
import {
  ChatMessage,
  Conversation,
  KnowledgeBase,
  Citation,
  AgentExecutionTimeline,
  ActiveModelItem,
  ConversationAttachment,
} from '../types';
import { CitationDrawer } from '../components/CitationDrawer';
import { AgentTimelineCard } from '../components/AgentTimelineCard';
import { AttachmentPreviewDrawer } from '../components/AttachmentPreviewDrawer';
import { MarkdownRenderer } from '../components/MarkdownRenderer';
import { ChatComposer } from '../components/ChatComposer';
import { MemoryDrawer } from '../components/MemoryDrawer';
import { getMemoryStats } from '../api/memory';

const { Text, Paragraph, Title } = Typography;

export const ChatPage: React.FC = () => {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<string>('');
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentConvId, setCurrentConvId] = useState<string>('');
  const [executionMode, setExecutionMode] = useState<'workflow' | 'agentic'>('workflow');
  const [activeModels, setActiveModels] = useState<ActiveModelItem[]>([]);
  const [selectedModelId, setSelectedModelId] = useState<string>('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [inputQuery, setInputQuery] = useState<string>('');
  const [sending, setSending] = useState<boolean>(false);
  const [streamingStatus, setStreamingStatus] = useState<string>('');
  const abortControllerRef = useRef<AbortController | null>(null);

  // Citation Drawer state
  const [drawerOpen, setDrawerOpen] = useState<boolean>(false);
  const [activeCitations, setActiveCitations] = useState<Citation[]>([]);
  const [selectedCitationIndex, setSelectedCitationIndex] = useState<number | undefined>(undefined);

  // Attachment state (P0.3)
  const [attachments, setAttachments] = useState<ConversationAttachment[]>([]);
  const [uploading, setUploading] = useState<boolean>(false);
  const [previewDrawerOpen, setPreviewDrawerOpen] = useState<boolean>(false);
  const [previewAttachmentId, setPreviewAttachmentId] = useState<string | undefined>(undefined);

  // Long-term Memory state (P1.2 Stage 4)
  const [memoryDrawerOpen, setMemoryDrawerOpen] = useState<boolean>(false);
  const [memoryCount, setMemoryCount] = useState<number>(0);

  const refreshMemoryCount = () => {
    getMemoryStats()
      .then((s) => setMemoryCount(s.total_memories))
      .catch(() => {});
  };

  useEffect(() => {
    refreshMemoryCount();
  }, []);

  // Cleanup in-flight SSE streams on component unmount (e.g. account switch or logout)
  useEffect(() => {
    return () => {
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
        abortControllerRef.current = null;
      }
    };
  }, []);

  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, sending]);

  // Load knowledge bases and conversations on mount
  useEffect(() => {
    getKnowledgeBases().then((kbs) => {
      setKnowledgeBases(kbs);
      if (kbs.length > 0) {
        setSelectedKbId(kbs[0].id);
      }
    });

    getActiveModels().then((models) => {
      setActiveModels(models);
      const def = models.find((m) => m.is_default) || models[0];
      if (def) {
        setSelectedModelId(def.id);
      }
    }).catch((err) => console.error('Failed to load active models', err));

    loadConversations(true);
  }, []);

  const loadConversations = async (autoSelectFirst: boolean = false) => {
    try {
      const convs = await getConversations();
      setConversations(convs);
      if (autoSelectFirst && convs.length > 0 && !currentConvId) {
        handleSelectConversation(convs[0]);
      }
    } catch (err: any) {
      console.error(err);
    }
  };

  const handleStop = () => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    setSending(false);
    setStreamingStatus('');
  };

  const handleSelectConversation = async (conv: Conversation) => {
    if (sending) {
      handleStop();
    }
    setCurrentConvId(conv.id);
    if (conv.knowledge_base_id) {
      setSelectedKbId(conv.knowledge_base_id);
    } else {
      setSelectedKbId('');
    }
    if (conv.mode === 'agentic') {
      setExecutionMode('agentic');
    } else {
      setExecutionMode('workflow');
    }
    try {
      const [msgs, atts] = await Promise.all([
        getConversationMessages(conv.id),
        getAttachments(conv.id).catch(() => []),
      ]);
      setMessages(msgs);
      setAttachments(atts);
    } catch (err: any) {
      message.error(`加载会话失败: ${err.message}`);
    }
  };

  const handleNewConversation = () => {
    if (sending) {
      handleStop();
    }
    setCurrentConvId('');
    setMessages([]);
    setAttachments([]);
    setInputQuery('');
  };

  const handleDeleteConversation = async (e: React.MouseEvent, convId: string) => {
    e.stopPropagation();
    if (convId === currentConvId && sending) {
      handleStop();
    }
    try {
      await deleteConversation(convId);
      message.success('会话已删除');
      if (currentConvId === convId) {
        handleNewConversation();
      }
      loadConversations();
    } catch (err: any) {
      message.error(`删除会话失败: ${err.message}`);
    }
  };

  const handleUploadFiles = async (files: FileList | File[]) => {
    const fileArray = Array.from(files);
    if (fileArray.length === 0) return;

    const allowedExts = ['.pdf', '.docx', '.txt', '.md'];
    const validFiles: File[] = [];

    for (const f of fileArray) {
      const ext = '.' + f.name.split('.').pop()?.toLowerCase();
      if (!allowedExts.includes(ext)) {
        message.error(`不支持的文件格式: ${f.name}。系统支持 PDF、DOCX、TXT、Markdown。`);
        return;
      }
      if (f.size > 20 * 1024 * 1024) {
        message.error(`文件 "${f.name}" 超过 20MB 大小限制 (${(f.size / (1024 * 1024)).toFixed(1)}MB)`);
        return;
      }
      validFiles.push(f);
    }

    if (attachments.length + validFiles.length > 10) {
      message.error(
        `单会话最多允许上传 10 个附件，当前已有 ${attachments.length} 个，无法继续添加 ${validFiles.length} 个。`
      );
      return;
    }

    setUploading(true);
    try {
      let convId = currentConvId;
      if (!convId) {
        const title =
          validFiles.length === 1
            ? `关于 ${validFiles[0].name} 的问答`
            : `关于 ${validFiles[0].name} 等附件的问答`;
        const newConv = await createConversation({
          title,
          knowledge_base_id: selectedKbId || undefined,
          mode: executionMode,
        });
        convId = newConv.id;
        setCurrentConvId(convId);
        setConversations((prev) => [newConv, ...prev]);
      }

      const uploaded = await uploadAttachments(convId, validFiles);
      message.success(`成功上传并解析 ${uploaded.length} 个附件`);
      const allAtts = await getAttachments(convId);
      setAttachments(allAtts);
    } catch (err: any) {
      const errorMsg = err.response?.data?.detail || err.message || '附件上传处理失败';
      message.error(`上传失败: ${errorMsg}`);
    } finally {
      setUploading(false);
    }
  };

  const handleDeleteAttachment = async (attachmentId: string) => {
    if (!currentConvId) return;
    try {
      await deleteAttachment(currentConvId, attachmentId);
      message.success('附件已删除');
      setAttachments((prev) => prev.filter((a) => a.id !== attachmentId));
    } catch (err: any) {
      message.error(`删除附件失败: ${err.message || '未知错误'}`);
    }
  };

  const openAttachmentPreview = (attachmentId: string) => {
    setPreviewAttachmentId(attachmentId);
    setPreviewDrawerOpen(true);
  };

  const handleSend = async () => {
    const query = inputQuery.trim();
    if (!query) return;

    const hasCompletedAttachments = attachments.some((a) => a.parse_status === 'COMPLETED');
    if (!selectedKbId && !hasCompletedAttachments) {
      message.warning('请先在顶部选择目标知识库或上传附件后再发起提问');
      return;
    }

    if (sending) {
      return;
    }

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: 'user',
      content: query,
      created_at: new Date().toISOString(),
    };

    const assistantTempId = `assistant-${Date.now()}`;
    const assistantPlaceholder: ChatMessage = {
      id: assistantTempId,
      role: 'assistant',
      content: '',
      citations: [],
      created_at: new Date().toISOString(),
    };

    setMessages((prev) => [...prev, userMsg, assistantPlaceholder]);
    setInputQuery('');
    setSending(true);

    const abortController = new AbortController();
    abortControllerRef.current = abortController;

    try {
      if (executionMode === 'agentic') {
        const chosenModel = activeModels.find((m) => m.id === selectedModelId);
        if (chosenModel && !chosenModel.capabilities.supports_tool_calling) {
          message.error(
            `当前选择的模型 [${chosenModel.display_name}] 不支持原生工具调用 (Tool Calling)。深度分析模式需要模型具备自主工具调用能力，请切换为支持 Tool Calling 的模型或使用 [⚡ 快速问答] 模式。`
          );
          setSending(false);
          setStreamingStatus('');
          return;
        }

        let currTimeline: AgentExecutionTimeline = {
          status: 'running',
          steps: [],
          tool_calls: [],
        };
        setStreamingStatus('Agent 正在进行多步推理规划...');

        await sendAgentRunStream(
          'default',
          {
            input: query,
            conversation_id: currentConvId || undefined,
            knowledge_base_id: selectedKbId || undefined,
            model_id: selectedModelId || undefined,
          },
          (sseEvent: SSEAgentRunEvent) => {
            switch (sseEvent.event) {
              case 'run_started': {
                const runId = sseEvent.data?.run_id;
                const convId = sseEvent.data?.conversation_id;
                if (!currentConvId && convId) {
                  setCurrentConvId(convId);
                }
                currTimeline = {
                  ...currTimeline,
                  run_id: runId,
                  status: 'running',
                };
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId ? { ...m, agent_timeline: { ...currTimeline } } : m
                  )
                );
                loadConversations();
                break;
              }
              case 'agent_step_completed': {
                const st = sseEvent.data?.step;
                const tcs = sseEvent.data?.tool_calls_count || 0;
                currTimeline = {
                  ...currTimeline,
                  current_step: typeof st === 'number' ? st : currTimeline.current_step,
                  steps: [
                    ...currTimeline.steps,
                    {
                      step: typeof st === 'number' ? st : currTimeline.steps.length + 1,
                      decision: sseEvent.data?.content_preview || '',
                      tools: sseEvent.data?.tools || [],
                      tool_calls_count: tcs,
                      timestamp: sseEvent.data?.timestamp,
                    },
                  ],
                };
                setStreamingStatus(
                  tcs > 0
                    ? `Agent 步骤 ${st}: 决策调用 ${tcs} 个工具...`
                    : `Agent 步骤 ${st}: 正在进行逻辑分析...`
                );
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId ? { ...m, agent_timeline: { ...currTimeline } } : m
                  )
                );
                break;
              }
              case 'tool_call_started': {
                const tcId = sseEvent.data?.tool_call_id;
                const toolName = sseEvent.data?.tool_name;
                const args = sseEvent.data?.args;
                const existing = currTimeline.tool_calls.findIndex((t) => t.tool_call_id === tcId);
                const newCall = {
                  tool_call_id: tcId,
                  tool_name: toolName,
                  args,
                  status: 'calling' as const,
                };
                const updatedCalls =
                  existing >= 0
                    ? currTimeline.tool_calls.map((t, idx) => (idx === existing ? { ...t, ...newCall } : t))
                    : [...currTimeline.tool_calls, newCall];
                currTimeline = {
                  ...currTimeline,
                  tool_calls: updatedCalls,
                };
                setStreamingStatus(`正在调用工具: ${toolName}...`);
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId ? { ...m, agent_timeline: { ...currTimeline } } : m
                  )
                );
                break;
              }
              case 'tool_call_completed': {
                const tcId = sseEvent.data?.tool_call_id;
                const duration = sseEvent.data?.duration_ms;
                const success = sseEvent.data?.success;
                const evidenceCount = sseEvent.data?.evidence_count ?? sseEvent.data?.returned_chunks;
                const updatedCalls = currTimeline.tool_calls.map((t) =>
                  t.tool_call_id === tcId
                    ? {
                        ...t,
                        status: (success ? 'success' : 'failed') as any,
                        duration_ms: duration,
                        evidence_count: evidenceCount,
                        error: sseEvent.data?.error,
                        source_type: sseEvent.data?.source_type,
                        attachment_id: sseEvent.data?.attachment_id,
                        original_filename: sseEvent.data?.original_filename,
                        retrieval_mode: sseEvent.data?.retrieval_mode,
                        retrieved_chunks: sseEvent.data?.retrieved_chunks,
                        returned_chunks: sseEvent.data?.returned_chunks,
                        evidence_refs: sseEvent.data?.evidence_refs,
                        evidence_tokens: sseEvent.data?.evidence_tokens,
                      }
                    : t
                );
                currTimeline = {
                  ...currTimeline,
                  tool_calls: updatedCalls,
                };
                setStreamingStatus(
                  `工具 ${sseEvent.data?.tool_name} 执行完毕 (${duration || 0}ms)，评估结论中...`
                );
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId ? { ...m, agent_timeline: { ...currTimeline } } : m
                  )
                );
                break;
              }
              case 'citation_verified': {
                const citations = sseEvent.data?.citations || [];
                currTimeline = {
                  ...currTimeline,
                  citations_count: citations.length,
                };
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId
                      ? { ...m, citations, agent_timeline: { ...currTimeline } }
                      : m
                  )
                );
                break;
              }
              case 'final_answer': {
                const ans = sseEvent.data?.final_answer || '';
                setMessages((prev) =>
                  prev.map((m) => (m.id === assistantTempId ? { ...m, content: ans } : m))
                );
                break;
              }
              case 'trace': {
                currTimeline = {
                  ...currTimeline,
                  duration_ms: sseEvent.data?.duration_ms,
                };
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId ? { ...m, agent_timeline: { ...currTimeline } } : m
                  )
                );
                break;
              }
              case 'run_completed': {
                currTimeline = {
                  ...currTimeline,
                  status: sseEvent.data?.status || 'completed',
                  duration_ms: sseEvent.data?.duration_ms || currTimeline.duration_ms,
                };
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId
                      ? {
                          ...m,
                          id: sseEvent.data?.message_id || m.id,
                          agent_timeline: { ...currTimeline },
                        }
                      : m
                  )
                );
                loadConversations();
                break;
              }
              case 'run_failed': {
                const err = sseEvent.data?.error || '深度分析执行异常';
                currTimeline = {
                  ...currTimeline,
                  status: 'failed',
                  error: err,
                };
                setMessages((prev) =>
                  prev.map((m) =>
                    m.id === assistantTempId
                      ? {
                          ...m,
                          content: m.content ? `${m.content}\n\n[执行异常: ${err}]` : `深度分析执行异常: ${err}`,
                          agent_timeline: { ...currTimeline },
                        }
                      : m
                  )
                );
                break;
              }
            }
          },
          abortController.signal
        );
      } else {
        // Workflow RAG Mode
        setStreamingStatus('正在检索向量知识库...');
        await sendChatMessageStream(
          {
            knowledge_base_id: selectedKbId || undefined,
            query: query,
            conversation_id: currentConvId || undefined,
            mode: 'workflow',
            top_k: 4,
            model_id: selectedModelId || undefined,
          },
          (sseEvent: SSEChatEvent) => {
            switch (sseEvent.event) {
              case 'start': {
                if (!currentConvId && sseEvent.data?.conversation_id) {
                  setCurrentConvId(sseEvent.data.conversation_id);
                }
                loadConversations();
                break;
              }
              case 'route': {
                const rt = sseEvent.data;
                const path = rt?.execution_path;
                if (path === 'DIRECT_LLM') {
                  setStreamingStatus('直接回答 (无需外部检索)...');
                } else if (path === 'KNOWLEDGE_BASE') {
                  setStreamingStatus('知识库检索...');
                } else if (path === 'ATTACHMENT') {
                  setStreamingStatus('附件检索...');
                } else if (path === 'HYBRID') {
                  setStreamingStatus('混合检索 (知识库 + 附件)...');
                } else if (path === 'AGENT') {
                  setStreamingStatus('Agent 智能体规划...');
                }
                break;
              }
              case 'query_rewrite': {
                const rw = sseEvent.data;
                if (rw?.is_rewritten) {
                  setStreamingStatus(
                    `问题已重写为: "${rw.rewritten_query}" (${rw.duration_ms}ms)，正在检索...`
                  );
                } else {
                  setStreamingStatus(
                    `分析问题为独立问题 (${rw?.duration_ms || 0}ms)，正在检索向量知识库...`
                  );
                }
                break;
              }
              case 'retrieval': {
                const r = sseEvent.data;
                const count = r?.retrieved_chunks_count ?? 0;
                const duration = r?.retrieval_duration_ms ?? 0;
                if (r?.reranker_enabled) {
                  setStreamingStatus(
                    `检索与精排完成: 召回 ${count} 分块 (总耗时 ${duration}ms, 精排: ${r?.reranker_duration_ms ?? 0}ms ${r?.reranker_degraded ? '⚠️降级' : ''})，分配Token预算中...`
                  );
                } else if (r?.retrieval_mode === 'hybrid') {
                  setStreamingStatus(
                    `RRF双路检索完成: 召回 ${count} 分块 (总耗时 ${duration}ms, Dense: ${r?.dense_duration_ms ?? 0}ms, BM25: ${r?.bm25_duration_ms ?? 0}ms, RRF: ${r?.rrf_duration_ms ?? 0}ms)，分配Token预算中...`
                  );
                } else {
                  setStreamingStatus(`已召回 ${count} 个分块 (耗时 ${duration}ms)，正在分配Token预算...`);
                }
                break;
              }
              case 'context_budget': {
                const b = sseEvent.data;
                if (b) {
                  setStreamingStatus(
                    `上下文预算已优化 (输入约 ${b.estimated_input_tokens} tokens, 历史: ${b.history_tokens}, 证据: ${b.retrieval_tokens})，大模型推理中...`
                  );
                }
                break;
              }
              case 'token': {
                const delta = sseEvent.data?.delta || '';
                setMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantTempId
                      ? { ...msg, content: msg.content + delta }
                      : msg
                  )
                );
                break;
              }
              case 'citations': {
                const citations = sseEvent.data || [];
                setMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantTempId
                      ? { ...msg, citations }
                      : msg
                  )
                );
                break;
              }
              case 'trace': {
                const execInfo = sseEvent.data;
                setMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantTempId
                      ? { ...msg, execution_info: execInfo }
                      : msg
                  )
                );
                break;
              }
              case 'done': {
                const finalMsgId = sseEvent.data?.message_id;
                if (finalMsgId) {
                  setMessages((prev) =>
                    prev.map((msg) =>
                      msg.id === assistantTempId
                        ? { ...msg, id: finalMsgId }
                        : msg
                    )
                  );
                }
                loadConversations();
                break;
              }
              case 'error': {
                const errDetail = sseEvent.data?.detail || '未知执行错误';
                message.error(`执行错误: ${errDetail}`);
                setMessages((prev) =>
                  prev.map((msg) =>
                    msg.id === assistantTempId
                      ? {
                          ...msg,
                          content: msg.content
                            ? `${msg.content}\n\n[发生错误: ${errDetail}]`
                            : `问答执行错误: ${errDetail}`,
                        }
                      : msg
                  )
                );
                break;
              }
            }
          },
          abortController.signal
        );
      }
    } catch (err: any) {
      if (err.name === 'AbortError') {
        message.info('已停止生成');
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantTempId
              ? {
                  ...msg,
                  content: msg.content
                    ? `${msg.content}\n\n[回答已被用户中止]`
                    : '[回答已被用户中止]',
                }
              : msg
          )
        );
      } else {
        message.error(`问答执行失败: ${err.message}`);
        setMessages((prev) =>
          prev.map((msg) =>
            msg.id === assistantTempId
              ? {
                  ...msg,
                  content: msg.content
                    ? `${msg.content}\n\n[执行错误: ${err.message}]`
                    : `问答执行失败: ${err.message}`,
                }
              : msg
          )
        );
      }
    } finally {
      setSending(false);
      setStreamingStatus('');
      abortControllerRef.current = null;
    }
  };

  const openCitationDetail = (citations: Citation[], index?: number) => {
    setActiveCitations(citations);
    setSelectedCitationIndex(index);
    setDrawerOpen(true);
  };

  const selectedKb = knowledgeBases.find((k) => k.id === selectedKbId);

  // Group conversations by date (Today, Yesterday, Last 7 Days, Older)
  const groupedConversations = useMemo(() => {
    const now = new Date();
    const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const yesterdayStart = todayStart - 86400000;
    const sevenDaysStart = todayStart - 6 * 86400000;

    const groups: {
      title: string;
      key: 'today' | 'yesterday' | 'last7Days' | 'older';
      items: Conversation[];
    }[] = [
      { title: '今天', key: 'today', items: [] },
      { title: '昨天', key: 'yesterday', items: [] },
      { title: '最近 7 天', key: 'last7Days', items: [] },
      { title: '更早', key: 'older', items: [] },
    ];

    for (const conv of conversations) {
      const rawDate = conv.updated_at || conv.created_at;
      const time = rawDate ? new Date(rawDate).getTime() : NaN;
      if (isNaN(time)) {
        groups[3].items.push(conv);
      } else if (time >= todayStart) {
        groups[0].items.push(conv);
      } else if (time >= yesterdayStart) {
        groups[1].items.push(conv);
      } else if (time >= sevenDaysStart) {
        groups[2].items.push(conv);
      } else {
        groups[3].items.push(conv);
      }
    }

    return groups.filter((g) => g.items.length > 0);
  }, [conversations]);

  return (
    <div
      style={{
        display: 'flex',
        height: 'calc(100vh - 60px)',
        backgroundColor: '#fafbfc',
        overflow: 'hidden',
      }}
    >
      {/* Left Sidebar: Conversations (260px) */}
      <div
        style={{
          width: 260,
          minWidth: 260,
          maxWidth: 260,
          backgroundColor: '#ffffff',
          borderRight: '1px solid #e2e8f0',
          display: 'flex',
          flexDirection: 'column',
          padding: '16px 12px 12px',
          height: '100%',
          overflow: 'hidden',
        }}
      >
        <Button
          type="primary"
          icon={<PlusOutlined />}
          style={{
            width: '100%',
            marginBottom: 14,
            height: 38,
            fontWeight: 500,
            borderRadius: 8,
            fontSize: 13.5,
            backgroundColor: '#2563eb',
            boxShadow: '0 1px 2px rgba(37, 99, 235, 0.15)',
          }}
          onClick={handleNewConversation}
        >
          新建智能问答
        </Button>

        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            padding: '0 4px 6px',
            userSelect: 'none',
          }}
        >
          <Text type="secondary" style={{ fontSize: 11, fontWeight: 600, color: '#64748b' }}>
            历史对话
          </Text>
          <Text type="secondary" style={{ fontSize: 11, color: '#94a3b8' }}>
            {conversations.length} 个会话
          </Text>
        </div>

        <div
          style={{
            flex: 1,
            overflowY: 'auto',
            paddingRight: 2,
          }}
        >
          {conversations.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '40px 0', color: '#94a3b8', fontSize: 13 }}>
              暂无历史会话
            </div>
          ) : (
            groupedConversations.map((group) => (
              <div key={group.key} style={{ marginBottom: 12 }}>
                <div
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: '#94a3b8',
                    padding: '6px 8px 4px',
                    letterSpacing: '0.3px',
                    userSelect: 'none',
                  }}
                >
                  {group.title}
                </div>

                {group.items.map((conv) => {
                  const active = conv.id === currentConvId;
                  return (
                    <div
                      key={conv.id}
                      onClick={() => handleSelectConversation(conv)}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        padding: '8px 10px',
                        borderRadius: 8,
                        cursor: 'pointer',
                        backgroundColor: active ? '#eff6ff' : 'transparent',
                        border: active ? '1px solid #bfdbfe' : '1px solid transparent',
                        marginBottom: 3,
                        transition: 'all 0.15s ease',
                      }}
                      onMouseEnter={(e) => {
                        if (!active) e.currentTarget.style.backgroundColor = '#f1f5f9';
                        const delBtn = e.currentTarget.querySelector('.conv-del-btn') as HTMLElement;
                        if (delBtn) delBtn.style.opacity = '1';
                      }}
                      onMouseLeave={(e) => {
                        if (!active) e.currentTarget.style.backgroundColor = 'transparent';
                        const delBtn = e.currentTarget.querySelector('.conv-del-btn') as HTMLElement;
                        if (delBtn && !active) delBtn.style.opacity = '0';
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0, flex: 1 }}>
                        <MessageOutlined
                          style={{
                            fontSize: 13,
                            color: active ? '#2563eb' : '#94a3b8',
                            flexShrink: 0,
                          }}
                        />
                        <div style={{ minWidth: 0, flex: 1 }}>
                          <Text
                            ellipsis
                            style={{
                              display: 'block',
                              fontSize: 13,
                              fontWeight: active ? 600 : 400,
                              color: active ? '#1d4ed8' : '#1e293b',
                            }}
                          >
                            {conv.title || '新对话'}
                          </Text>
                          <div style={{ fontSize: 11, color: active ? '#3b82f6' : '#94a3b8', marginTop: 1 }}>
                            {conv.message_count ? `${conv.message_count} 条消息` : '新对话'}
                          </div>
                        </div>
                      </div>

                      <Popconfirm
                        title="确定删除此会话？"
                        description="删除后该会话消息及临时附件将被永久清理。"
                        onConfirm={(e) => handleDeleteConversation(e as any, conv.id)}
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                      >
                        <Button
                          className="conv-del-btn"
                          type="text"
                          size="small"
                          icon={<DeleteOutlined style={{ fontSize: 12, color: '#94a3b8' }} />}
                          onClick={(e) => e.stopPropagation()}
                          style={{
                            opacity: active ? 0.8 : 0,
                            transition: 'opacity 0.15s ease',
                            padding: 0,
                            width: 22,
                            height: 22,
                            flexShrink: 0,
                          }}
                        />
                      </Popconfirm>
                    </div>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>

      {/* Middle & Right: Chat Interaction Area */}
      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          backgroundColor: '#fafbfc',
          height: '100%',
          overflow: 'hidden',
        }}
      >
        {/* Current Session Top Toolbar */}
        <div
          style={{
            height: 56,
            backgroundColor: '#ffffff',
            borderBottom: '1px solid #e2e8f0',
            padding: '0 24px',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            flexShrink: 0,
            gap: 12,
          }}
        >
          {/* Left: KB Selector, Mode Switcher, Attachment Pill */}
          <Space size={12} align="center" wrap>
            <Select
              style={{ width: 230 }}
              placeholder="选择挂载知识库 (可选)"
              allowClear
              value={selectedKbId || undefined}
              onChange={(val) => setSelectedKbId(val || '')}
              options={[
                { label: '不绑定知识库 (仅附件问答)', value: '' },
                ...knowledgeBases.map((kb) => ({
                  label: `${kb.name} (${kb.document_count} 文档)`,
                  value: kb.id,
                })),
              ]}
              suffixIcon={<DatabaseOutlined style={{ color: selectedKbId ? '#2563eb' : '#94a3b8' }} />}
            />

            <Tooltip
              title={
                executionMode === 'agentic'
                  ? '深度分析模式：自主多步推理、动态工具决策与交叉证据核验'
                  : '快速问答模式：高效混合召回、RRF 融合精排与上下文预算注入'
              }
            >
              <Segmented
                value={executionMode}
                onChange={(val) => setExecutionMode(val as 'workflow' | 'agentic')}
                options={[
                  {
                    label: (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 13 }}>
                        ⚡ 快速问答
                      </span>
                    ),
                    value: 'workflow',
                  },
                  {
                    label: (
                      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 13 }}>
                        🧠 深度分析
                      </span>
                    ),
                    value: 'agentic',
                  },
                ]}
                style={{
                  backgroundColor: '#f1f5f9',
                  padding: 2,
                  fontWeight: 500,
                }}
              />
            </Tooltip>

            {attachments.length > 0 && (
              <Tag
                icon={<PaperClipOutlined style={{ color: '#2563eb' }} />}
                style={{
                  backgroundColor: '#eff6ff',
                  borderColor: '#bfdbfe',
                  color: '#1d4ed8',
                  borderRadius: 12,
                  padding: '2px 8px',
                  fontSize: 12,
                  margin: 0,
                  cursor: 'default',
                }}
              >
                {attachments.length} 个附件已就绪
              </Tag>
            )}
          </Space>

          {/* Right: Model Selector */}
          <Space size={8} align="center">
            <span style={{ fontSize: 13, color: '#64748b', fontWeight: 500 }}>模型:</span>
            <Select
              value={selectedModelId || undefined}
              onChange={(val) => {
                setSelectedModelId(val);
                const chosen = activeModels.find((m) => m.id === val);
                if (chosen && executionMode === 'agentic' && !chosen.capabilities.supports_tool_calling) {
                  message.warning(
                    `提示：模型 [${chosen.display_name}] 不支持工具调用，深度分析模式需使用具备 Tool Calling 能力的模型。`
                  );
                }
              }}
              placeholder="选择运行模型"
              style={{ minWidth: 200 }}
              popupMatchSelectWidth={false}
            >
              {activeModels.map((m) => (
                <Select.Option key={m.id} value={m.id}>
                  <Space size={6}>
                    <span>{m.display_name}</span>
                    {m.is_default && (
                      <Tag color="gold" style={{ fontSize: 10, padding: '0 4px', lineHeight: '18px' }}>
                        默认
                      </Tag>
                    )}
                    {m.capabilities.supports_tool_calling ? (
                      <Tag color="purple" style={{ fontSize: 10, padding: '0 4px', lineHeight: '18px' }}>
                        Tool
                      </Tag>
                    ) : (
                      <Tag color="default" style={{ fontSize: 10, padding: '0 4px', lineHeight: '18px' }}>
                        普通
                      </Tag>
                    )}
                  </Space>
                </Select.Option>
              ))}
            </Select>

            <Tooltip title="长期记忆库：管理自动沉淀与用户定义的事实、偏好与规则">
              <Button
                icon={<BulbOutlined style={{ color: '#2563eb' }} />}
                onClick={() => setMemoryDrawerOpen(true)}
                style={{
                  borderRadius: 8,
                  fontSize: 13,
                  borderColor: '#e2e8f0',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 4,
                  height: 32,
                }}
              >
                记忆库
                {memoryCount > 0 && (
                  <Tag
                    color="blue"
                    style={{
                      borderRadius: 10,
                      fontSize: 11,
                      padding: '0 6px',
                      margin: 0,
                      lineHeight: '18px',
                    }}
                  >
                    {memoryCount}
                  </Tag>
                )}
              </Button>
            </Tooltip>
          </Space>
        </div>

        {/* Message Reading Area (Centered Stream max-width: 840px) */}
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            padding: '24px 20px',
          }}
        >
          <div
            style={{
              maxWidth: 840,
              width: '100%',
              margin: '0 auto',
            }}
          >
            {messages.length === 0 ? (
              <div style={{ textAlign: 'center', padding: '60px 0 40px' }}>
                <Avatar
                  size={56}
                  icon={<RobotOutlined />}
                  style={{ backgroundColor: '#2563eb', marginBottom: 16 }}
                />
                <Title level={4} style={{ color: '#0f172a', margin: '0 0 8px 0', fontWeight: 700 }}>
                  企业级智能知识助手
                </Title>
                <Paragraph type="secondary" style={{ maxWidth: 520, margin: '0 auto 24px', fontSize: 13.5, lineHeight: 1.6 }}>
                  基于混合高保真检索、智能 Agent 多步规划与防幻觉事实依据驱动。所生成的回答均由事实片段提供精确溯源。
                </Paragraph>

                {selectedKb && selectedKb.document_count > 0 && (
                  <div style={{ maxWidth: 520, margin: '0 auto', textAlign: 'left' }}>
                    <Text type="secondary" style={{ fontSize: 12, marginBottom: 8, display: 'block', fontWeight: 500 }}>
                      💡 建议提问示例：
                    </Text>
                    <Space direction="vertical" style={{ width: '100%' }}>
                      <Button
                        block
                        style={{
                          textAlign: 'left',
                          borderRadius: 8,
                          height: 38,
                          backgroundColor: '#ffffff',
                          borderColor: '#e2e8f0',
                          color: '#334155',
                        }}
                        onClick={() => setInputQuery('请总结该知识库文档的核心要点与规范')}
                      >
                        👉 请总结该知识库文档的核心要点与规范
                      </Button>
                    </Space>
                  </div>
                )}
              </div>
            ) : (
              messages.map((msg) => {
                const isUser = msg.role === 'user';
                return (
                  <div
                    key={msg.id}
                    style={{
                      display: 'flex',
                      marginBottom: 24,
                      flexDirection: isUser ? 'row-reverse' : 'row',
                      alignItems: 'flex-start',
                      gap: 12,
                      width: '100%',
                    }}
                  >
                    <Avatar
                      icon={isUser ? <UserOutlined /> : <RobotOutlined />}
                      style={{
                        backgroundColor: isUser ? '#10b981' : '#2563eb',
                        flexShrink: 0,
                        marginTop: 2,
                      }}
                    />

                    <div
                      style={{
                        maxWidth: isUser ? '75%' : '100%',
                        flex: isUser ? undefined : 1,
                        minWidth: 0,
                        backgroundColor: isUser ? '#2563eb' : '#ffffff',
                        color: isUser ? '#ffffff' : '#1e293b',
                        padding: isUser ? '12px 18px' : '16px 20px',
                        borderRadius: isUser ? '16px 4px 16px 16px' : '4px 16px 16px 16px',
                        boxShadow: isUser ? '0 1px 3px rgba(37,99,235,0.15)' : '0 1px 3px rgba(0,0,0,0.05)',
                        border: isUser ? 'none' : '1px solid #e2e8f0',
                        fontSize: 14.5,
                        lineHeight: isUser ? 1.6 : 1.75,
                      }}
                    >
                    {/* Agent Execution Timeline Card */}
                    {!isUser && (msg.agent_timeline || msg.execution_info?.agent_timeline) && (
                      <AgentTimelineCard timeline={msg.agent_timeline || msg.execution_info?.agent_timeline} />
                    )}

                    {!isUser && !msg.content ? (
                      <Space size={8}>
                        <Spin size="small" />
                        <Text type="secondary" style={{ fontSize: 13 }}>
                          {streamingStatus || (executionMode === 'agentic' ? 'Agent 正在进行多步推理规划...' : '正在分析检索知识库...')}
                        </Text>
                      </Space>
                    ) : isUser ? (
                      <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                        {msg.content}
                      </div>
                    ) : (
                      <MarkdownRenderer
                        content={msg.content}
                        citations={msg.citations}
                        onCitationClick={(idx) => openCitationDetail(msg.citations!, idx)}
                        isStreaming={sending && msg.id.startsWith('assistant-')}
                      />
                    )}

                    {/* Citations block for Assistant */}
                    {!isUser && msg.citations && msg.citations.length > 0 && (
                      <div
                        style={{
                          marginTop: 14,
                          paddingTop: 12,
                          borderTop: '1px solid #f0f0f0',
                        }}
                      >
                        <div
                          style={{
                            display: 'flex',
                            justifyContent: 'space-between',
                            alignItems: 'center',
                            marginBottom: 8,
                          }}
                        >
                          <Space size={6}>
                            <FileTextOutlined style={{ color: '#1677ff', fontSize: 13 }} />
                            <Text strong style={{ fontSize: 12, color: '#1677ff' }}>
                              真实引用事实依据 ({msg.citations.length} 处):
                            </Text>
                          </Space>
                          <Button
                            size="small"
                            type="link"
                            onClick={() => openCitationDetail(msg.citations!)}
                            style={{ padding: 0, fontSize: 12 }}
                          >
                            展开所有证据详情 <RightOutlined style={{ fontSize: 10 }} />
                          </Button>
                        </div>

                        <Space wrap size={[6, 6]}>
                          {msg.citations.map((c) => (
                            <Tooltip
                              key={c.chunk_id || c.citation_index}
                              title={`来源: ${c.document_name} (第 ${c.page_number} 页) | 匹配度: ${(c.similarity_score * 100).toFixed(0)}%`}
                            >
                              <Tag
                                color="blue"
                                style={{
                                  cursor: 'pointer',
                                  borderRadius: 4,
                                  padding: '2px 8px',
                                  fontSize: 12,
                                }}
                                onClick={() => openCitationDetail(msg.citations!, c.citation_index)}
                              >
                                [Ref-{c.citation_index}] {c.document_name} · P.{c.page_number}
                              </Tag>
                            </Tooltip>
                          ))}
                        </Space>
                      </div>
                    )}

                    {/* Execution Info Badge bar */}
                    {!isUser && msg.execution_info && (
                      <div
                        style={{
                          marginTop: 10,
                          fontSize: 11,
                          color: '#8c8c8c',
                          display: 'flex',
                          flexWrap: 'wrap',
                          alignItems: 'center',
                          gap: 8,
                        }}
                      >
                        {/* Execution Path / Intent Route Badge */}
                        {msg.execution_info.execution_path === 'DIRECT_LLM' && (
                          <Tooltip title={msg.execution_info.route_reason || '普通通用问答，直接由大模型生成回答，无需外部检索'}>
                            <Tag color="success" style={{ fontSize: 10, margin: 0, cursor: 'pointer', fontWeight: 500 }}>
                              直接回答
                            </Tag>
                          </Tooltip>
                        )}
                        {msg.execution_info.execution_path === 'KNOWLEDGE_BASE' && (
                          <Tooltip title={msg.execution_info.route_reason || '调用企业知识库混合检索 (Dense + BM25 + RRF)'}>
                            <Tag color="blue" style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}>
                              知识库检索
                            </Tag>
                          </Tooltip>
                        )}
                        {msg.execution_info.execution_path === 'ATTACHMENT' && (
                          <Tooltip title={msg.execution_info.route_reason || '调用当前会话临时附件检索服务'}>
                            <Tag color="purple" style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}>
                              附件检索
                            </Tag>
                          </Tooltip>
                        )}
                        {msg.execution_info.execution_path === 'HYBRID' && (
                          <Tooltip title={msg.execution_info.route_reason || '联合调用企业知识库与会话附件双源检索'}>
                            <Tag color="cyan" style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}>
                              混合检索
                            </Tag>
                          </Tooltip>
                        )}
                        {msg.execution_info.execution_path === 'AGENT' && (
                          <Tooltip title={msg.execution_info.route_reason || '进入 LangGraph Agent 智能体自主多步规划与工具协同'}>
                            <Tag color="geekblue" style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}>
                              Agent 分析
                            </Tag>
                          </Tooltip>
                        )}

                        <Space size={4}>
                          <ClockCircleOutlined />
                          <span>耗时: {msg.execution_info.total_duration_ms} ms</span>
                        </Space>
                        <span>
                          (检索: {msg.execution_info.retrieval_duration_ms} ms, 生成:{' '}
                          {msg.execution_info.generation_duration_ms} ms
                          {msg.execution_info.ttft_ms ? `, 首字: ${msg.execution_info.ttft_ms} ms` : ''})
                        </span>
                        <span>召回片段: {msg.execution_info.retrieved_chunks_count} 个</span>
                        {msg.execution_info.retrieval_mode === 'hybrid' && (
                          <Tooltip
                            title={`RRF双路混合检索 | 总检索: ${msg.execution_info.retrieval_duration_ms} ms (Dense: ${msg.execution_info.dense_duration_ms ?? 0}ms [${msg.execution_info.dense_chunks_count ?? 0}条], BM25: ${msg.execution_info.bm25_duration_ms ?? 0}ms [${msg.execution_info.bm25_chunks_count ?? 0}条], RRF融合: ${msg.execution_info.rrf_duration_ms ?? 0}ms [候选: ${msg.execution_info.deduplicated_chunks_count ?? 0}条]) | 降级状态: ${msg.execution_info.degradation_status || 'none'}`}
                          >
                            <Tag color="purple" style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}>
                              RRF融合
                              {msg.execution_info.degradation_status && msg.execution_info.degradation_status !== 'none'
                                ? ` (${msg.execution_info.degradation_status})`
                                : ''}
                            </Tag>
                          </Tooltip>
                        )}
                        {msg.execution_info.reranker_enabled && (
                          <Tooltip
                            title={`Cross-Encoder 精排 (${msg.execution_info.reranker_model || 'bge-reranker-base'}) | 精排耗时: ${msg.execution_info.reranker_duration_ms ?? 0} ms | 候选池: ${msg.execution_info.reranker_candidates_in ?? 0} -> ${msg.execution_info.reranker_candidates_out ?? 0}${msg.execution_info.reranker_degraded ? ' | ⚠️触发降级回退' : ''}`}
                          >
                            <Tag
                              color={msg.execution_info.reranker_degraded ? 'orange' : 'volcano'}
                              style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}
                            >
                              精排 {msg.execution_info.reranker_duration_ms ?? 0}ms
                              {msg.execution_info.reranker_degraded ? ' (降级)' : ''}
                            </Tag>
                          </Tooltip>
                        )}
                        {msg.execution_info.is_rewritten && msg.execution_info.rewritten_query && (
                          <Tooltip title={`原始问题: ${msg.execution_info.original_query || '无'} | 重写耗时: ${msg.execution_info.rewrite_duration_ms || 0} ms${msg.execution_info.rewrite_tokens_used ? ` | 消耗: ${msg.execution_info.rewrite_tokens_used} tokens` : ''}`}>
                            <Tag color="geekblue" style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}>
                              Query已重写: {msg.execution_info.rewritten_query}
                            </Tag>
                          </Tooltip>
                        )}
                        {!msg.execution_info.is_rewritten && msg.execution_info.rewrite_duration_ms !== undefined && msg.execution_info.rewrite_duration_ms !== null && (
                          <Tag style={{ fontSize: 10, margin: 0 }}>
                            独立问题 (分析 {msg.execution_info.rewrite_duration_ms}ms)
                          </Tag>
                        )}
                        {msg.execution_info.estimated_input_tokens !== undefined && msg.execution_info.estimated_input_tokens !== null && (
                          <Tooltip
                            title={`上下文窗口: ${msg.execution_info.model_context_window || 32768} tokens | 输入预估: ${msg.execution_info.estimated_input_tokens} tokens (历史: ${msg.execution_info.history_tokens ?? 0}, 证据: ${msg.execution_info.retrieval_tokens ?? 0}) | 预留输出: ${msg.execution_info.reserved_output_tokens ?? 4096} | 估算方案: ${msg.execution_info.budget_estimation_method || 'heuristic'}${msg.execution_info.history_messages_pruned ? ` | 历史裁剪丢弃: ${msg.execution_info.history_messages_pruned} 条` : ''}${msg.execution_info.retrieval_chunks_pruned ? ` | 证据裁剪丢弃: ${msg.execution_info.retrieval_chunks_pruned} 个` : ''}`}
                          >
                            <Tag color="cyan" style={{ fontSize: 10, margin: 0, cursor: 'pointer' }}>
                              Token预算: {msg.execution_info.estimated_input_tokens} tokens
                              {msg.execution_info.history_messages_pruned || msg.execution_info.retrieval_chunks_pruned ? ' (已动态裁剪)' : ''}
                            </Tag>
                          </Tooltip>
                        )}
                        {msg.execution_info.llm_model && (
                          <Tag style={{ fontSize: 10, margin: 0 }}>
                            {msg.execution_info.llm_model}
                          </Tag>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              );
            })
          )}

          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Bottom Floating Composer Area */}
      <div
        style={{
          backgroundColor: '#fafbfc',
          padding: '0 20px 20px',
          flexShrink: 0,
        }}
      >
        <ChatComposer
          inputQuery={inputQuery}
          setInputQuery={setInputQuery}
          sending={sending}
          onSend={handleSend}
          onStop={handleStop}
          attachments={attachments}
          uploading={uploading}
          onUploadFiles={handleUploadFiles}
          onDeleteAttachment={handleDeleteAttachment}
          onPreviewAttachment={openAttachmentPreview}
        />
      </div>
    </div>

      {/* Citation Detail Drawer */}
      <CitationDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        citations={activeCitations}
        selectedIndex={selectedCitationIndex}
      />

      {/* Attachment Detail & Parsed Preview Drawer (P0.3) */}
      <AttachmentPreviewDrawer
        open={previewDrawerOpen}
        onClose={() => setPreviewDrawerOpen(false)}
        conversationId={currentConvId}
        attachmentId={previewAttachmentId}
      />

      {/* Long-term Memory Drawer (P1.2 Stage 4) */}
      <MemoryDrawer
        open={memoryDrawerOpen}
        onClose={() => setMemoryDrawerOpen(false)}
        onMemoryChanged={refreshMemoryCount}
      />
    </div>
  );
};
