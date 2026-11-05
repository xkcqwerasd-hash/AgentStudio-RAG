import React from 'react';
import { Drawer, Card, Tag, Typography, Space, Button, message } from 'antd';
import { FileTextOutlined, CopyOutlined } from '@ant-design/icons';
import { Citation } from '../types';

const { Text, Paragraph } = Typography;

interface CitationDrawerProps {
  open: boolean;
  onClose: () => void;
  citations: Citation[];
  selectedIndex?: number;
}

export const getProvenanceDisplay = (c: Citation): { label: string; tag: string; tagColor: string } => {
  const src = (c.source_type || '').toLowerCase();
  const name = (c.document_name || '').toLowerCase();

  const isDocx = src === 'docx' || name.endsWith('.docx') || name.endsWith('.doc');
  const isMd = src === 'md' || src === 'markdown' || name.endsWith('.md');
  const isTxt = src === 'txt' || name.endsWith('.txt');
  const isPdf = src === 'pdf' || name.endsWith('.pdf');

  if (c.provenance_label) {
    if (isDocx) return { label: c.provenance_label, tag: 'DOCX 附件', tagColor: 'blue' };
    if (isMd) return { label: c.provenance_label, tag: 'Markdown 附件', tagColor: 'purple' };
    if (isTxt) return { label: c.provenance_label, tag: 'TXT 附件', tagColor: 'green' };
    if (isPdf) return { label: c.provenance_label, tag: 'PDF 来源', tagColor: 'volcano' };
    return { label: c.provenance_label, tag: '参考证据', tagColor: 'cyan' };
  }

  if (isDocx) {
    const heading = Array.isArray(c.heading_path) ? c.heading_path.join(' > ') : (c.heading_path || '');
    const block =
      c.block_index !== undefined && c.block_index !== null
        ? `段落 #${c.block_index}`
        : `逻辑块 #${c.page_number}`;
    const label = heading ? `章节: ${heading} | ${block}` : block;
    return { label, tag: 'DOCX 附件', tagColor: 'blue' };
  }

  if (isMd) {
    const heading = Array.isArray(c.heading_path) ? c.heading_path.join(' > ') : (c.heading_path || '');
    const label = heading ? `章节: ${heading}` : `分块 #${c.page_number}`;
    return { label, tag: 'Markdown 附件', tagColor: 'purple' };
  }

  if (isTxt) {
    const start = c.line_start ?? 1;
    const end = c.line_end ?? start;
    const label = `行号: 第 ${start}-${end} 行`;
    return { label, tag: 'TXT 附件', tagColor: 'green' };
  }

  return {
    label: `原始页码: 第 ${c.page_number} 页`,
    tag: src === 'attachment' ? '会话附件' : '知识库文档',
    tagColor: src === 'attachment' ? 'geekblue' : 'blue',
  };
};

export const CitationDrawer: React.FC<CitationDrawerProps> = ({
  open,
  onClose,
  citations,
  selectedIndex,
}) => {
  const handleCopy = (text: string) => {
    navigator.clipboard.writeText(text);
    message.success('已复制片段内容到剪贴板');
  };

  return (
    <Drawer
      title={
        <Space>
          <FileTextOutlined style={{ color: '#1677ff' }} />
          <span>引用事实依据溯源 ({citations.length} 处)</span>
        </Space>
      }
      placement="right"
      width={480}
      open={open}
      onClose={onClose}
    >
      <div style={{ marginBottom: 16 }}>
        <Text type="secondary" style={{ fontSize: 13 }}>
          以下为本次回答引用的真实文档切块证据，均保留原始文档文件名、页码与检索相关度得分，杜绝大模型凭空幻觉。
        </Text>
      </div>

      <Space direction="vertical" style={{ width: '100%' }} size={16}>
        {citations.map((c) => {
          const isSelected = selectedIndex === c.citation_index;
          const prov = getProvenanceDisplay(c);
          return (
            <Card
              key={c.chunk_id || c.citation_index}
              size="small"
              style={{
                borderLeft: isSelected ? '4px solid #1677ff' : '4px solid #d9d9d9',
                backgroundColor: isSelected ? '#f0f7ff' : '#ffffff',
                boxShadow: isSelected ? '0 2px 8px rgba(22, 119, 255, 0.15)' : 'none',
              }}
              title={
                <Space>
                  <Tag color="blue" style={{ fontWeight: 600 }}>
                    [Ref-{c.citation_index}]
                  </Tag>
                  <Text strong ellipsis style={{ maxWidth: 200 }}>
                    {c.document_name}
                  </Text>
                </Space>
              }
              extra={
                <Space size={6}>
                  <Tag color={prov.tagColor} style={{ margin: 0, fontSize: 11 }}>
                    {prov.tag}
                  </Tag>
                  <Tag color={c.similarity_score > 0.3 ? 'success' : 'processing'} style={{ margin: 0 }}>
                    {(c.similarity_score * 100).toFixed(0)}%
                  </Tag>
                </Space>
              }
            >
              <div style={{ marginBottom: 8 }}>
                <Space size={16} wrap>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    <strong>{prov.label}</strong>
                  </Text>
                  <Text type="secondary" style={{ fontSize: 11 }}>
                    分块 ID: <Text copyable={{ text: c.chunk_id }}>{c.chunk_id.slice(0, 8)}...</Text>
                  </Text>
                </Space>
              </div>

              <div
                style={{
                  backgroundColor: '#f9f9f9',
                  padding: '8px 12px',
                  borderRadius: 6,
                  border: '1px solid #f0f0f0',
                  fontSize: 13,
                  lineHeight: 1.6,
                }}
              >
                <Paragraph style={{ margin: 0 }}>{c.content_snippet}</Paragraph>
              </div>

              <div style={{ marginTop: 8, textAlign: 'right' }}>
                <Button
                  size="small"
                  type="text"
                  icon={<CopyOutlined />}
                  onClick={() => handleCopy(c.content_snippet)}
                >
                  复制证据原文
                </Button>
              </div>
            </Card>
          );
        })}
      </Space>
    </Drawer>
  );
};
