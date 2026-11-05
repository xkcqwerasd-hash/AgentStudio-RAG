import React, { useRef, useState } from 'react';
import { Input, Button, Tooltip, Popconfirm, Typography, Space } from 'antd';
import {
  ArrowUpOutlined,
  StopOutlined,
  PaperClipOutlined,
  LoadingOutlined,
  CloudUploadOutlined,
  EyeOutlined,
  CloseOutlined,
  FilePdfOutlined,
  FileWordOutlined,
  FileTextOutlined,
} from '@ant-design/icons';
import { ConversationAttachment } from '../types';

const { Text } = Typography;
const { TextArea } = Input;

export interface ChatComposerProps {
  inputQuery: string;
  setInputQuery: (val: string) => void;
  sending: boolean;
  onSend: () => void;
  onStop: () => void;
  attachments: ConversationAttachment[];
  uploading: boolean;
  onUploadFiles: (files: FileList | File[]) => void;
  onDeleteAttachment: (attachmentId: string) => void;
  onPreviewAttachment: (attachmentId: string) => void;
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
}

const formatFileSize = (bytes: number): string => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
};

const getFormatIcon = (ext: string) => {
  const e = (ext || '').toLowerCase();
  if (e.includes('pdf')) return <FilePdfOutlined style={{ color: '#ef4444' }} />;
  if (e.includes('docx') || e.includes('doc')) return <FileWordOutlined style={{ color: '#2563eb' }} />;
  if (e.includes('md') || e.includes('markdown')) return <FileTextOutlined style={{ color: '#8b5cf6' }} />;
  return <FileTextOutlined style={{ color: '#10b981' }} />;
};

