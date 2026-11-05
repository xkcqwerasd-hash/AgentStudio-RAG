import React, { useState } from 'react';
import { Modal, Upload, Button, message, Alert, Typography } from 'antd';
import { InboxOutlined } from '@ant-design/icons';
import type { UploadFile } from 'antd/es/upload/interface';
import { uploadDocument } from '../api/knowledge';

const { Dragger } = Upload;
const { Text } = Typography;

interface UploadDocModalProps {
  knowledgeBaseId: string;
  knowledgeBaseName: string;
  open: boolean;
  onClose: () => void;
  onSuccess: () => void;
}

export const UploadDocModal: React.FC<UploadDocModalProps> = ({
  knowledgeBaseId,
  knowledgeBaseName,
  open,
  onClose,
  onSuccess,
}) => {
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [uploading, setUploading] = useState(false);

  const handleUpload = async () => {
    if (fileList.length === 0) {
      message.warning('请先选择要上传的文件');
      return;
    }

    setUploading(true);
    let successCount = 0;
    let failCount = 0;

    for (const fileItem of fileList) {
      const originalFile = fileItem.originFileObj as File;
      try {
        await uploadDocument(knowledgeBaseId, originalFile);
        successCount++;
      } catch (err: any) {
        failCount++;
        message.error(`文件 ${fileItem.name} 处理失败: ${err.message}`);
      }
    }

    setUploading(false);
    if (successCount > 0) {
      message.success(`成功上传并解析 ${successCount} 个文档`);
      setFileList([]);
      onSuccess();
      onClose();
    }
  };

  const uploadProps = {
    onRemove: (file: UploadFile) => {
      setFileList((prev) => prev.filter((item) => item.uid !== file.uid));
    },
    beforeUpload: (file: File) => {
      const ext = file.name.substring(file.name.lastIndexOf('.')).toLowerCase();
      if (!['.pdf', '.md', '.markdown', '.txt'].includes(ext)) {
        message.error(`不支持的文件格式 ${ext}，仅支持 .pdf, .md, .txt`);
        return Upload.LIST_IGNORE;
      }
      setFileList((prev) => [...prev, file as unknown as UploadFile]);
      return false; // prevent automatic antd upload
    },
    fileList,
    multiple: true,
  };

  return (
    <Modal
      title={
        <div>
          <span>上传文档至知识库：</span>
          <Text strong style={{ color: '#1677ff' }}>
            {knowledgeBaseName}
          </Text>
        </div>
      }
      open={open}
      onCancel={onClose}
      footer={[
        <Button key="back" onClick={onClose} disabled={uploading}>
          取消
        </Button>,
        <Button
          key="submit"
          type="primary"
          loading={uploading}
          onClick={handleUpload}
          disabled={fileList.length === 0}
        >
          {uploading ? '正在解析并建立向量索引...' : '开始解析并建立索引'}
        </Button>,
      ]}
      destroyOnClose
    >
      <Alert
        message="文档入库流水线说明"
        description="系统将调用 PyMuPDF 逐页高精度提取文本与页码，按设定的滑动窗口分块，自动生成向量索引并落库。所有分块均保留原始页码以提供防幻觉引用。"
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
      />

      <Dragger {...uploadProps} style={{ padding: '24px 0' }}>
        <p className="ant-upload-drag-icon">
          <InboxOutlined style={{ color: '#1677ff', fontSize: 44 }} />
        </p>
        <p className="ant-upload-text" style={{ fontSize: 15, fontWeight: 500 }}>
          点击或将文档拖拽至此区域
        </p>
        <p className="ant-upload-hint" style={{ color: '#8c8c8c', fontSize: 13 }}>
          支持单文件或批量上传：<strong>PDF (.pdf)</strong>、<strong>Markdown (.md)</strong>、<strong>纯文本 (.txt)</strong>，单个文件上限 20MB。
        </p>
      </Dragger>
    </Modal>
  );
};
