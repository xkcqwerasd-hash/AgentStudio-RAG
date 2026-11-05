import React from 'react';
import { Modal, Form, Input, InputNumber, message } from 'antd';
import { createKnowledgeBase } from '../api/knowledge';
import { KnowledgeBase } from '../types';

interface CreateKBModalProps {
  open: boolean;
  onClose: () => void;
  onSuccess: (kb: KnowledgeBase) => void;
}

export const CreateKBModal: React.FC<CreateKBModalProps> = ({
  open,
  onClose,
  onSuccess,
}) => {
  const [form] = Form.useForm();
  const [submitting, setSubmitting] = React.useState(false);

  const handleOk = async () => {
    try {
      const values = await form.validateFields();
      setSubmitting(true);
      const kb = await createKnowledgeBase(values);
      message.success(`知识库【${kb.name}】创建成功`);
      form.resetFields();
      onSuccess(kb);
      onClose();
    } catch (err: any) {
      if (err.message) {
        message.error(`创建失败: ${err.message}`);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      title="创建新企业知识库"
      open={open}
      onOk={handleOk}
      onCancel={onClose}
      confirmLoading={submitting}
      destroyOnClose
    >
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          chunk_size: 500,
          chunk_overlap: 50,
          embedding_model: 'BAAI/bge-m3',
        }}
      >
        <Form.Item
          name="name"
          label="知识库名称"
          rules={[{ required: true, message: '请输入知识库名称' }]}
        >
          <Input placeholder="例如：产品技术白皮书、HR规章制度" maxLength={100} />
        </Form.Item>

        <Form.Item name="description" label="知识库描述 (可选)">
          <Input.TextArea
            rows={3}
            placeholder="简要描述该知识库覆盖的业务范围及文档类型..."
            maxLength={300}
          />
        </Form.Item>

        <Form.Item
          name="chunk_size"
          label="分块字符大小 (Chunk Size)"
          tooltip="每个文本切块的最大字符数，建议 300~800 字"
        >
          <InputNumber min={100} max={2000} step={50} style={{ width: '100%' }} />
        </Form.Item>

        <Form.Item
          name="chunk_overlap"
          label="分块重叠大小 (Chunk Overlap)"
          tooltip="相邻切块之间的重叠字符数，用于保留上下文连续性，建议为切块大小的 10%"
        >
          <InputNumber min={0} max={500} step={10} style={{ width: '100%' }} />
        </Form.Item>
      </Form>
    </Modal>
  );
};