export const ChatComposer: React.FC<ChatComposerProps> = ({
  inputQuery,
  setInputQuery,
  sending,
  onSend,
  onStop,
  attachments,
  uploading,
  onUploadFiles,
  onDeleteAttachment,
  onPreviewAttachment,
  disabled = false,
  className,
  style,
}) => {
  const [isFocused, setIsFocused] = useState(false);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragCounterRef = useRef<number>(0);

  const isAtLimit = attachments.length >= 10;
  const canSend = Boolean(inputQuery.trim()) && !sending && !disabled;

  const handleDragEnter = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current += 1;
    if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
      setIsDragging(true);
    }
  };

  const handleDragLeave = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current -= 1;
    if (dragCounterRef.current <= 0) {
      dragCounterRef.current = 0;
      setIsDragging(false);
    }
  };

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current = 0;
    setIsDragging(false);
    if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
      onUploadFiles(e.dataTransfer.files);
    }
  };

  return (
    <div
      className={className}
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
      style={{
        maxWidth: 840,
        width: '100%',
        margin: '0 auto',
        position: 'relative',
        ...style,
      }}
    >
      {/* Drag & Drop Overlay */}
      {isDragging && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            backgroundColor: 'rgba(239, 246, 255, 0.94)',
            backdropFilter: 'blur(4px)',
            border: '2px dashed #2563eb',
            borderRadius: 20,
            zIndex: 30,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexDirection: 'column',
            gap: 6,
            pointerEvents: 'none',
            boxShadow: '0 8px 30px rgba(37, 99, 235, 0.15)',
          }}
        >
          <CloudUploadOutlined style={{ fontSize: 36, color: '#2563eb' }} />
          <div style={{ fontWeight: 600, color: '#1d4ed8', fontSize: 14 }}>
            松开鼠标即可添加聊天附件
          </div>
          <div style={{ fontSize: 12, color: '#64748b' }}>
            支持 PDF、DOCX、TXT、Markdown (单文件上限 20MB)
          </div>
        </div>
      )}

      {/* Floating Prompt Composer Card */}
      <div
        style={{
          backgroundColor: '#ffffff',
          borderRadius: 20,
          border: isFocused ? '1px solid #2563eb' : '1px solid #e2e8f0',
          boxShadow: isFocused
            ? '0 4px 24px rgba(37, 99, 235, 0.12), 0 0 0 1px #2563eb'
            : '0 4px 20px rgba(0, 0, 0, 0.05), 0 1px 3px rgba(0, 0, 0, 0.02)',
          padding: '12px 14px 10px 16px',
          transition: 'border-color 0.2s ease, box-shadow 0.2s ease',
          display: 'flex',
          flexDirection: 'column',
          gap: 6,
        }}
      >
        {/* Top Attached Files Ribbon */}
        {(attachments.length > 0 || uploading) && (
          <div
            style={{
              display: 'flex',
              gap: 8,
              overflowX: 'auto',
              paddingBottom: 6,
              borderBottom: '1px solid #f1f5f9',
              marginBottom: 4,
            }}
          >
            {attachments.map((att) => {
              const isCompleted = att.parse_status === 'COMPLETED';
              const isFailed = att.parse_status === 'FAILED';
              return (
                <div
                  key={att.id}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    backgroundColor: isFailed ? '#fff2f0' : '#f8fafc',
                    border: `1px solid ${isFailed ? '#fecaca' : '#e2e8f0'}`,
                    borderRadius: 8,
                    padding: '4px 8px 4px 10px',
                    fontSize: 12,
                    flexShrink: 0,
                    maxWidth: 220,
                    transition: 'all 0.15s ease',
                  }}
                >
                  <span style={{ fontSize: 14 }}>{getFormatIcon(att.file_extension)}</span>
                  <div
                    style={{
                      display: 'flex',
                      flexDirection: 'column',
                      minWidth: 0,
                      flex: 1,
                      cursor: 'pointer',
                    }}
                    onClick={() => onPreviewAttachment(att.id)}
                  >
                    <Tooltip title={`${att.original_filename} (${formatFileSize(att.file_size)}) - 点击预览解析文本`}>
                      <Text strong ellipsis style={{ fontSize: 12, color: '#1e293b' }}>
                        {att.original_filename}
                      </Text>
                    </Tooltip>
                    <Space size={6} style={{ fontSize: 10 }}>
                      <span style={{ color: '#64748b' }}>{formatFileSize(att.file_size)}</span>
                      {isCompleted ? (
                        <span style={{ color: '#16a34a', fontWeight: 500 }}>✓ 已就绪</span>
                      ) : isFailed ? (
                        <Tooltip title={att.error_message || '文件解析失败'}>
                          <span style={{ color: '#dc2626', fontWeight: 500 }}>✗ 解析失败</span>
                        </Tooltip>
                      ) : (
                        <span style={{ color: '#2563eb' }}>解析中...</span>
                      )}
                    </Space>
                  </div>

                  <Space size={2}>
                    <Tooltip title="查看解析内容">
                      <Button
                        type="text"
                        size="small"
                        icon={<EyeOutlined style={{ fontSize: 12, color: '#64748b' }} />}
                        onClick={() => onPreviewAttachment(att.id)}
                        style={{ padding: '0 4px', height: 22 }}
                      />
                    </Tooltip>
                    <Popconfirm
                      title="确定移除该附件？"
                      description="移除后该附件将不再参与本次会话的上下文检索。"
                      onConfirm={() => onDeleteAttachment(att.id)}
                      okText="移除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                    >
                      <Button
                        type="text"
                        size="small"
                        icon={<CloseOutlined style={{ fontSize: 11, color: '#94a3b8' }} />}
                        style={{ padding: '0 4px', height: 22 }}
                      />
                    </Popconfirm>
                  </Space>
                </div>
              );
            })}

            {uploading && (
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 6,
                  backgroundColor: '#eff6ff',
                  border: '1px dashed #93c5fd',
                  borderRadius: 8,
                  padding: '4px 12px',
                  fontSize: 12,
                  color: '#2563eb',
                  flexShrink: 0,
                }}
              >
                <LoadingOutlined />
                <span>正在上传并解析文件...</span>
              </div>
            )}
          </div>
        )}

        {/* Middle Auto-expanding Textarea */}
        <TextArea
          value={inputQuery}
          disabled={sending || disabled}
          onChange={(e) => setInputQuery(e.target.value)}
          onFocus={() => setIsFocused(true)}
          onBlur={() => setIsFocused(false)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !sending) {
              e.preventDefault();
              if (canSend) {
                onSend();
              }
            }
          }}
          autoSize={{ minRows: 2, maxRows: 8 }}
          placeholder={
            sending
              ? '回答正在实时生成中，您可以随时点击停止生成...'
              : attachments.length > 0
              ? '输入您的问题，将结合已解析附件与知识库进行综合分析... (Enter 发送，Shift+Enter 换行)'
              : '向 AgentStudio 发送提问... (Enter 发送，Shift+Enter 换行)'
          }
          bordered={false}
          style={{
            padding: '4px 2px',
            fontSize: 14.5,
            lineHeight: 1.6,
            color: '#1e293b',
            resize: 'none',
            boxShadow: 'none',
            backgroundColor: 'transparent',
          }}
        />

        {/* Bottom Toolbar & Action Buttons */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            paddingTop: 4,
            userSelect: 'none',
          }}
        >
          {/* Left: Attachments trigger & format hints */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Tooltip title={isAtLimit ? '已达单会话 10 个附件上限' : '添加附件 (PDF / DOCX / TXT / MD ≤20MB)'}>
              <Button
                type="text"
                icon={uploading ? <LoadingOutlined /> : <PaperClipOutlined style={{ fontSize: 16 }} />}
                disabled={uploading || isAtLimit}
                onClick={() => fileInputRef.current?.click()}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 4,
                  height: 32,
                  padding: '0 8px',
                  borderRadius: 8,
                  color: isAtLimit ? '#cbd5e1' : '#64748b',
                  backgroundColor: '#f8fafc',
                  border: '1px solid #e2e8f0',
                  fontSize: 12.5,
                }}
              >
                <span>添加附件</span>
                {attachments.length > 0 && (
                  <span style={{ fontSize: 11, color: '#2563eb', fontWeight: 600 }}>
                    ({attachments.length}/10)
                  </span>
                )}
              </Button>
            </Tooltip>

            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.docx,.txt,.md"
              style={{ display: 'none' }}
              onChange={(e) => {
                if (e.target.files && e.target.files.length > 0) {
                  onUploadFiles(e.target.files);
                }
                e.target.value = '';
              }}
            />

            <span style={{ fontSize: 11.5, color: '#94a3b8' }}>
              Shift + Enter 换行
            </span>
          </div>

          {/* Right: Circular Send / Stop Action Button */}
          <div>
            {sending ? (
              <Tooltip title="停止生成">
                <button
                  type="button"
                  onClick={onStop}
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: '50%',
                    backgroundColor: '#0f172a',
                    color: '#ffffff',
                    border: 'none',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    boxShadow: '0 2px 6px rgba(15, 23, 42, 0.25)',
                    transition: 'all 0.15s ease',
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = '#ef4444')}
                  onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = '#0f172a')}
                >
                  <StopOutlined style={{ fontSize: 15 }} />
                </button>
              </Tooltip>
            ) : (
              <Tooltip title={canSend ? '发送提问 (Enter)' : '请输入问题后发送'}>
                <button
                  type="button"
                  disabled={!canSend}
                  onClick={onSend}
                  style={{
                    width: 34,
                    height: 34,
                    borderRadius: '50%',
                    backgroundColor: canSend ? '#2563eb' : '#e2e8f0',
                    color: canSend ? '#ffffff' : '#94a3b8',
                    border: 'none',
                    cursor: canSend ? 'pointer' : 'not-allowed',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    boxShadow: canSend ? '0 2px 8px rgba(37, 99, 235, 0.3)' : 'none',
                    transition: 'all 0.2s cubic-bezier(0.4, 0, 0.2, 1)',
                  }}
                  onMouseEnter={(e) => {
                    if (canSend) e.currentTarget.style.backgroundColor = '#1d4ed8';
                  }}
                  onMouseLeave={(e) => {
                    if (canSend) e.currentTarget.style.backgroundColor = '#2563eb';
                  }}
                >
                  <ArrowUpOutlined style={{ fontSize: 16, fontWeight: 700 }} />
                </button>
              </Tooltip>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default ChatComposer;
