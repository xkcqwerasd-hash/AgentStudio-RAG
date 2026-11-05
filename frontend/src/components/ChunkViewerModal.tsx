import React, { useEffect, useState } from 'react';
import { Modal, Table, Tag, Typography, Pagination } from 'antd';
import { getDocumentChunks } from '../api/knowledge';
import { DocumentChunk, DocumentItem } from '../types';

const { Text, Paragraph } = Typography;

interface ChunkViewerModalProps {
  document: DocumentItem | null;
  open: boolean;
  onClose: () => void;
}

export const ChunkViewerModal: React.FC<ChunkViewerModalProps> = ({
  document,
  open,
  onClose,
}) => {
  const [loading, setLoading] = useState(false);
  const [chunks, setChunks] = useState<DocumentChunk[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const pageSize = 10;

  useEffect(() => {
    if (open && document) {
      loadChunks(1);
    }
  }, [open, document]);

  const loadChunks = async (p: number) => {
    if (!document) return;
    setLoading(true);
    try {
      const data = await getDocumentChunks(document.id, p, pageSize);
      setChunks(data.items);
      setTotal(data.total);
      setPage(p);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const columns = [
    {
      title: '序号',
      dataIndex: 'chunk_index',
      key: 'chunk_index',
      width: 80,
      render: (idx: number) => <Tag color="blue">#{idx + 1}</Tag>,
    },
    {
      title: '页码',
      dataIndex: 'page_number',
      key: 'page_number',
      width: 90,
      render: (p: number) => <Tag color="cyan">第 {p} 页</Tag>,
    },
    {
      title: '字符数',
      dataIndex: 'char_length',
      key: 'char_length',
      width: 90,
      render: (len: number) => `${len} 字`,
    },
    {
      title: '切块文本内容 (Preview)',
      dataIndex: 'content',
      key: 'content',
      render: (text: string) => (
        <Paragraph
          ellipsis={{ rows: 3, expandable: true, symbol: '展开全文' }}
          style={{ margin: 0, fontSize: 13, lineHeight: 1.6 }}
        >
          {text}
        </Paragraph>
      ),
    },
  ];

  return (
    <Modal
      title={
        <div>
          <span>文档分块详情：</span>
          <Text strong style={{ color: '#1677ff' }}>
            {document?.file_name}
          </Text>
          <Text type="secondary" style={{ fontSize: 12, marginLeft: 12 }}>
            (共 {total} 个分块，已生成向量索引)
          </Text>
        </div>
      }
      open={open}
      onCancel={onClose}
      footer={null}
      width={850}
      destroyOnClose
    >
      <div style={{ marginTop: 16 }}>
        <Table
          dataSource={chunks}
          columns={columns}
          rowKey="id"
          loading={loading}
          pagination={false}
          size="middle"
        />
        <div style={{ marginTop: 16, textAlign: 'right' }}>
          <Pagination
            current={page}
            pageSize={pageSize}
            total={total}
            onChange={(p) => loadChunks(p)}
            showTotal={(t) => `共 ${t} 个 Chunk`}
            size="small"
          />
        </div>
      </div>
    </Modal>
  );
};
