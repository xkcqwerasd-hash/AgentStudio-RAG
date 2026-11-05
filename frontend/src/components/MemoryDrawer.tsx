import React, { useEffect, useState, useMemo } from 'react';
import {
  Drawer,
  Space,
  Button,
  Tag,
  Typography,
  Switch,
  Input,
  Popconfirm,
  Modal,
  Form,
  Select,
  Tooltip,
  message,
  Empty,
  Spin,
} from 'antd';
import {
  BulbOutlined,
  PlusOutlined,
  DeleteOutlined,
  EditOutlined,
  SearchOutlined,
  ReloadOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  EyeOutlined,
  RobotOutlined,
  UserOutlined,
} from '@ant-design/icons';
import {
  getMemories,
  getMemoryStats,
  getMemorySettings,
  updateMemorySettings,
  createMemory,
  updateMemory,
  toggleMemoryStatus,
  deleteMemory,
  clearAllMemories,
} from '../api/memory';
import { MemoryItem, MemoryStats } from '../types';

const { Text } = Typography;

interface MemoryDrawerProps {
  open: boolean;
  onClose: () => void;
  onMemoryChanged?: () => void;
}

const TYPE_CONFIG: Record<
  string,
  { label: string; color: string; border: string; bg: string; icon: string }
> = {
  USER_PROFILE: {
    label: '用户画像',
    color: '#0891b2',
    border: '#a5f3fc',
    bg: '#ecfeff',
    icon: '👤',
  },
  PROJECT_CONTEXT: {
    label: '项目背景',
    color: '#2563eb',
    border: '#bfdbfe',
    bg: '#eff6ff',
    icon: '🏢',
  },
  IMPORTANT_FACT: {
    label: '核心事实',
    color: '#16a34a',
    border: '#bbf7d0',
    bg: '#f0fdf4',
    icon: '💡',
  },
  DIRECT_RULE: {
    label: '约定规则',
    color: '#ea580c',
    border: '#fed7aa',
    bg: '#fff7ed',
    icon: '📜',
  },
};

