import React, { useState } from 'react';
import { Tag, Typography, Space, Button } from 'antd';
import {
  CheckCircleOutlined,
  LoadingOutlined,
  CloseCircleOutlined,
  StopOutlined,
  WarningOutlined,
  DownOutlined,
  UpOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  FileSearchOutlined,
} from '@ant-design/icons';
import { AgentExecutionTimeline } from '../types';

const { Text } = Typography;

interface AgentTimelineCardProps {
  timeline?: AgentExecutionTimeline;
}

export const AgentTimelineCard: React.FC<AgentTimelineCardProps> = ({ timeline }) => {
  if (!timeline) return null;

  const [expanded, setExpanded] = useState<boolean>(timeline.status === 'running');
  const toolCalls = timeline.tool_calls || [];
  const steps = timeline.steps || [];

  const getStatusTag = () => {
    switch (timeline.status) {
      case 'running':
        return (
          <Tag color="processing" icon={<LoadingOutlined />}>
            深度分析执行中...
          </Tag>
        );
      case 'completed':
        return (
          <Tag color="success" icon={<CheckCircleOutlined />}>
            执行完成 {timeline.duration_ms ? `(${timeline.duration_ms}ms)` : ''}
          </Tag>
        );
      case 'cancelled':
        return (
          <Tag color="default" icon={<StopOutlined />}>
            执行已中止
          </Tag>
        );
      case 'max_steps_reached':
        return (
          <Tag color="warning" icon={<WarningOutlined />}>
            达到最大步数限制
          </Tag>
        );
      case 'failed':
        return (
          <Tag color="error" icon={<CloseCircleOutlined />}>
            执行失败
          </Tag>
        );
      default:
        return <Tag color="blue">Agent 执行</Tag>;
    }
  };

  return (
    <div
      style={{
        backgroundColor: '#f8fafc',
        borderRadius: 8,
        border: '1px solid #e2e8f0',
        padding: '10px 14px',
        marginBottom: 12,
        fontSize: 13,
      }}
    >
      {/* Header bar */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          cursor: 'pointer',
          userSelect: 'none',
        }}
        onClick={() => setExpanded((prev) => !prev)}
      >
        <Space size={8} wrap>
          <ThunderboltOutlined style={{ color: '#722ed1', fontSize: 14 }} />
          <Text strong style={{ color: '#1e293b', fontSize: 13 }}>
            Agent 深度推理链
          </Text>
          {getStatusTag()}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {toolCalls.length > 0
              ? `调用 ${toolCalls.length} 次工具`
              : '逻辑直接推导'}
          </Text>
        </Space>

        <Button
          type="link"
          size="small"
          style={{ padding: 0, fontSize: 12, color: '#64748b' }}
          icon={expanded ? <UpOutlined /> : <DownOutlined />}
        >
          {expanded ? '收起详情' : '展开详情'}
        </Button>
      </div>

      {/* Expanded Timeline View */}
      {expanded && (
        <div style={{ marginTop: 12, paddingTop: 10, borderTop: '1px dashed #cbd5e1' }}>
          {/* Step list */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {steps.map((st, idx) => (
              <div key={idx} style={{ paddingLeft: 8, borderLeft: '2px solid #8b5cf6' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
                  <Text strong style={{ fontSize: 12, color: '#475569' }}>
                    步骤 {st.step}
                  </Text>
                  {st.tool_calls_count ? (
                    <Tag color="purple" style={{ fontSize: 10, padding: '0 4px', margin: 0 }}>
                      决策调用 {st.tool_calls_count} 个工具
                    </Tag>
                  ) : (
                    <Tag style={{ fontSize: 10, padding: '0 4px', margin: 0 }}>综合分析决策</Tag>
                  )}
                </div>
              </div>
            ))}

            {/* Tool Calls Detail */}
            {toolCalls.length > 0 && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8, marginTop: 4 }}>
                {toolCalls.map((tc, tcIdx) => (
                  <div
                    key={tc.tool_call_id || tcIdx}
                    style={{
                      backgroundColor: '#ffffff',
                      borderRadius: 6,
                      border: '1px solid #e2e8f0',
                      padding: '8px 12px',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        marginBottom: 4,
                      }}
                    >
                      <Space size={6}>
                        {tc.tool_name === 'search_knowledge_base' ? (
                          <DatabaseOutlined style={{ color: '#1677ff' }} />
                        ) : (
                          <FileSearchOutlined style={{ color: '#722ed1' }} />
                        )}
                        <Text strong style={{ color: '#0f172a', fontSize: 12 }}>
                          {tc.tool_name === 'search_conversation_attachments'
                            ? 'search_conversation_attachments (临时附件检索)'
                            : tc.tool_name === 'search_knowledge_base'
                            ? 'search_knowledge_base (知识库检索)'
                            : tc.tool_name}
                        </Text>
                        {tc.retrieval_mode && (
                          <Tag color="geekblue" style={{ fontSize: 10, margin: 0 }}>
                            {tc.retrieval_mode === 'bm25' ? 'BM25 检索' : tc.retrieval_mode}
                          </Tag>
                        )}
                        {tc.original_filename && (
                          <Tag color="purple" style={{ fontSize: 10, margin: 0 }}>
                            {tc.original_filename}
                          </Tag>
                        )}
                      </Space>

                      <Space size={6}>
                        {tc.duration_ms !== undefined && (
                          <Text type="secondary" style={{ fontSize: 11 }}>
                            耗时 {tc.duration_ms} ms
                          </Text>
                        )}
                        {tc.status === 'calling' && (
                          <Tag color="processing" icon={<LoadingOutlined />} style={{ fontSize: 10, margin: 0 }}>
                            执行中
                          </Tag>
                        )}
                        {tc.status === 'success' && (
                          <Tag color="success" icon={<CheckCircleOutlined />} style={{ fontSize: 10, margin: 0 }}>
                            成功
                          </Tag>
                        )}
                        {tc.status === 'failed' && (
                          <Tag color="error" icon={<CloseCircleOutlined />} style={{ fontSize: 10, margin: 0 }}>
                            失败
                          </Tag>
                        )}
                      </Space>
                    </div>

                    {/* Arguments preview */}
                    {tc.args && (
                      <div
                        style={{
                          backgroundColor: '#f1f5f9',
                          borderRadius: 4,
                          padding: '4px 8px',
                          fontSize: 11,
                          fontFamily: 'monospace',
                          color: '#334155',
                          marginBottom: 4,
                          wordBreak: 'break-all',
                        }}
                      >
                        {Object.entries(tc.args).map(([k, v]) => (
                          <div key={k}>
                            <span style={{ color: '#64748b' }}>{k}: </span>
                            <span>{typeof v === 'object' ? JSON.stringify(v) : String(v)}</span>
                          </div>
                        ))}
                      </div>
                    )}

                    {/* Result preview */}
                    {tc.evidence_count !== undefined && (
                      <div style={{ fontSize: 11, color: '#15803d', display: 'flex', flexWrap: 'wrap', gap: 6, alignItems: 'center' }}>
                        <span>✓ 命中并验证 {tc.evidence_count} 处可溯源事实证据分块</span>
                        {tc.evidence_tokens ? (
                          <span style={{ color: '#64748b' }}>({tc.evidence_tokens} tokens)</span>
                        ) : null}
                        {tc.evidence_refs && tc.evidence_refs.length > 0 ? (
                          <Tag color="cyan" style={{ fontSize: 10, margin: 0 }}>
                            分配引用: [Ref-{tc.evidence_refs.join('], [Ref-')}]
                          </Tag>
                        ) : null}
                      </div>
                    )}
                    {tc.error && (
                      <div style={{ fontSize: 11, color: '#dc2626' }}>
                        ✗ 异常原因: {tc.error}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}

            {/* Error or exception note */}
            {timeline.error && (
              <div
                style={{
                  padding: '6px 10px',
                  borderRadius: 4,
                  backgroundColor: '#fef2f2',
                  border: '1px solid #fecaca',
                  color: '#b91c1c',
                  fontSize: 12,
                }}
              >
                异常中断: {timeline.error}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};
