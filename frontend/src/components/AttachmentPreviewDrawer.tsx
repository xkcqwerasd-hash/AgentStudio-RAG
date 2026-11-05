import React, { useEffect, useState } from 'react';
import {
  Drawer,
  Typography,
  Space,
  Tag,
  Tabs,
  Card,
  Button,
  Spin,
  Alert,
  message,
} from 'antd';
import {
  FileTextOutlined,
  FilePdfOutlined,
  FileWordOutlined,
  CopyOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  LoadingOutlined,
} from '@ant-design/icons';
import { ConversationAttachmentDetail } from '../types';
import { getAttachmentDetail } from '../api/attachment';

const { Text, Paragraph } = Typography;

interface AttachmentPreviewDrawerProps {
  open: boolean;
  onClose: () => void;
  conversationId?: string;
  attachmentId?: string;
}

export const AttachmentPreviewDrawer: React.FC<AttachmentPreviewDrawerProps> = ({
  open,
  onClose,
  conversationId,
  attachmentId,
}) => {
  const [loading, setLoading] = useState<boolean>(false);
  const [detail, setDetail] = useState<ConversationAttachmentDetail | null>(null);

  useEffect(() => {
    if (open && conversationId && attachmentId) {
      setLoading(true);
      getAttachmentDetail(conversationId, attachmentId)
        .then((res) => {
          setDetail(res);
        })
        .catch((err) => {
          message.error(`获取附件详情失败: ${err.message || '未知错误'}`);
          setDetail(null);
        })
        .finally(() => {
          setLoading(false);
        });
    } else {
      setDetail(null);
    }
  }, [open, conversationId, attachmentId]);

  const handleCopy = (text: string) => {
    navigator.clipboard.writeText(text);
    message.success('已复制到剪贴板');
  };

  const getFormatIcon = (ext: string) => {
    const e = ext.toLowerCase();
    if (e.includes('pdf')) return <FilePdfOutlined style={{ color: '#ff4d4f' }} />;
    if (e.includes('docx') || e.includes('doc')) return <FileWordOutlined style={{ color: '#1677ff' }} />;
    if (e.includes('md') || e.includes('markdown')) return <FileTextOutlined style={{ color: '#722ed1' }} />;
    return <FileTextOutlined style={{ color: '#52c41a' }} />;
  };

  const formatFileSize = (bytes: number): string => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  };

  const formatHeadingPath = (heading: any): string => {
    if (!heading) return '';
    if (Array.isArray(heading)) return heading.join(' > ');
    return String(heading);
  };

  return (
    <Drawer
      title={
        detail ? (
          <Space>
            {getFormatIcon(detail.file_extension)}
            <Text strong ellipsis style={{ maxWidth: 360 }}>
              {detail.original_filename}
            </Text>
          </Space>
        ) : (
          '附件详情预览'
        )
      }
      placement="right"
      width={560}
      open={open}
      onClose={onClose}
    >
      {loading ? (
        <div style={{ textAlign: 'center', padding: '60px 0' }}>
          <Spin size="large" indicator={<LoadingOutlined />} />
          <div style={{ marginTop: 16, color: '#8c8c8c' }}>正在加载解析内容...</div>
        </div>
      ) : !detail ? (
        <div style={{ textAlign: 'center', padding: '60px 0', color: '#8c8c8c' }}>
          暂无附件内容或加载失败
        </div>
      ) : (
        <div>
          {/* Metadata Bar */}
          <div
            style={{
              backgroundColor: '#f8fafc',
              border: '1px solid #e2e8f0',
              borderRadius: 8,
              padding: '12px 16px',
              marginBottom: 16,
            }}
          >
            <Space wrap size={[12, 8]}>
              <Tag color="blue">{detail.file_extension.toUpperCase().replace('.', '')}</Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                大小: <strong>{formatFileSize(detail.file_size)}</strong>
              </Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                分块/页数: <strong>{detail.page_count}</strong>
              </Text>
              <Text type="secondary" style={{ fontSize: 12 }}>
                预估 Token: <strong>~{detail.token_count}</strong>
              </Text>
              {detail.parse_status === 'COMPLETED' ? (
                <Tag color="success" icon={<CheckCircleOutlined />}>
                  解析就绪
                </Tag>
              ) : detail.parse_status === 'FAILED' ? (
                <Tag color="error" icon={<CloseCircleOutlined />}>
                  解析失败
                </Tag>
              ) : (
                <Tag color="processing" icon={<LoadingOutlined />}>
                  解析中
                </Tag>
              )}
            </Space>
          </div>

          {detail.error_message && (
            <Alert
              type="error"
              showIcon
              message="文件解析异常"
              description={detail.error_message}
              style={{ marginBottom: 16 }}
            />
          )}

          {/* Content Tabs */}
          <Tabs
            defaultActiveKey="chunks"
            items={[
              {
                key: 'chunks',
                label: `结构化分块 (${detail.parsed_pages.length})`,
                children: (
                  <Space direction="vertical" style={{ width: '100%' }} size={12}>
                    {detail.parsed_pages.map((p, idx) => {
                      const ext = detail.file_extension.toLowerCase();
                      const meta = p.metadata || {};
                      let provenanceLabel = `第 ${p.page_number} 页`;
                      if (ext.includes('docx')) {
                        const h = formatHeadingPath(meta.heading_path);
                        const b = meta.block_index ?? p.page_number;
                        provenanceLabel = h ? `章节: ${h} | 段落 #${b}` : `段落 #${b}`;
                      } else if (ext.includes('md')) {
                        const h = formatHeadingPath(meta.heading_path);
                        provenanceLabel = h ? `章节: ${h}` : `分块 #${p.page_number}`;
                      } else if (ext.includes('txt')) {
                        const start = meta.line_start ?? 1;
                        const end = meta.line_end ?? start;
                        provenanceLabel = `行号: 第 ${start}-${end} 行`;
                      }

                      return (
                        <Card
                          key={idx}
                          size="small"
                          style={{
                            borderLeft: '4px solid #1677ff',
                            backgroundColor: '#ffffff',
                          }}
                          title={
                            <Space>
                              <Tag color="geekblue" style={{ fontSize: 11 }}>
                                #{idx + 1}
                              </Tag>
                              <Text strong style={{ fontSize: 12 }}>
                                {provenanceLabel}
                              </Text>
                            </Space>
                          }
                          extra={
                            <Button
                              type="text"
                              size="small"
                              icon={<CopyOutlined />}
                              onClick={() => handleCopy(p.text)}
                            >
                              复制
                            </Button>
                          }
                        >
                          <div
                            style={{
                              backgroundColor: '#f9f9f9',
                              padding: '8px 12px',
                              borderRadius: 6,
                              fontSize: 12,
                              lineHeight: 1.6,
                              maxHeight: 180,
                              overflowY: 'auto',
                            }}
                          >
                            <Paragraph style={{ margin: 0, whiteSpace: 'pre-wrap' }}>
                              {p.text}
                            </Paragraph>
                          </div>
                        </Card>
                      );
                    })}
                  </Space>
                ),
              },
              {
                key: 'full_text',
                label: `提取全文 (${detail.char_length} 字符)`,
                children: (
                  <div>
                    <div style={{ marginBottom: 8, textAlign: 'right' }}>
                      <Button
                        size="small"
                        icon={<CopyOutlined />}
                        onClick={() => handleCopy(detail.extracted_text)}
                      >
                        复制全部提取文本
                      </Button>
                    </div>
                    <div
                      style={{
                        backgroundColor: '#f8fafc',
                        border: '1px solid #e2e8f0',
                        borderRadius: 6,
                        padding: 12,
                        maxHeight: 'calc(100vh - 300px)',
                        overflowY: 'auto',
                        fontSize: 12,
                        lineHeight: 1.6,
                        whiteSpace: 'pre-wrap',
                        fontFamily: 'monospace',
                      }}
                    >
                      {detail.extracted_text || '未提取到文本内容'}
                    </div>
                  </div>
                ),
              },
            ]}
          />
        </div>
      )}
    </Drawer>
  );
};
