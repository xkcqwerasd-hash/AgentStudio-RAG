import React, { useEffect, useState } from 'react';
import {
  Input,
  Select,
  InputNumber,
  Button,
  Space,
  Typography,
  Tag,
  List,
  Alert,
  message,
  Empty,
} from 'antd';
import {
  SearchOutlined,
  CheckCircleOutlined,
  ThunderboltOutlined,
  AimOutlined,
} from '@ant-design/icons';
import { getKnowledgeBases } from '../api/knowledge';
import { testRetrieval } from '../api/chat';
import { KnowledgeBase, RetrievalTestResponse } from '../types';
import { DESIGN_TOKENS } from '../theme';

const { Title, Text, Paragraph } = Typography;

interface RetrievalDebugPageProps {
  initialKbId?: string;
}

export const RetrievalDebugPage: React.FC<RetrievalDebugPageProps> = ({ initialKbId }) => {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<string>(initialKbId || '');
  const [query, setQuery] = useState<string>('');
  const [topK, setTopK] = useState<number>(5);
  const [mode, setMode] = useState<'rerank' | 'hybrid' | 'dense' | 'bm25'>('rerank');
  const [loading, setLoading] = useState<boolean>(false);
  const [result, setResult] = useState<RetrievalTestResponse | null>(null);

  useEffect(() => {
    getKnowledgeBases().then((kbs) => {
      setKnowledgeBases(kbs);
      if (kbs.length > 0 && !selectedKbId) {
        setSelectedKbId(kbs[0].id);
      }
    });
  }, []);

  useEffect(() => {
    if (initialKbId) {
      setSelectedKbId(initialKbId);
    }
  }, [initialKbId]);

  const handleTest = async () => {
    if (!selectedKbId) {
      message.warning('请先选择目标知识库');
      return;
    }
    if (!query.trim()) {
      message.warning('请输入待测试的查询词');
      return;
    }

    setLoading(true);
    try {
      const res = await testRetrieval(selectedKbId, query, topK, mode);
      setResult(res);
      message.success(`检索完成，召回 ${res.total_retrieved} 个片段，总耗时 ${res.duration_ms} ms`);
    } catch (err: any) {
      message.error(`检索调试请求失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ padding: '24px 32px', maxWidth: 1440, margin: '0 auto' }}>
      {/* Control Card */}
      <div
        style={{
          backgroundColor: '#ffffff',
          borderRadius: DESIGN_TOKENS.cardRadius,
          border: `1px solid ${DESIGN_TOKENS.borderRegular}`,
          boxShadow: '0 1px 3px rgba(0,0,0,0.03)',
          padding: '20px 24px',
          marginBottom: 20,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 14, marginBottom: 16 }}>
          <div
            style={{
              width: 42,
              height: 42,
              borderRadius: 10,
              backgroundColor: '#fff7ed',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#ea580c',
              fontSize: 20,
            }}
          >
            <ThunderboltOutlined />
          </div>
          <div>
            <Title level={4} style={{ margin: 0, color: DESIGN_TOKENS.brandDark, fontWeight: 700 }}>
              向量检索质量调试台 (Retrieval Quality Workbench)
            </Title>
            <Text type="secondary" style={{ fontSize: 13 }}>
              支持算法工程师在线调试 Dense 向量语义检索、Sparse BM25 词法检索、RRF 混合召回与 Cross-Encoder 重排得分分布。
            </Text>
          </div>
        </div>

        <Space direction="vertical" style={{ width: '100%' }} size={16}>
          <Space wrap size={16} align="center">
            <div>
              <Text strong style={{ marginRight: 8, fontSize: 13 }}>
                目标知识库:
              </Text>
              <Select
                style={{ width: 280 }}
                placeholder="选择目标知识库"
                value={selectedKbId || undefined}
                onChange={(val) => setSelectedKbId(val)}
                options={knowledgeBases.map((kb) => ({
                  label: `${kb.name} (${kb.document_count} 文档)`,
                  value: kb.id,
                }))}
              />
            </div>

            <div>
              <Text strong style={{ marginRight: 8, fontSize: 13 }}>
                Top-K 召回数:
              </Text>
              <InputNumber min={1} max={20} value={topK} onChange={(v) => setTopK(v || 5)} style={{ width: 80 }} />
            </div>

            <div>
              <Text strong style={{ marginRight: 8, fontSize: 13 }}>
                检索算法策略:
              </Text>
              <Select
                style={{ width: 300 }}
                value={mode}
                onChange={(v) => setMode(v)}
                options={[
                  { label: '⚡ Cross-Encoder 精排 (RRF + Reranker)', value: 'rerank' },
                  { label: '🔀 RRF 混合检索 (Dense + BM25)', value: 'hybrid' },
                  { label: '🎯 向量语义检索 (Dense Only)', value: 'dense' },
                  { label: '🔍 关键词稀疏检索 (BM25 Only)', value: 'bm25' },
                ]}
              />
            </div>
          </Space>

          <Space.Compact style={{ width: '100%' }}>
            <Input
              size="large"
              placeholder="输入待测试的业务提问或关键词 (例如：微服务网关接口超时是多少？/ 熔断器机制 / min_idle)"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onPressEnter={handleTest}
              style={{ borderRadius: '8px 0 0 8px' }}
            />
            <Button
              type="primary"
              size="large"
              icon={<SearchOutlined />}
              loading={loading}
              onClick={handleTest}
              style={{ borderRadius: '0 8px 8px 0', fontWeight: 600, padding: '0 24px' }}
            >
              执行检索测试
            </Button>
          </Space.Compact>
        </Space>
      </div>

      {/* Results Section */}
      {result ? (
        <div
          style={{
            backgroundColor: '#ffffff',
            borderRadius: DESIGN_TOKENS.cardRadius,
            border: `1px solid ${DESIGN_TOKENS.borderRegular}`,
            boxShadow: '0 1px 3px rgba(0,0,0,0.03)',
            padding: '20px 24px',
          }}
        >
          {/* Header Summary */}
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: 12,
              paddingBottom: 16,
              marginBottom: 16,
              borderBottom: `1px solid ${DESIGN_TOKENS.borderLight}`,
            }}
          >
            <Space align="center" size={10}>
              <CheckCircleOutlined style={{ color: '#16a34a', fontSize: 18 }} />
              <span style={{ fontSize: 16, fontWeight: 700, color: DESIGN_TOKENS.brandDark }}>
                检索召回结果清单 ({result.results.length} 条片段)
              </span>
            </Space>

            <Space wrap size={8}>
              <Tag color="orange" style={{ borderRadius: 4, fontWeight: 600 }}>总耗时: {result.duration_ms} ms</Tag>
              {result.dense_duration_ms !== undefined && result.dense_duration_ms !== null && (
                <Tag color="blue" style={{ borderRadius: 4 }}>Dense: {result.dense_duration_ms} ms</Tag>
              )}
              {result.bm25_duration_ms !== undefined && result.bm25_duration_ms !== null && (
                <Tag color="gold" style={{ borderRadius: 4 }}>BM25: {result.bm25_duration_ms} ms</Tag>
              )}
              {result.rrf_duration_ms !== undefined && result.rrf_duration_ms !== null && (
                <Tag color="purple" style={{ borderRadius: 4 }}>RRF融合: {result.rrf_duration_ms} ms</Tag>
              )}
              {result.reranker_duration_ms !== undefined && result.reranker_duration_ms !== null && (
                <Tag color={result.reranker_degraded ? 'orange' : 'volcano'} style={{ borderRadius: 4 }}>
                  精排: {result.reranker_duration_ms} ms {result.reranker_degraded ? '(降级)' : ''}
                </Tag>
              )}
              {result.reranker_candidates_in !== undefined && result.reranker_candidates_in !== null && (
                <Tag color="cyan" style={{ borderRadius: 4 }}>
                  候选池: {result.reranker_candidates_in} ➔ {result.reranker_candidates_out}
                </Tag>
              )}
            </Space>
          </div>

          {result.results.length === 0 ? (
            <Alert
              message="未召回任何片段"
              description="该知识库中暂无匹配的切块数据，请检查知识库中是否上传了文档并完成了向量化处理。"
              type="warning"
              showIcon
              style={{ borderRadius: 8 }}
            />
          ) : (
            <List
              itemLayout="vertical"
              dataSource={result.results}
              renderItem={(item, idx) => {
                const isTop = idx === 0;
                return (
                  <List.Item
                    key={item.chunk_id}
                    style={{
                      backgroundColor: isTop ? '#f0fdf4' : '#fafbfc',
                      border: isTop ? '1px solid #86efac' : `1px solid ${DESIGN_TOKENS.borderRegular}`,
                      borderRadius: 10,
                      padding: '16px 20px',
                      marginBottom: 16,
                      boxShadow: isTop ? '0 2px 8px rgba(34, 197, 94, 0.08)' : 'none',
                    }}
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        marginBottom: 10,
                        flexWrap: 'wrap',
                        gap: 8,
                      }}
                    >
                      <Space size={8} wrap align="center">
                        <Tag
                          color={isTop ? 'success' : 'default'}
                          style={{ fontSize: 12, padding: '2px 8px', fontWeight: 700, borderRadius: 4 }}
                        >
                          Rank #{idx + 1}
                        </Tag>
                        <Text strong style={{ fontSize: 14, color: DESIGN_TOKENS.brandDark }}>
                          {item.document_name}
                        </Text>
                        <Tag color="cyan" style={{ borderRadius: 4 }}>第 {item.page_number} 页</Tag>
                        {item.fusion_score !== undefined && item.fusion_score !== null && (
                          <Tag color="purple" style={{ fontSize: 11, borderRadius: 4 }}>
                            RRF: {item.fusion_score.toFixed(5)}
                          </Tag>
                        )}
                        {item.dense_rank && (
                          <Tag color="geekblue" style={{ fontSize: 11, borderRadius: 4 }}>
                            Dense: #{item.dense_rank} ({item.dense_score})
                          </Tag>
                        )}
                        {item.bm25_rank && (
                          <Tag color="gold" style={{ fontSize: 11, borderRadius: 4 }}>
                            BM25: #{item.bm25_rank} ({item.bm25_score})
                          </Tag>
                        )}
                      </Space>

                      <div>
                        <Text type="secondary" style={{ fontSize: 12, marginRight: 8 }}>
                          {mode === 'rerank'
                            ? 'Cross-Encoder 相关度归一化得分 (Sigmoid):'
                            : mode === 'hybrid'
                            ? 'RRF 融合分:'
                            : '检索得分:'}
                        </Text>
                        <Tag
                          color={
                            mode === 'rerank'
                              ? item.similarity_score >= 0.5
                                ? 'volcano'
                                : 'geekblue'
                              : mode === 'hybrid'
                              ? 'purple'
                              : item.similarity_score >= 0.3
                              ? 'success'
                              : 'processing'
                          }
                          style={{ borderRadius: 4, fontWeight: 600 }}
                        >
                          {mode === 'rerank'
                            ? item.reranker_score !== undefined && item.reranker_score !== null
                              ? `${item.reranker_score.toFixed(4)} (Logit: ${item.reranker_raw_score ?? '-'})`
                              : item.similarity_score.toFixed(4)
                            : mode === 'hybrid'
                            ? item.similarity_score.toFixed(6)
                            : `${(item.similarity_score * 100).toFixed(1)}% (${item.similarity_score})`}
                        </Tag>
                      </div>
                    </div>

                    <div
                      style={{
                        backgroundColor: '#ffffff',
                        padding: '12px 16px',
                        borderRadius: 8,
                        border: '1px solid #e2e8f0',
                        fontSize: 13.5,
                        lineHeight: 1.6,
                        color: DESIGN_TOKENS.textMain,
                      }}
                    >
                      <Paragraph style={{ margin: 0 }}>{item.content}</Paragraph>
                    </div>
                  </List.Item>
                );
              }}
            />
          )}
        </div>
      ) : (
        <div
          style={{
            backgroundColor: '#ffffff',
            borderRadius: DESIGN_TOKENS.cardRadius,
            border: `1px solid ${DESIGN_TOKENS.borderRegular}`,
            padding: '60px 24px',
            textAlign: 'center',
          }}
        >
          <Empty
            image={<AimOutlined style={{ fontSize: 48, color: '#94a3b8' }} />}
            description="请输入业务提问并点击「执行检索测试」，直观观测多路召回得分分布与高保真原始分块"
          />
        </div>
      )}
    </div>
  );
};

export default RetrievalDebugPage;
