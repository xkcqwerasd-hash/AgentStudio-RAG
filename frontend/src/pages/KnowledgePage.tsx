import React, { useEffect, useState, useMemo } from 'react';
import {
  Table,
  Button,
  Tag,
  Space,
  Select,
  Typography,
  Popconfirm,
  message,
  Row,
  Col,
  Statistic,
  Tooltip,
  Empty,
  Input,
} from 'antd';
import {
  PlusOutlined,
  UploadOutlined,
  DeleteOutlined,
  EyeOutlined,
  DatabaseOutlined,
  FileTextOutlined,
  AppstoreOutlined,
  SearchOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
  ReloadOutlined,
  FilePdfOutlined,
  FileWordOutlined,
  FileMarkdownOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { getKnowledgeBases, getDocuments, deleteDocument, deleteKnowledgeBase } from '../api/knowledge';
import { KnowledgeBase, DocumentItem } from '../types';
import { CreateKBModal } from '../components/CreateKBModal';
import { UploadDocModal } from '../components/UploadDocModal';
import { ChunkViewerModal } from '../components/ChunkViewerModal';
import { DESIGN_TOKENS } from '../theme';

const { Title, Text } = Typography;

interface KnowledgePageProps {
  onNavigateToDebug?: (kbId: string) => void;
}

export const KnowledgePage: React.FC<KnowledgePageProps> = ({ onNavigateToDebug }) => {
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([]);
  const [selectedKbId, setSelectedKbId] = useState<string>('');
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [loadingDocs, setLoadingDocs] = useState<boolean>(false);
  const [loadingKBs, setLoadingKBs] = useState<boolean>(false);

  // Search and Filter State
  const [searchText, setSearchText] = useState<string>('');
  const [statusFilter, setStatusFilter] = useState<string>('ALL');
  const [typeFilter, setTypeFilter] = useState<string>('ALL');

  // Modals
  const [createKbOpen, setCreateKbOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [selectedDocForChunks, setSelectedDocForChunks] = useState<DocumentItem | null>(null);

  const selectedKb = knowledgeBases.find((k) => k.id === selectedKbId);

  const loadKnowledgeBases = async () => {
    setLoadingKBs(true);
    try {
      const data = await getKnowledgeBases();
      setKnowledgeBases(data);
      if (data.length > 0 && !selectedKbId) {
        setSelectedKbId(data[0].id);
      }
    } catch (err: any) {
      message.error(`加载知识库列表失败: ${err.message}`);
    } finally {
      setLoadingKBs(false);
    }
  };

  const loadDocuments = async (kbId: string) => {
    if (!kbId) return;
    setLoadingDocs(true);
    try {
      const docs = await getDocuments(kbId);
      setDocuments(docs);
    } catch (err: any) {
      message.error(`加载文档列表失败: ${err.message}`);
    } finally {
      setLoadingDocs(false);
    }
  };

  useEffect(() => {
    loadKnowledgeBases();
  }, []);

  useEffect(() => {
    if (selectedKbId) {
      loadDocuments(selectedKbId);
    } else {
      setDocuments([]);
    }
    setSearchText('');
    setStatusFilter('ALL');
    setTypeFilter('ALL');
  }, [selectedKbId]);

  const handleDeleteDoc = async (docId: string) => {
    try {
      await deleteDocument(docId);
      message.success('文档及其所有向量索引已安全删除');
      loadDocuments(selectedKbId);
      loadKnowledgeBases();
    } catch (err: any) {
      message.error(`删除文档失败: ${err.message}`);
    }
  };

  const handleDeleteKB = async () => {
    if (!selectedKbId) return;
    try {
      await deleteKnowledgeBase(selectedKbId);
      message.success('知识库已成功删除');
      setSelectedKbId('');
      loadKnowledgeBases();
    } catch (err: any) {
      message.error(`删除知识库失败: ${err.message}`);
    }
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  };

  const getFormatIcon = (fileType: string) => {
    const t = (fileType || '').toLowerCase();
    if (t === 'pdf') return <FilePdfOutlined style={{ color: '#ef4444', fontSize: 16 }} />;
    if (t.includes('doc')) return <FileWordOutlined style={{ color: '#2563eb', fontSize: 16 }} />;
    if (t.includes('md') || t.includes('markdown')) return <FileMarkdownOutlined style={{ color: '#8b5cf6', fontSize: 16 }} />;
    return <FileTextOutlined style={{ color: '#10b981', fontSize: 16 }} />;
  };

  const renderStatus = (status: string, error?: string | null) => {
    switch (status) {
      case 'COMPLETED':
        return (
          <Tag color="success" icon={<CheckCircleOutlined />} style={{ borderRadius: 6, fontWeight: 500 }}>
            已就绪 (已建立索引)
          </Tag>
        );
      case 'PARSING':
        return (
          <Tag color="processing" icon={<LoadingOutlined />} style={{ borderRadius: 6 }}>
            正在解析页面...
          </Tag>
        );
      case 'CHUNKING':
        return (
          <Tag color="processing" icon={<LoadingOutlined />} style={{ borderRadius: 6 }}>
            正在切分分块...
          </Tag>
        );
      case 'EMBEDDING':
        return (
          <Tag color="purple" icon={<LoadingOutlined />} style={{ borderRadius: 6 }}>
            正在计算向量...
          </Tag>
        );
      case 'FAILED':
        return (
          <Tooltip title={error || '文档解析处理失败，建议删除后重新上传'}>
            <Tag color="error" icon={<CloseCircleOutlined />} style={{ borderRadius: 6, cursor: 'pointer' }}>
              解析失败
            </Tag>
          </Tooltip>
        );
      default:
        return (
          <Tag color="default" style={{ borderRadius: 6 }}>
            等待处理
          </Tag>
        );
    }
  };

  // Filtered documents
  const filteredDocuments = useMemo(() => {
    return documents.filter((doc) => {
      const matchSearch =
        !searchText.trim() ||
        doc.file_name.toLowerCase().includes(searchText.trim().toLowerCase());
      const matchStatus =
        statusFilter === 'ALL' || doc.status === statusFilter;
      const matchType =
        typeFilter === 'ALL' ||
        doc.file_type.toLowerCase() === typeFilter.toLowerCase();
      return matchSearch && matchStatus && matchType;
    });
  }, [documents, searchText, statusFilter, typeFilter]);

  const columns = [
    {
      title: '文档名称',
      dataIndex: 'file_name',
      key: 'file_name',
      render: (name: string, record: DocumentItem) => (
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span>{getFormatIcon(record.file_type)}</span>
          <div style={{ minWidth: 0 }}>
            <Tooltip title={name}>
              <Text strong ellipsis style={{ fontSize: 13.5, color: DESIGN_TOKENS.textMain, display: 'block', maxWidth: 320 }}>
                {name}
              </Text>
            </Tooltip>
            <Text type="secondary" style={{ fontSize: 11, fontFamily: 'monospace' }}>
              ID: {record.id.slice(0, 8)}...
            </Text>
          </div>
        </div>
      ),
    },
    {
      title: '格式',
      dataIndex: 'file_type',
      key: 'file_type',
      width: 90,
      render: (type: string) => (
        <Tag style={{ borderRadius: 4, textTransform: 'uppercase', fontSize: 11, fontWeight: 600 }}>
          {type}
        </Tag>
      ),
    },
    {
      title: '文件大小',
      dataIndex: 'file_size',
      key: 'file_size',
      width: 100,
      render: (size: number) => (
        <span style={{ fontSize: 12.5, color: DESIGN_TOKENS.textMuted }}>{formatFileSize(size)}</span>
      ),
    },
    {
      title: '页数',
      dataIndex: 'page_count',
      key: 'page_count',
      width: 80,
      render: (pages: number) => (
        <span style={{ fontSize: 12.5 }}>{pages > 0 ? `${pages} 页` : '-'}</span>
      ),
    },
    {
      title: '分块数 (Chunks)',
      dataIndex: 'chunk_count',
      key: 'chunk_count',
      width: 130,
      render: (count: number) => (
        <Tag color="cyan" style={{ fontWeight: 600, borderRadius: 4 }}>
          {count} 块
        </Tag>
      ),
    },
    {
      title: '索引状态',
      dataIndex: 'status',
      key: 'status',
      width: 190,
      render: (status: string, record: DocumentItem) =>
        renderStatus(status, record.error_message),
    },
    {
      title: '上传时间',
      dataIndex: 'created_at',
      key: 'created_at',
      width: 170,
      render: (t: string) => (
        <span style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>
          {new Date(t).toLocaleString('zh-CN', { hour12: false })}
        </span>
      ),
    },
    {
      title: '操作',
      key: 'action',
      width: 140,
      render: (_: any, record: DocumentItem) => (
        <Space size={6}>
          <Tooltip title={record.status !== 'COMPLETED' ? '仅支持查看已就绪文档的分块数据' : '查看该文档的切分分块及向量内容'}>
            <Button
              type="text"
              size="small"
              icon={<EyeOutlined style={{ color: record.status === 'COMPLETED' ? DESIGN_TOKENS.interactiveBlue : '#cbd5e1' }} />}
              onClick={() => setSelectedDocForChunks(record)}
              disabled={record.status !== 'COMPLETED'}
              style={{ fontSize: 12 }}
            >
              分块
            </Button>
          </Tooltip>
          <Popconfirm
            title="确认删除该文档？"
            description="删除后将物理删除该文件并同步清理 pgvector 向量索引，此操作不可撤销。"
            onConfirm={() => handleDeleteDoc(record.id)}
            okText="确认删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
          >
            <Button type="text" size="small" danger icon={<DeleteOutlined />} style={{ fontSize: 12 }}>
              删除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div style={{ padding: '24px 32px', maxWidth: 1440, margin: '0 auto' }}>
      {/* Top Header Card */}
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
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 16 }}>
          <Space size={14} align="center">
            <div
              style={{
                width: 42,
                height: 42,
                borderRadius: 10,
                backgroundColor: '#eff6ff',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: DESIGN_TOKENS.interactiveBlue,
                fontSize: 20,
              }}
            >
              <DatabaseOutlined />
            </div>
            <div>
              <Title level={4} style={{ margin: 0, color: DESIGN_TOKENS.brandDark, fontWeight: 700 }}>
                企业知识库管理
              </Title>
              <Text type="secondary" style={{ fontSize: 13 }}>
                统一管理企业内部文档解析、滑动窗口分块切分与 pgvector 向量索引生命周期。
              </Text>
            </div>
          </Space>

          <Space size={12} wrap>
            <Select
              style={{ width: 280 }}
              placeholder="选择当前知识库"
              value={selectedKbId || undefined}
              onChange={(val) => setSelectedKbId(val)}
              loading={loadingKBs}
              options={knowledgeBases.map((kb) => ({
                label: `${kb.name} (${kb.document_count} 个文档)`,
                value: kb.id,
              }))}
            />

            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setCreateKbOpen(true)}
              style={{ fontWeight: 500 }}
            >
              新建知识库
            </Button>

            {selectedKb && (
              <Popconfirm
                title="确认彻底删除此知识库？"
                description={`将永久删除知识库【${selectedKb.name}】及其全部 ${selectedKb.document_count} 个文档、${selectedKb.chunk_count} 个向量分块！`}
                onConfirm={handleDeleteKB}
                okText="确认删除"
                cancelText="取消"
                okButtonProps={{ danger: true }}
              >
                <Button danger icon={<DeleteOutlined />}>
                  删除此库
                </Button>
              </Popconfirm>
            )}
          </Space>
        </div>

        {/* Selected KB KPI Metrics Row */}
        {selectedKb && (
          <div style={{ marginTop: 20, paddingTop: 16, borderTop: `1px solid ${DESIGN_TOKENS.borderLight}` }}>
            <Row gutter={[16, 16]}>
              <Col xs={12} sm={6}>
                <div
                  style={{
                    backgroundColor: '#f8fafc',
                    borderRadius: 10,
                    padding: '12px 16px',
                    border: '1px solid #e2e8f0',
                  }}
                >
                  <Statistic
                    title={<span style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>文档总数</span>}
                    value={selectedKb.document_count}
                    prefix={<FileTextOutlined style={{ color: DESIGN_TOKENS.interactiveBlue, marginRight: 6 }} />}
                    valueStyle={{ fontSize: 20, fontWeight: 700, color: DESIGN_TOKENS.brandDark }}
                  />
                </div>
              </Col>
              <Col xs={12} sm={6}>
                <div
                  style={{
                    backgroundColor: '#f8fafc',
                    borderRadius: 10,
                    padding: '12px 16px',
                    border: '1px solid #e2e8f0',
                  }}
                >
                  <Statistic
                    title={<span style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>向量分块总数 (Chunks)</span>}
                    value={selectedKb.chunk_count}
                    prefix={<AppstoreOutlined style={{ color: '#0891b2', marginRight: 6 }} />}
                    valueStyle={{ fontSize: 20, fontWeight: 700, color: '#0891b2' }}
                  />
                </div>
              </Col>
              <Col xs={12} sm={6}>
                <div
                  style={{
                    backgroundColor: '#f8fafc',
                    borderRadius: 10,
                    padding: '12px 16px',
                    border: '1px solid #e2e8f0',
                  }}
                >
                  <Statistic
                    title={<span style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>分块策略配置</span>}
                    value={`${selectedKb.chunk_size} 字符 / ${selectedKb.chunk_overlap} 重叠`}
                    prefix={<SettingOutlined style={{ color: '#7c3aed', marginRight: 6 }} />}
                    valueStyle={{ fontSize: 15, fontWeight: 600, color: DESIGN_TOKENS.brandDark }}
                  />
                </div>
              </Col>
              <Col xs={12} sm={6}>
                <div
                  style={{
                    backgroundColor: '#f8fafc',
                    borderRadius: 10,
                    padding: '12px 16px',
                    border: '1px solid #e2e8f0',
                  }}
                >
                  <Statistic
                    title={<span style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>向量计算模型</span>}
                    value={selectedKb.embedding_model}
                    prefix={<DatabaseOutlined style={{ color: '#16a34a', marginRight: 6 }} />}
                    valueStyle={{ fontSize: 14, fontWeight: 600, color: '#16a34a' }}
                  />
                </div>
              </Col>
            </Row>
          </div>
        )}
      </div>

      {/* Document List Card */}
      {selectedKb ? (
        <div
          style={{
            backgroundColor: '#ffffff',
            borderRadius: DESIGN_TOKENS.cardRadius,
            border: `1px solid ${DESIGN_TOKENS.borderRegular}`,
            boxShadow: '0 1px 3px rgba(0,0,0,0.03)',
            padding: '20px 24px',
          }}
        >
          {/* Action Toolbar & Search Filters */}
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: 12,
              marginBottom: 16,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <Input
                placeholder="搜索文档名称..."
                prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                allowClear
                style={{ width: 220, borderRadius: 8 }}
              />

              <Select
                value={statusFilter}
                onChange={setStatusFilter}
                style={{ width: 140 }}
                options={[
                  { label: '全部状态', value: 'ALL' },
                  { label: '已就绪', value: 'COMPLETED' },
                  { label: '解析中', value: 'PARSING' },
                  { label: '切块中', value: 'CHUNKING' },
                  { label: '向量计算中', value: 'EMBEDDING' },
                  { label: '解析失败', value: 'FAILED' },
                ]}
              />

              <Select
                value={typeFilter}
                onChange={setTypeFilter}
                style={{ width: 120 }}
                options={[
                  { label: '全部格式', value: 'ALL' },
                  { label: 'PDF', value: 'pdf' },
                  { label: 'Markdown', value: 'markdown' },
                  { label: 'TXT', value: 'txt' },
                ]}
              />

              <Button
                icon={<ReloadOutlined />}
                onClick={() => loadDocuments(selectedKb.id)}
                loading={loadingDocs}
                title="刷新文档列表"
              />
            </div>

            <Space size={10}>
              {onNavigateToDebug && (
                <Button
                  icon={<SearchOutlined />}
                  onClick={() => onNavigateToDebug(selectedKb.id)}
                  style={{ borderRadius: 8 }}
                >
                  去调试检索质量
                </Button>
              )}
              <Button
                type="primary"
                icon={<UploadOutlined />}
                onClick={() => setUploadOpen(true)}
                style={{ borderRadius: 8, fontWeight: 500 }}
              >
                上传文档
              </Button>
            </Space>
          </div>

          {/* Documents Table */}
          <Table
            dataSource={filteredDocuments}
            columns={columns}
            rowKey="id"
            loading={loadingDocs}
            pagination={{
              pageSize: 10,
              showSizeChanger: true,
              pageSizeOptions: ['10', '20', '50'],
              showTotal: (total) => `共 ${total} 个文档${searchText || statusFilter !== 'ALL' || typeFilter !== 'ALL' ? ' (已筛选)' : ''}`,
            }}
            locale={{
              emptyText: (
                <Empty
                  description={
                    documents.length === 0
                      ? '当前知识库暂无文档，点击下方按钮上传首批文档'
                      : '未匹配到符合筛选条件的文档'
                  }
                  style={{ padding: '32px 0' }}
                >
                  {documents.length === 0 && (
                    <Button
                      type="primary"
                      icon={<UploadOutlined />}
                      onClick={() => setUploadOpen(true)}
                      style={{ marginTop: 8 }}
                    >
                      立即上传文档
                    </Button>
                  )}
                </Empty>
              ),
            }}
          />
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
            description="暂无选中的知识库，请先选择已有知识库或点击上方按钮创建新知识库"
          >
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setCreateKbOpen(true)}
              style={{ marginTop: 12, height: 38 }}
            >
              立即创建知识库
            </Button>
          </Empty>
        </div>
      )}

      {/* Create KB Modal */}
      <CreateKBModal
        open={createKbOpen}
        onClose={() => setCreateKbOpen(false)}
        onSuccess={(newKb) => {
          loadKnowledgeBases();
          setSelectedKbId(newKb.id);
        }}
      />

      {/* Upload Document Modal */}
      {selectedKb && (
        <UploadDocModal
          knowledgeBaseId={selectedKb.id}
          knowledgeBaseName={selectedKb.name}
          open={uploadOpen}
          onClose={() => setUploadOpen(false)}
          onSuccess={() => {
            loadDocuments(selectedKb.id);
            loadKnowledgeBases();
          }}
        />
      )}

      {/* Chunk Viewer Modal */}
      <ChunkViewerModal
        document={selectedDocForChunks}
        open={!!selectedDocForChunks}
        onClose={() => setSelectedDocForChunks(null)}
      />
    </div>
  );
};

export default KnowledgePage;