export const MemoryDrawer: React.FC<MemoryDrawerProps> = ({
  open,
  onClose,
  onMemoryChanged,
}) => {
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [autoMemoryEnabled, setAutoMemoryEnabled] = useState<boolean>(true);
  const [loading, setLoading] = useState<boolean>(false);
  const [togglingSetting, setTogglingSetting] = useState<boolean>(false);

  // Filter & Search state
  const [searchQuery, setSearchQuery] = useState<string>('');
  const [selectedTab, setSelectedTab] = useState<string>('ALL');

  // Edit modal state
  const [editModalVisible, setEditModalVisible] = useState<boolean>(false);
  const [editingItem, setEditingItem] = useState<MemoryItem | null>(null);
  const [form] = Form.useForm();

  // Create modal state
  const [createModalVisible, setCreateModalVisible] = useState<boolean>(false);
  const [createForm] = Form.useForm();

  const loadData = async () => {
    setLoading(true);
    try {
      const [listRes, statsRes, settingsRes] = await Promise.all([
        getMemories({ page: 1, page_size: 100 }),
        getMemoryStats(),
        getMemorySettings(),
      ]);
      setMemories(listRes.items);
      setStats(statsRes);
      setAutoMemoryEnabled(settingsRes.auto_memory_enabled);
    } catch (err: any) {
      message.error(`加载长期记忆失败: ${err.message || '网络异常'}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) {
      loadData();
    }
  }, [open]);

  const handleToggleAutoMemory = async (checked: boolean) => {
    setTogglingSetting(true);
    try {
      await updateMemorySettings({ auto_memory_enabled: checked });
      setAutoMemoryEnabled(checked);
      if (stats) {
        setStats({ ...stats, auto_memory_enabled: checked });
      }
      message.success(
        checked ? '已开启对话记忆自动沉淀' : '已关闭自动沉淀，已有记忆仍可正常使用'
      );
      onMemoryChanged?.();
    } catch (err: any) {
      message.error(`切换自动记忆设置失败: ${err.message || '未知错误'}`);
    } finally {
      setTogglingSetting(false);
    }
  };

  const handleToggleStatus = async (item: MemoryItem, newActive: boolean) => {
    try {
      const updated = await toggleMemoryStatus(item.id, newActive);
      setMemories((prev) => prev.map((m) => (m.id === item.id ? updated : m)));
      if (stats) {
        setStats({
          ...stats,
          active_memories: stats.active_memories + (newActive ? 1 : -1),
        });
      }
      message.success(newActive ? '记忆已启用' : '记忆已停用 (不再参与检索召回)');
      onMemoryChanged?.();
    } catch (err: any) {
      message.error(`更新状态失败: ${err.message || '未知错误'}`);
    }
  };

  const handleDeleteItem = async (id: string) => {
    try {
      await deleteMemory(id);
      setMemories((prev) => prev.filter((m) => m.id !== id));
      if (stats) {
        setStats({
          ...stats,
          total_memories: Math.max(0, stats.total_memories - 1),
        });
      }
      message.success('记忆已永久删除');
      onMemoryChanged?.();
    } catch (err: any) {
      message.error(`删除记忆失败: ${err.message || '未知错误'}`);
    }
  };

  const handleClearAll = async () => {
    try {
      const res = await clearAllMemories();
      setMemories([]);
      if (stats) {
        setStats({
          total_memories: 0,
          active_memories: 0,
          profile_count: 0,
          background_count: 0,
          fact_count: 0,
          rule_count: 0,
          auto_memory_enabled: stats.auto_memory_enabled,
        });
      }
      message.success(`已清空 ${res.deleted_count} 条长期记忆`);
      onMemoryChanged?.();
    } catch (err: any) {
      message.error(`清空记忆失败: ${err.message || '未知错误'}`);
    }
  };

  const handleOpenEdit = (item: MemoryItem) => {
    setEditingItem(item);
    form.setFieldsValue({
      content: item.content,
      memory_type: item.memory_type,
    });
    setEditModalVisible(true);
  };

  const handleSaveEdit = async () => {
    try {
      const values = await form.validateFields();
      if (!editingItem) return;
      const updated = await updateMemory(editingItem.id, values);
      setMemories((prev) => prev.map((m) => (m.id === editingItem.id ? updated : m)));
      message.success('记忆已成功更新');
      setEditModalVisible(false);
      onMemoryChanged?.();
    } catch (err: any) {
      if (err.errorFields) return;
      message.error(`保存更新失败: ${err.message || '未知错误'}`);
    }
  };

  const handleCreateNew = async () => {
    try {
      const values = await createForm.validateFields();
      const created = await createMemory(values);
      setMemories((prev) => [created, ...prev]);
      if (stats) {
        setStats({
          ...stats,
          total_memories: stats.total_memories + 1,
          active_memories: stats.active_memories + 1,
        });
      }
      message.success('已成功添加新记忆');
      createForm.resetFields();
      setCreateModalVisible(false);
      onMemoryChanged?.();
    } catch (err: any) {
      if (err.errorFields) return;
      message.error(`添加记忆失败: ${err.message || '未知错误'}`);
    }
  };

  // Filtered memories
  const filteredMemories = useMemo(() => {
    return memories.filter((m) => {
      const matchTab = selectedTab === 'ALL' || m.memory_type === selectedTab;
      const matchSearch =
        !searchQuery.trim() ||
        m.content.toLowerCase().includes(searchQuery.trim().toLowerCase());
      return matchTab && matchSearch;
    });
  }, [memories, selectedTab, searchQuery]);

  return (
    <Drawer
      title={
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%' }}>
          <Space size={8}>
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: 6,
                backgroundColor: '#eff6ff',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                color: '#2563eb',
              }}
            >
              <BulbOutlined style={{ fontSize: 16 }} />
            </div>
            <span style={{ fontSize: 16, fontWeight: 600, color: '#1e293b' }}>
              长期记忆库
            </span>
            <Tag color="blue" style={{ borderRadius: 10, fontSize: 12, margin: 0 }}>
              {stats?.total_memories ?? memories.length} 条事实
            </Tag>
          </Space>

          <Space size={12}>
            <Tooltip title="开启后，正常完成的对话将在后台自动提炼有价值的技术偏好与事实约定">
              <Space size={6} style={{ cursor: 'pointer' }}>
                <Text style={{ fontSize: 12.5, color: '#475569' }}>自动沉淀</Text>
                <Switch
                  size="small"
                  checked={autoMemoryEnabled}
                  loading={togglingSetting}
                  onChange={handleToggleAutoMemory}
                />
              </Space>
            </Tooltip>

            <Button
              size="small"
              type="text"
              icon={<ReloadOutlined />}
              onClick={loadData}
              title="刷新数据"
            />
          </Space>
        </div>
      }
      placement="right"
      width={620}
      onClose={onClose}
      open={open}
      styles={{
        body: {
          backgroundColor: '#fafbfc',
          padding: '16px 20px',
          overflowY: 'auto',
        },
      }}
    >
      {/* 1. Statistics Cards Overview */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(5, 1fr)',
          gap: 8,
          marginBottom: 16,
        }}
      >
        <div
          onClick={() => setSelectedTab('ALL')}
          style={{
            backgroundColor: selectedTab === 'ALL' ? '#eff6ff' : '#ffffff',
            border: selectedTab === 'ALL' ? '1.5px solid #2563eb' : '1px solid #e2e8f0',
            borderRadius: 10,
            padding: '10px 8px',
            textAlign: 'center',
            cursor: 'pointer',
            transition: 'all 0.15s ease',
          }}
        >
          <div style={{ fontSize: 11, color: '#64748b' }}>全部</div>
          <div style={{ fontSize: 17, fontWeight: 700, color: '#1e293b', marginTop: 2 }}>
            {stats?.total_memories ?? 0}
          </div>
        </div>

        {Object.entries(TYPE_CONFIG).map(([typeKey, cfg]) => {
          const count =
            typeKey === 'USER_PROFILE'
              ? stats?.profile_count ?? 0
              : typeKey === 'PROJECT_CONTEXT'
              ? stats?.background_count ?? 0
              : typeKey === 'IMPORTANT_FACT'
              ? stats?.fact_count ?? 0
              : stats?.rule_count ?? 0;
          const active = selectedTab === typeKey;
          return (
            <div
              key={typeKey}
              onClick={() => setSelectedTab(typeKey)}
              style={{
                backgroundColor: active ? cfg.bg : '#ffffff',
                border: active ? `1.5px solid ${cfg.color}` : '1px solid #e2e8f0',
                borderRadius: 10,
                padding: '10px 8px',
                textAlign: 'center',
                cursor: 'pointer',
                transition: 'all 0.15s ease',
              }}
            >
              <div style={{ fontSize: 11, color: cfg.color, fontWeight: 500 }}>
                {cfg.icon} {cfg.label}
              </div>
              <div style={{ fontSize: 17, fontWeight: 700, color: '#1e293b', marginTop: 2 }}>
                {count}
              </div>
            </div>
          );
        })}
      </div>

      {/* 2. Action Toolbar: Search + Add + Clear */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          marginBottom: 14,
        }}
      >
        <Input
          placeholder="搜索记忆内容..."
          prefix={<SearchOutlined style={{ color: '#94a3b8' }} />}
          value={searchQuery}
          onChange={(e) => setSearchQuery(e.target.value)}
          allowClear
          style={{ flex: 1, borderRadius: 8, height: 34 }}
        />

        <Space size={8}>
          <Button
            type="primary"
            icon={<PlusOutlined />}
            size="middle"
            onClick={() => setCreateModalVisible(true)}
            style={{
              borderRadius: 8,
              backgroundColor: '#2563eb',
              fontSize: 13,
              height: 34,
            }}
          >
            添加记忆
          </Button>

          {memories.length > 0 && (
            <Popconfirm
              title="确定清空全部长期记忆？"
              description="清空后所有用户记忆将被永久物理清理，三大执行引擎将不再使用这些偏好与背景。"
              okText="彻底清空"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={handleClearAll}
            >
              <Button
                danger
                icon={<DeleteOutlined />}
                size="middle"
                style={{ borderRadius: 8, height: 34, fontSize: 13 }}
              >
                一键清空
              </Button>
            </Popconfirm>
          )}
        </Space>
      </div>

      {/* 3. Memory Card List */}
      {loading ? (
        <div style={{ textAlign: 'center', padding: '60px 0' }}>
          <Spin size="large" />
          <div style={{ marginTop: 12, color: '#64748b', fontSize: 13 }}>正在读取用户专属长期记忆...</div>
        </div>
      ) : filteredMemories.length === 0 ? (
        <div
          style={{
            backgroundColor: '#ffffff',
            borderRadius: 12,
            border: '1px dashed #cbd5e1',
            padding: '48px 24px',
            textAlign: 'center',
            marginTop: 8,
          }}
        >
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={
              <div>
                <div style={{ fontSize: 14, fontWeight: 500, color: '#475569', marginBottom: 6 }}>
                  {searchQuery ? '未找到匹配的记忆记录' : '暂无长期记忆'}
                </div>
                <div style={{ fontSize: 12.5, color: '#94a3b8', maxWidth: 360, margin: '0 auto' }}>
                  系统会在对话完成后自动提取有价值的项目背景、技术偏好与约定规则，也可以点击上方「添加记忆」主动录入。
                </div>
              </div>
            }
          />
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {filteredMemories.map((item) => {
            const cfg = TYPE_CONFIG[item.memory_type] || TYPE_CONFIG.IMPORTANT_FACT;
            const isAuto = item.source_type === 'AUTO_EXTRACTED';
            return (
              <div
                key={item.id}
                style={{
                  backgroundColor: '#ffffff',
                  borderRadius: 12,
                  border: item.is_active ? '1px solid #e2e8f0' : '1px dashed #cbd5e1',
                  boxShadow: '0 1px 2px rgba(0, 0, 0, 0.03)',
                  padding: '14px 16px',
                  opacity: item.is_active ? 1 : 0.65,
                  transition: 'all 0.15s ease',
                }}
              >
                {/* Header Row: Type Tag, Source Tag, Active Switch, Actions */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: 8,
                  }}
                >
                  <Space size={6} wrap>
                    <Tag
                      style={{
                        backgroundColor: cfg.bg,
                        borderColor: cfg.border,
                        color: cfg.color,
                        borderRadius: 6,
                        fontWeight: 600,
                        fontSize: 11.5,
                        margin: 0,
                      }}
                    >
                      {cfg.icon} {cfg.label}
                    </Tag>

                    <Tag
                      icon={isAuto ? <RobotOutlined /> : <UserOutlined />}
                      style={{
                        borderRadius: 6,
                        fontSize: 11,
                        margin: 0,
                        backgroundColor: isAuto ? '#f5f3ff' : '#f8fafc',
                        borderColor: isAuto ? '#ddd6fe' : '#e2e8f0',
                        color: isAuto ? '#7c3aed' : '#64748b',
                      }}
                    >
                      {isAuto ? '自动提取' : '主动录入'}
                    </Tag>

                    {!item.is_active && (
                      <Tag color="default" style={{ borderRadius: 6, fontSize: 11, margin: 0 }}>
                        已停用 (不召回)
                      </Tag>
                    )}
                  </Space>

                  <Space size={10}>
                    <Tooltip title={item.is_active ? '点击停用（不再参与检索）' : '点击启用'}>
                      <Switch
                        size="small"
                        checked={item.is_active}
                        onChange={(checked) => handleToggleStatus(item, checked)}
                      />
                    </Tooltip>

                    <Button
                      type="text"
                      size="small"
                      icon={<EditOutlined style={{ fontSize: 13, color: '#64748b' }} />}
                      onClick={() => handleOpenEdit(item)}
                      style={{ padding: '0 4px', height: 24 }}
                    />

                    <Popconfirm
                      title="确定删除此条记忆？"
                      description="删除后该记忆将物理清除且无法再被检索使用。"
                      okText="删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                      onConfirm={() => handleDeleteItem(item.id)}
                    >
                      <Button
                        type="text"
                        size="small"
                        icon={<DeleteOutlined style={{ fontSize: 13, color: '#ef4444' }} />}
                        style={{ padding: '0 4px', height: 24 }}
                      />
                    </Popconfirm>
                  </Space>
                </div>

                {/* Content text */}
                <div
                  style={{
                    fontSize: 13.5,
                    lineHeight: 1.6,
                    color: item.is_active ? '#1e293b' : '#64748b',
                    marginBottom: 10,
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >
                  {item.content}
                </div>

                {/* Footer Row: Metadata stats */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    fontSize: 11,
                    color: '#94a3b8',
                    borderTop: '1px solid #f1f5f9',
                    paddingTop: 8,
                  }}
                >
                  <Space size={12}>
                    <span>
                      <ClockCircleOutlined style={{ marginRight: 3 }} />
                      {new Date(item.created_at).toLocaleString('zh-CN', {
                        month: 'numeric',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                    <span>
                      <EyeOutlined style={{ marginRight: 3 }} />
                      访问 {item.access_count} 次
                    </span>
                  </Space>

                  <Space size={4}>
                    <CheckCircleOutlined style={{ color: '#10b981' }} />
                    <span>置信度 {Math.round(item.confidence * 100)}%</span>
                  </Space>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Edit Memory Modal */}
      <Modal
        title="编辑长期记忆"
        open={editModalVisible}
        onOk={handleSaveEdit}
        onCancel={() => setEditModalVisible(false)}
        okText="保存更改"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="memory_type"
            label="记忆分类"
            rules={[{ required: true, message: '请选择记忆分类' }]}
          >
            <Select
              options={Object.entries(TYPE_CONFIG).map(([k, v]) => ({
                value: k,
                label: `${v.icon} ${v.label}`,
              }))}
            />
          </Form.Item>

          <Form.Item
            name="content"
            label="记忆事实内容"
            rules={[{ required: true, min: 2, message: '记忆内容至少需2个字符' }]}
          >
            <Input.TextArea
              rows={4}
              placeholder="请输入精准凝练的长期事实、项目背景或用户偏好描述..."
            />
          </Form.Item>
        </Form>
      </Modal>

      {/* Create Memory Modal */}
      <Modal
        title="手动录入长期记忆"
        open={createModalVisible}
        onOk={handleCreateNew}
        onCancel={() => setCreateModalVisible(false)}
        okText="添加"
        cancelText="取消"
        destroyOnClose
      >
        <Form form={createForm} layout="vertical" initialValues={{ memory_type: 'USER_PROFILE' }}>
          <Form.Item
            name="memory_type"
            label="记忆分类"
            rules={[{ required: true, message: '请选择记忆分类' }]}
          >
            <Select
              options={Object.entries(TYPE_CONFIG).map(([k, v]) => ({
                value: k,
                label: `${v.icon} ${v.label}`,
              }))}
            />
          </Form.Item>

          <Form.Item
            name="content"
            label="记忆事实内容"
            rules={[{ required: true, min: 2, message: '记忆内容至少需2个字符' }]}
          >
            <Input.TextArea
              rows={4}
              placeholder="例如：用户偏好使用 FastAPI 进行全异步微服务设计，并强制要求所有响应包含 Trace-ID..."
            />
          </Form.Item>
        </Form>
      </Modal>
    </Drawer>
  );
};
