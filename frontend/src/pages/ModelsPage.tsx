import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Col,
  Divider,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudServerOutlined,
  DeleteOutlined,
  EditOutlined,
  ThunderboltOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  StarFilled,
  StarOutlined,
  LockOutlined,
} from '@ant-design/icons';
import {
  createModelConfig,
  createProvider,
  deleteModelConfig,
  deleteProvider,
  getProviders,
  probeOllama,
  setDefaultModel,
  testModelConnection,
  updateModelConfig,
  updateProvider,
} from '../api/models';
import {
  ModelCapabilities,
  ModelConfig,
  ModelProvider,
  ModelTestResponse,
  OllamaModelItem,
} from '../types';
import { DESIGN_TOKENS } from '../theme';

const { Title, Text, Paragraph } = Typography;
const { Option } = Select;

export const ModelsPage: React.FC = () => {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [loading, setLoading] = useState<boolean>(false);

  // Modals state
  const [isProviderModalOpen, setIsProviderModalOpen] = useState<boolean>(false);
  const [editingProvider, setEditingProvider] = useState<ModelProvider | null>(null);
  const [providerForm] = Form.useForm();

  const [isModelModalOpen, setIsModelModalOpen] = useState<boolean>(false);
  const [currentProviderId, setCurrentProviderId] = useState<string>('');
  const [editingModel, setEditingModel] = useState<ModelConfig | null>(null);
  const [modelForm] = Form.useForm();

  // Ollama Probe Modal
  const [isOllamaModalOpen, setIsOllamaModalOpen] = useState<boolean>(false);
  const [ollamaBaseUrl, setOllamaBaseUrl] = useState<string>('http://localhost:11434');
  const [probingOllama, setProbingOllama] = useState<boolean>(false);
  const [discoveredOllamaModels, setDiscoveredOllamaModels] = useState<OllamaModelItem[]>([]);

  // Testing connectivity state per modelId
  const [testingModelId, setTestingModelId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ModelTestResponse>>({});

  const loadData = async () => {
    setLoading(true);
    try {
      const data = await getProviders();
      setProviders(data);
    } catch (err: any) {
      message.error(`加载模型列表失败: ${err.message}`);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
  }, []);

  // Provider Handlers
  const handleOpenCreateProvider = () => {
    setEditingProvider(null);
    providerForm.resetFields();
    providerForm.setFieldsValue({
      provider_type: 'deepseek',
      name: 'DeepSeek 官方',
      base_url: 'https://api.deepseek.com',
      enabled: true,
      initial_model_name: 'deepseek-chat',
      initial_display_name: 'DeepSeek-V3',
      supports_tool_calling: true,
      supports_streaming: true,
      context_window: 65536,
    });
    setIsProviderModalOpen(true);
  };

  const handleOpenEditProvider = (provider: ModelProvider) => {
    setEditingProvider(provider);
    providerForm.resetFields();
    providerForm.setFieldsValue({
      name: provider.name,
      provider_type: provider.provider_type,
      base_url: provider.base_url,
      api_key: '',
      enabled: provider.enabled,
    });
    setIsProviderModalOpen(true);
  };

  const handleProviderTypeChange = (type: string) => {
    if (editingProvider) return;
    if (type === 'deepseek') {
      providerForm.setFieldsValue({
        name: 'DeepSeek 官方',
        base_url: 'https://api.deepseek.com',
        initial_model_name: 'deepseek-chat',
        initial_display_name: 'DeepSeek-V3',
        context_window: 65536,
      });
    } else if (type === 'ollama') {
      providerForm.setFieldsValue({
        name: 'Ollama 本地服务',
        base_url: 'http://localhost:11434',
        initial_model_name: 'llama3:8b',
        initial_display_name: 'Llama 3 8B (本地)',
        context_window: 16384,
      });
    } else if (type === 'vllm') {
      providerForm.setFieldsValue({
        name: 'vLLM 推理服务',
        base_url: 'http://localhost:8000/v1',
        initial_model_name: 'Qwen/Qwen2.5-7B',
        initial_display_name: '通义千问 7B (vLLM)',
        context_window: 32768,
      });
    } else if (type === 'openai_compatible') {
      providerForm.setFieldsValue({
        name: 'OpenAI 兼容服务',
        base_url: 'https://api.siliconflow.cn/v1',
        initial_model_name: 'deepseek-ai/DeepSeek-V3',
        initial_display_name: 'DeepSeek-V3 (托管)',
        context_window: 65536,
      });
    } else if (type === 'moonshot') {
      providerForm.setFieldsValue({
        name: 'Moonshot 月之暗面',
        base_url: 'https://api.moonshot.cn/v1',
        initial_model_name: 'moonshot-v1-32k',
        initial_display_name: 'Moonshot v1 32K',
        context_window: 32768,
      });
    } else if (type === 'dashscope') {
      providerForm.setFieldsValue({
        name: '阿里百炼 DashScope',
        base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        initial_model_name: 'qwen-max',
        initial_display_name: '通义千问 Max',
        context_window: 32768,
      });
    }
  };

  const handleSaveProvider = async () => {
    try {
      const values = await providerForm.validateFields();
      if (editingProvider) {
        await updateProvider(editingProvider.id, {
          name: values.name,
          provider_type: values.provider_type,
          base_url: values.base_url,
          api_key: values.api_key ? values.api_key : undefined,
          enabled: values.enabled,
        });
        message.success('服务商配置已更新');
      } else {
        const initialModels = values.initial_model_name
          ? [
              {
                model_name: values.initial_model_name,
                display_name: values.initial_display_name || values.initial_model_name,
                is_default: providers.length === 0,
                capabilities: {
                  supports_streaming: values.supports_streaming ?? true,
                  supports_tool_calling: values.supports_tool_calling ?? true,
                  supports_vision: false,
                  context_window: values.context_window || 32768,
                },
              },
            ]
          : [];

        await createProvider({
          name: values.name,
          provider_type: values.provider_type,
          base_url: values.base_url,
          api_key: values.api_key,
          enabled: values.enabled,
          models: initialModels,
        });
        message.success('已成功添加模型服务商');
      }
      setIsProviderModalOpen(false);
      loadData();
    } catch (err: any) {
      message.error(`保存失败: ${err.message}`);
    }
  };

  const handleDeleteProvider = async (providerId: string) => {
    try {
      await deleteProvider(providerId);
      message.success('服务商及下属模型已安全删除');
      loadData();
    } catch (err: any) {
      message.error(`删除失败: ${err.message}`);
    }
  };

  // Model Handlers
  const handleOpenCreateModel = (providerId: string) => {
    setCurrentProviderId(providerId);
    setEditingModel(null);
    modelForm.resetFields();
    modelForm.setFieldsValue({
      model_type: 'chat',
      supports_streaming: true,
      supports_tool_calling: true,
      supports_vision: false,
      context_window: 32768,
      enabled: true,
      is_default: false,
    });
    setIsModelModalOpen(true);
  };

  const handleOpenEditModel = (model: ModelConfig) => {
    setCurrentProviderId(model.provider_id);
    setEditingModel(model);
    modelForm.resetFields();
    modelForm.setFieldsValue({
      model_name: model.model_name,
      display_name: model.display_name,
      model_type: model.model_type,
      supports_streaming: model.capabilities.supports_streaming,
      supports_tool_calling: model.capabilities.supports_tool_calling,
      context_window: model.capabilities.context_window,
      enabled: model.enabled,
      is_default: model.is_default,
    });
    setIsModelModalOpen(true);
  };

  const handleSaveModel = async () => {
    try {
      const values = await modelForm.validateFields();
      const capabilities: ModelCapabilities = {
        supports_streaming: values.supports_streaming ?? true,
        supports_tool_calling: values.supports_tool_calling ?? true,
        supports_vision: false,
        context_window: values.context_window || 32768,
      };

      if (editingModel) {
        await updateModelConfig(editingModel.id, {
          display_name: values.display_name,
          model_type: values.model_type,
          enabled: values.enabled,
          is_default: values.is_default,
          capabilities,
        });
        message.success('模型配置已更新');
      } else {
        await createModelConfig(currentProviderId, {
          model_name: values.model_name,
          display_name: values.display_name,
          model_type: values.model_type,
          enabled: values.enabled,
          is_default: values.is_default,
          capabilities,
        });
        message.success('已成功添加新模型');
      }
      setIsModelModalOpen(false);
      loadData();
    } catch (err: any) {
      message.error(`保存失败: ${err.message}`);
    }
  };

  const handleDeleteModel = async (modelId: string) => {
    try {
      await deleteModelConfig(modelId);
      message.success('模型配置已删除');
      loadData();
    } catch (err: any) {
      message.error(`删除失败: ${err.message}`);
    }
  };

  const handleSetDefault = async (modelId: string) => {
    try {
      await setDefaultModel(modelId);
      message.success('已更新为全局默认问答模型');
      loadData();
    } catch (err: any) {
      message.error(`设置默认失败: ${err.message}`);
    }
  };

  // Test Connectivity
  const handleTestConnection = async (model: ModelConfig) => {
    setTestingModelId(model.id);
    try {
      const res = await testModelConnection({ model_id: model.id });
      setTestResults((prev) => ({ ...prev, [model.id]: res }));
      if (res.success) {
        message.success(`连接测试成功！耗时: ${res.latency_ms}ms`);
      } else {
        message.error(`连接测试失败: ${res.error}`);
      }
    } catch (err: any) {
      message.error(`请求失败: ${err.message}`);
    } finally {
      setTestingModelId(null);
    }
  };

  // Ollama Probing
  const handleProbeOllama = async () => {
    setProbingOllama(true);
    try {
      const res = await probeOllama(ollamaBaseUrl);
      if (res.success) {
        setDiscoveredOllamaModels(res.models);
        message.success(`成功探测到 ${res.models.length} 个 Ollama 本地模型！`);
      } else {
        message.error(res.error || '探测失败，请确认服务已启动');
      }
    } catch (err: any) {
      message.error(`探测异常: ${err.message}`);
    } finally {
      setProbingOllama(false);
    }
  };

  const handleBatchAddOllama = async () => {
    if (discoveredOllamaModels.length === 0) return;
    try {
      const modelsPayload = discoveredOllamaModels.map((m) => ({
        model_name: m.name,
        display_name: `${m.name} (${m.parameter_size || '本地'})`,
        is_default: false,
        capabilities: {
          supports_streaming: true,
          supports_tool_calling: m.name.toLowerCase().includes('qwen') || m.name.toLowerCase().includes('llama3'),
          supports_vision: false,
          context_window: 16384,
        },
      }));

      await createProvider({
        name: 'Ollama 本地大模型',
        provider_type: 'ollama',
        base_url: ollamaBaseUrl,
        enabled: true,
        models: modelsPayload,
      });

      message.success(`已一键导入 Ollama 服务商及 ${modelsPayload.length} 个模型！`);
      setIsOllamaModalOpen(false);
      loadData();
    } catch (err: any) {
      message.error(`导入失败: ${err.message}`);
    }
  };

  const getProviderTypeColor = (type: string) => {
    switch (type) {
      case 'deepseek':
        return 'blue';
      case 'ollama':
        return 'orange';
      case 'vllm':
        return 'cyan';
      case 'moonshot':
        return 'purple';
      case 'dashscope':
        return 'gold';
      default:
        return 'geekblue';
    }
  };

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
              <ApiOutlined />
            </div>
            <div>
              <Title level={4} style={{ margin: 0, color: DESIGN_TOKENS.brandDark, fontWeight: 700 }}>
                模型管理中心 (Model Management Center)
              </Title>
              <Text type="secondary" style={{ fontSize: 13 }}>
                支持开箱即用配置商业大模型 API（DeepSeek、OpenAI、Moonshot）与本地私有化推理引擎（Ollama、vLLM）。所有密钥均采用 AES-256 加密安全落库。
              </Text>
            </div>
          </Space>

          <Space size={10} wrap>
            <Button
              icon={<CloudServerOutlined style={{ color: '#ea580c' }} />}
              onClick={() => {
                setIsOllamaModalOpen(true);
                setDiscoveredOllamaModels([]);
              }}
              style={{ borderRadius: 8 }}
            >
              探测 Ollama 本地模型
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={handleOpenCreateProvider}
              style={{ borderRadius: 8, fontWeight: 500 }}
            >
              添加模型服务商
            </Button>
            <Button
              icon={<ReloadOutlined />}
              onClick={loadData}
              loading={loading}
              title="刷新服务商及模型"
              style={{ borderRadius: 8 }}
            />
          </Space>
        </div>
      </div>

      {/* Main Providers and Models List */}
      <Spin spinning={loading}>
        {providers.length === 0 ? (
          <div
            style={{
              backgroundColor: '#ffffff',
              borderRadius: DESIGN_TOKENS.cardRadius,
              border: `1px solid ${DESIGN_TOKENS.borderRegular}`,
              padding: '60px 24px',
              textAlign: 'center',
            }}
          >
            <ApiOutlined style={{ fontSize: 48, color: '#94a3b8', marginBottom: 16 }} />
            <Title level={4} style={{ color: DESIGN_TOKENS.brandDark, marginBottom: 8 }}>
              暂无配置的模型服务商
            </Title>
            <Paragraph type="secondary" style={{ maxWidth: 460, margin: '0 auto 20px', fontSize: 13.5 }}>
              点击上方「添加模型服务商」即可快速配置 DeepSeek 官方 API、OpenAI 兼容接口或 Ollama 本地私有化模型。
            </Paragraph>
            <Button type="primary" icon={<PlusOutlined />} onClick={handleOpenCreateProvider} style={{ height: 38 }}>
              立即配置首个大模型服务商
            </Button>
          </div>
        ) : (
          <Space direction="vertical" size={20} style={{ width: '100%' }}>
            {providers.map((prov) => (
              <div
                key={prov.id}
                style={{
                  backgroundColor: '#ffffff',
                  borderRadius: DESIGN_TOKENS.cardRadius,
                  border: `1px solid ${DESIGN_TOKENS.borderRegular}`,
                  boxShadow: '0 1px 4px rgba(0,0,0,0.03)',
                  overflow: 'hidden',
                }}
              >
                {/* Provider Card Header */}
                <div
                  style={{
                    padding: '16px 20px',
                    backgroundColor: '#fafbfc',
                    borderBottom: `1px solid ${DESIGN_TOKENS.borderRegular}`,
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    flexWrap: 'wrap',
                    gap: 12,
                  }}
                >
                  <Space size={10} align="center" wrap>
                    <span style={{ fontSize: 15, fontWeight: 700, color: DESIGN_TOKENS.brandDark }}>
                      {prov.name}
                    </span>
                    <Tag color={getProviderTypeColor(prov.provider_type)} style={{ borderRadius: 4, fontWeight: 600 }}>
                      {prov.provider_type.toUpperCase()}
                    </Tag>
                    {prov.has_api_key ? (
                      <Tag
                        icon={<SafetyCertificateOutlined style={{ color: '#10b981' }} />}
                        color="success"
                        style={{ borderRadius: 4, fontWeight: 500 }}
                      >
                        密钥加密存储: {prov.masked_api_key}
                      </Tag>
                    ) : (
                      <Tag color="default" style={{ borderRadius: 4 }}>
                        免密访问 (本地/无需 Key)
                      </Tag>
                    )}
                    {!prov.enabled ? (
                      <Tag color="error" style={{ borderRadius: 4 }}>已停用</Tag>
                    ) : (
                      <Tag color="green" style={{ borderRadius: 4 }}>服务在线</Tag>
                    )}
                  </Space>

                  <Space size={10} align="center">
                    <Tooltip title="API Base URL 端点">
                      <span
                        style={{
                          fontSize: 12,
                          fontFamily: 'monospace',
                          color: DESIGN_TOKENS.textMuted,
                          backgroundColor: '#f1f5f9',
                          padding: '2px 8px',
                          borderRadius: 4,
                          border: '1px solid #e2e8f0',
                        }}
                      >
                        {prov.base_url}
                      </span>
                    </Tooltip>
                    <Button
                      size="small"
                      type="link"
                      icon={<PlusOutlined />}
                      onClick={() => handleOpenCreateModel(prov.id)}
                      style={{ fontSize: 12 }}
                    >
                      添加模型
                    </Button>
                    <Button
                      size="small"
                      type="text"
                      icon={<EditOutlined />}
                      onClick={() => handleOpenEditProvider(prov)}
                      style={{ fontSize: 12 }}
                    >
                      编辑服务商
                    </Button>
                    <Popconfirm
                      title="确定删除此服务商及其所有下属模型吗？"
                      description="删除后该服务商下的所有模型将无法继续参与问答或 Agent 推理。"
                      onConfirm={() => handleDeleteProvider(prov.id)}
                      okText="确认删除"
                      cancelText="取消"
                      okButtonProps={{ danger: true }}
                    >
                      <Button size="small" type="text" danger icon={<DeleteOutlined />} style={{ fontSize: 12 }}>
                        删除
                      </Button>
                    </Popconfirm>
                  </Space>
                </div>

                {/* Nested Models Table */}
                <div style={{ padding: '4px 16px 12px' }}>
                  <Table
                    dataSource={prov.models}
                    rowKey="id"
                    pagination={false}
                    size="middle"
                    columns={[
                      {
                        title: '展示名称 / 模型标识',
                        key: 'name',
                        width: 240,
                        render: (_, m: ModelConfig) => (
                          <Space direction="vertical" size={2}>
                            <Space size={8}>
                              <Text strong style={{ fontSize: 13.5, color: DESIGN_TOKENS.brandDark }}>
                                {m.display_name}
                              </Text>
                              {m.is_default && (
                                <Tag color="gold" icon={<StarFilled />} style={{ fontWeight: 600, borderRadius: 4, fontSize: 11 }}>
                                  全局默认
                                </Tag>
                              )}
                            </Space>
                            <Text type="secondary" style={{ fontSize: 11.5, fontFamily: 'monospace' }}>
                              {m.model_name}
                            </Text>
                          </Space>
                        ),
                      },
                      {
                        title: '推理能力规格 (Capabilities)',
                        key: 'capabilities',
                        render: (_, m: ModelConfig) => (
                          <Space size={6} wrap>
                            {m.capabilities.supports_streaming && (
                              <Tag color="processing" icon={<ThunderboltOutlined />} style={{ borderRadius: 4 }}>
                                流式输出
                              </Tag>
                            )}
                            {m.capabilities.supports_tool_calling ? (
                              <Tag color="purple" style={{ borderRadius: 4, fontWeight: 500 }}>
                                🧠 原生工具调用 (Tool Calling)
                              </Tag>
                            ) : (
                              <Tag color="default" style={{ borderRadius: 4 }}>
                                仅普通问答 (无 Tool Calling)
                              </Tag>
                            )}
                            <Tag color="geekblue" style={{ borderRadius: 4 }}>
                              {Math.round(m.capabilities.context_window / 1024)}K 上下文
                            </Tag>
                          </Space>
                        ),
                      },
                      {
                        title: '健康连接检测 (Connectivity)',
                        key: 'test',
                        width: 220,
                        render: (_, m: ModelConfig) => {
                          const result = testResults[m.id];
                          const isTesting = testingModelId === m.id;
                          return (
                            <Space size={8} align="center">
                              <Button
                                size="small"
                                loading={isTesting}
                                onClick={() => handleTestConnection(m)}
                                style={{ borderRadius: 6, fontSize: 12 }}
                              >
                                测试连接
                              </Button>
                              {result && (
                                result.success ? (
                                  <Tooltip title={`测试成功，耗时 ${result.latency_ms}ms。回复摘要: ${result.reply_snippet || '正常'}`}>
                                    <Tag color="success" icon={<CheckCircleOutlined />} style={{ borderRadius: 4, cursor: 'pointer' }}>
                                      正常 ({result.latency_ms}ms)
                                    </Tag>
                                  </Tooltip>
                                ) : (
                                  <Tooltip title={result.error || '连接超时或鉴权失败'}>
                                    <Tag color="error" icon={<CloseCircleOutlined />} style={{ borderRadius: 4, cursor: 'pointer' }}>
                                      连接失败
                                    </Tag>
                                  </Tooltip>
                                )
                              )}
                            </Space>
                          );
                        },
                      },
                      {
                        title: '状态 / 设为默认',
                        key: 'default_switch',
                        width: 140,
                        render: (_, m: ModelConfig) => (
                          <div>
                            {m.is_default ? (
                              <span style={{ color: '#d97706', fontSize: 12, fontWeight: 600 }}>
                                <StarFilled style={{ marginRight: 4 }} /> 当前默认
                              </span>
                            ) : (
                              <Button
                                size="small"
                                type="link"
                                icon={<StarOutlined />}
                                onClick={() => handleSetDefault(m.id)}
                                style={{ padding: 0, fontSize: 12 }}
                              >
                                设为默认
                              </Button>
                            )}
                          </div>
                        ),
                      },
                      {
                        title: '操作',
                        key: 'action',
                        width: 100,
                        render: (_, m: ModelConfig) => (
                          <Space size={4}>
                            <Tooltip title="编辑模型参数与能力">
                              <Button
                                size="small"
                                type="text"
                                icon={<EditOutlined style={{ color: DESIGN_TOKENS.interactiveBlue }} />}
                                onClick={() => handleOpenEditModel(m)}
                              />
                            </Tooltip>
                            <Popconfirm
                              title="确定删除此模型配置吗？"
                              description={`删除后模型【${m.display_name}】将不再出现在聊天或 Agent 下拉列表中。`}
                              onConfirm={() => handleDeleteModel(m.id)}
                              okText="删除"
                              cancelText="取消"
                              okButtonProps={{ danger: true }}
                            >
                              <Tooltip title="删除模型">
                                <Button size="small" type="text" danger icon={<DeleteOutlined />} />
                              </Tooltip>
                            </Popconfirm>
                          </Space>
                        ),
                      },
                    ]}
                  />
                </div>
              </div>
            ))}
          </Space>
        )}
      </Spin>

      {/* Provider Create/Edit Modal */}
      <Modal
        title={
          <Space>
            <LockOutlined style={{ color: DESIGN_TOKENS.interactiveBlue }} />
            <span>{editingProvider ? '编辑模型服务商' : '添加大模型服务商'}</span>
          </Space>
        }
        open={isProviderModalOpen}
        onOk={handleSaveProvider}
        onCancel={() => setIsProviderModalOpen(false)}
        width={620}
        destroyOnClose
      >
        <Alert
          type="info"
          showIcon
          icon={<SafetyCertificateOutlined style={{ color: '#10b981' }} />}
          message="高标准密钥安全防护"
          description="API Key 将自动通过 AES-256 加密落库，仅在后端进行模型推理调用时实时解密，绝不暴露至前端或网络日志。"
          style={{ marginBottom: 16, borderRadius: 8 }}
        />

        <Form form={providerForm} layout="vertical">
          <Row gutter={16}>
            <Col span={14}>
              <Form.Item
                name="name"
                label="服务商名称"
                rules={[{ required: true, message: '请输入服务商名称' }]}
              >
                <Input placeholder="例如: DeepSeek 官方 / 硅基流动 / Ollama 本地" />
              </Form.Item>
            </Col>
            <Col span={10}>
              <Form.Item
                name="provider_type"
                label="服务商类型"
                rules={[{ required: true }]}
              >
                <Select onChange={handleProviderTypeChange}>
                  <Option value="deepseek">DeepSeek 官方</Option>
                  <Option value="openai_compatible">OpenAI 兼容协议</Option>
                  <Option value="ollama">Ollama 本地模型</Option>
                  <Option value="vllm">vLLM 本地推理引擎</Option>
                  <Option value="moonshot">Moonshot 月之暗面</Option>
                  <Option value="dashscope">阿里百炼 DashScope</Option>
                </Select>
              </Form.Item>
            </Col>
          </Row>

          <Form.Item
            name="base_url"
            label="API Base URL"
            rules={[{ required: true, message: '请输入 Base URL' }]}
            extra="系统会自动适配补齐标准 /chat/completions 路由路径"
          >
            <Input placeholder="例如: https://api.deepseek.com 或 http://localhost:11434" />
          </Form.Item>

          <Form.Item
            name="api_key"
            label="API Key (密钥)"
            extra={editingProvider ? '留空则保持当前加密密钥不变；如需清空请输入单个空格' : '若为免密本地服务 (如 Ollama / 本地 vLLM) 可留空'}
          >
            <Input.Password placeholder={editingProvider?.has_api_key ? '已配置安全密钥 (输入新密钥可覆盖)' : 'sk-****************'} />
          </Form.Item>

          <Form.Item name="enabled" valuePropName="checked">
            <Checkbox>启用此服务商</Checkbox>
          </Form.Item>

          {!editingProvider && (
            <>
              <Divider orientation="left" style={{ fontSize: 13, color: DESIGN_TOKENS.textMuted }}>
                附带初始模型 (可选)
              </Divider>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="initial_model_name" label="模型调用名 (Model Name)">
                    <Input placeholder="例如: deepseek-chat" />
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="initial_display_name" label="前端展示名称">
                    <Input placeholder="例如: DeepSeek-V3" />
                  </Form.Item>
                </Col>
              </Row>
              <Row gutter={16}>
                <Col span={12}>
                  <Form.Item name="supports_tool_calling" valuePropName="checked">
                    <Checkbox defaultChecked>支持原生工具调用 (Tool Calling)</Checkbox>
                  </Form.Item>
                </Col>
                <Col span={12}>
                  <Form.Item name="context_window" label="上下文大小 (Tokens)">
                    <InputNumber style={{ width: '100%' }} min={1024} max={1000000} step={1024} />
                  </Form.Item>
                </Col>
              </Row>
            </>
          )}
        </Form>
      </Modal>

      {/* Model Create/Edit Modal */}
      <Modal
        title={editingModel ? '编辑模型参数与能力' : '添加大模型配置'}
        open={isModelModalOpen}
        onOk={handleSaveModel}
        onCancel={() => setIsModelModalOpen(false)}
        width={580}
        destroyOnClose
      >
        <Form form={modelForm} layout="vertical" style={{ marginTop: 8 }}>
          <Form.Item
            name="model_name"
            label="模型调用名 (Model Name)"
            rules={[{ required: true, message: '请输入模型调用标识' }]}
            extra="发送给 API 的真实模型参数，如 deepseek-chat、gpt-4o、qwen2.5:7b"
          >
            <Input placeholder="例如: deepseek-chat" disabled={!!editingModel} />
          </Form.Item>

          <Form.Item
            name="display_name"
            label="展示名称 (Display Name)"
            rules={[{ required: true, message: '请输入界面展示名称' }]}
          >
            <Input placeholder="例如: DeepSeek-V3 (官方)" />
          </Form.Item>

          <Form.Item name="context_window" label="最大上下文窗口 (Tokens)">
            <InputNumber style={{ width: '100%' }} min={1024} max={2000000} step={1024} />
          </Form.Item>

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="supports_streaming" valuePropName="checked">
                <Checkbox>支持 SSE 流式生成</Checkbox>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="supports_tool_calling" valuePropName="checked">
                <Checkbox>支持工具调用 (Tool Calling)</Checkbox>
              </Form.Item>
            </Col>
          </Row>

          <Alert
            type="info"
            showIcon
            message="工具调用说明"
            description="开启「支持工具调用 (Tool Calling)」后，该模型将支持「🧠 深度分析（LangGraph Agent）」模式自主决策调用知识库与会话临时附件检索工具。"
            style={{ marginBottom: 16, borderRadius: 8, fontSize: 12.5 }}
          />

          <Row gutter={16}>
            <Col span={12}>
              <Form.Item name="enabled" valuePropName="checked">
                <Checkbox>启用该模型</Checkbox>
              </Form.Item>
            </Col>
            <Col span={12}>
              <Form.Item name="is_default" valuePropName="checked">
                <Checkbox>设为系统全局默认模型</Checkbox>
              </Form.Item>
            </Col>
          </Row>
        </Form>
      </Modal>

      {/* Ollama Probe Modal */}
      <Modal
        title="🦙 自动探测 Ollama 本地模型"
        open={isOllamaModalOpen}
        onCancel={() => setIsOllamaModalOpen(false)}
        footer={[
          <Button key="close" onClick={() => setIsOllamaModalOpen(false)}>
            取消
          </Button>,
          <Button
            key="import"
            type="primary"
            disabled={discoveredOllamaModels.length === 0}
            onClick={handleBatchAddOllama}
          >
            一键导入所探测的模型 ({discoveredOllamaModels.length})
          </Button>,
        ]}
        width={700}
      >
        <Space direction="vertical" size={16} style={{ width: '100%', marginTop: 8 }}>
          <Alert
            type="info"
            showIcon
            message="快速接入本地私有化大模型"
            description="确认您的本地 Ollama 服务已在运行后，输入服务端口点击探测，即可自动读取本地已下载的模型列表并批量配置至系统。"
            style={{ borderRadius: 8 }}
          />
          <Space.Compact style={{ width: '100%' }}>
            <Input
              value={ollamaBaseUrl}
              onChange={(e) => setOllamaBaseUrl(e.target.value)}
              placeholder="http://localhost:11434"
            />
            <Button type="primary" loading={probingOllama} onClick={handleProbeOllama}>
              立即探测
            </Button>
          </Space.Compact>

          {discoveredOllamaModels.length > 0 && (
            <Table
              dataSource={discoveredOllamaModels}
              rowKey="name"
              pagination={false}
              size="small"
              columns={[
                {
                  title: '模型名称',
                  dataIndex: 'name',
                  key: 'name',
                  render: (text) => <Text strong>{text}</Text>,
                },
                {
                  title: '参数规格',
                  dataIndex: 'parameter_size',
                  key: 'parameter_size',
                  render: (text, item) => (
                    <Space size={4}>
                      {text && <Tag color="blue">{text}</Tag>}
                      {item.family && <Tag>{item.family}</Tag>}
                      {item.quantization_level && <Tag color="orange">{item.quantization_level}</Tag>}
                    </Space>
                  ),
                },
                {
                  title: '体积大小',
                  dataIndex: 'size',
                  key: 'size',
                  render: (bytes?: number) =>
                    bytes ? `${(bytes / 1024 / 1024 / 1024).toFixed(1)} GB` : '-',
                },
              ]}
            />
          )}
        </Space>
      </Modal>
    </div>
  );
};

export default ModelsPage;
