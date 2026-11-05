import React, { useState } from 'react';
import { Card, Form, Input, Button, Tabs, Alert, Space, Typography, Tag, message } from 'antd';
import {
  UserOutlined,
  LockOutlined,
  MailOutlined,
  SafetyCertificateOutlined,
  InfoCircleOutlined,
} from '@ant-design/icons';
import { useAuth } from '../context/AuthContext';
import { DESIGN_TOKENS } from '../theme';
import { LoginCredentials, RegisterData } from '../types';

const { Title, Text } = Typography;

export const LoginPage: React.FC = () => {
  const { login, register, allowRegistration, systemVersion } = useAuth();
  const [activeTab, setActiveTab] = useState<'login' | 'register'>('login');
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [loginForm] = Form.useForm();
  const [registerForm] = Form.useForm();

  const handleLoginSubmit = async (values: LoginCredentials) => {
    setSubmitting(true);
    setErrorMessage(null);
    try {
      await login(values);
      message.success('登录成功，欢迎使用 AgentStudio-RAG！');
    } catch (err: any) {
      // Intentionally generic security error message to avoid enumeration
      setErrorMessage(err.message || '用户名或密码错误，请检查输入。');
    } finally {
      setSubmitting(false);
    }
  };

  const handleRegisterSubmit = async (values: any) => {
    if (values.password !== values.confirmPassword) {
      setErrorMessage('两次输入的密码不一致，请重新确认。');
      return;
    }
    setSubmitting(true);
    setErrorMessage(null);
    try {
      const regData: RegisterData = {
        username: values.username,
        password: values.password,
        email: values.email || undefined,
      };
      await register(regData);
      message.success('账号注册成功！现可直接登录。');
      registerForm.resetFields();
      loginForm.setFieldsValue({ username: values.username });
      setActiveTab('login');
    } catch (err: any) {
      setErrorMessage(err.message || '注册失败，请稍后重试。');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      style={{
        minHeight: '100vh',
        backgroundColor: DESIGN_TOKENS.pageBg,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        alignItems: 'center',
        padding: '24px 16px',
      }}
    >
      {/* Brand Header */}
      <div style={{ textAlign: 'center', marginBottom: 28 }}>
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: 48,
            height: 48,
            borderRadius: DESIGN_TOKENS.cardRadius,
            backgroundColor: DESIGN_TOKENS.interactiveBlue,
            color: '#ffffff',
            fontSize: 24,
            fontWeight: 700,
            boxShadow: '0 4px 16px rgba(37, 99, 235, 0.3)',
            marginBottom: 12,
          }}
        >
          ⚡
        </div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
          <Title
            level={3}
            style={{
              margin: 0,
              fontWeight: 700,
              color: DESIGN_TOKENS.brandDark,
              letterSpacing: -0.5,
            }}
          >
            AgentStudio-RAG
          </Title>
          <Tag color="blue" style={{ borderRadius: DESIGN_TOKENS.pillRadius, fontWeight: 600 }}>
            v{systemVersion}
          </Tag>
        </div>
        <Text style={{ color: DESIGN_TOKENS.textMuted, fontSize: 13 }}>
          企业级智能知识助手与多租户协作工作台
        </Text>
      </div>

      {/* Main Form Card */}
      <Card
        style={{
          width: '100%',
          maxWidth: 420,
          borderRadius: DESIGN_TOKENS.cardRadius,
          borderColor: DESIGN_TOKENS.borderRegular,
          boxShadow: '0 8px 30px rgba(15, 23, 42, 0.06)',
          backgroundColor: DESIGN_TOKENS.cardBg,
        }}
        bodyStyle={{ padding: '24px 28px' }}
      >
        <Tabs
          activeKey={activeTab}
          onChange={(key) => {
            setActiveTab(key as 'login' | 'register');
            setErrorMessage(null);
          }}
          centered
          items={[
            {
              key: 'login',
              label: (
                <span style={{ fontSize: 15, fontWeight: 600, padding: '0 8px' }}>
                  账号登录
                </span>
              ),
            },
            ...(allowRegistration
              ? [
                  {
                    key: 'register',
                    label: (
                      <span style={{ fontSize: 15, fontWeight: 600, padding: '0 8px' }}>
                        注册新账号
                      </span>
                    ),
                  },
                ]
              : []),
          ]}
        />

        {errorMessage && (
          <Alert
            message={errorMessage}
            type="error"
            showIcon
            closable
            onClose={() => setErrorMessage(null)}
            style={{ marginBottom: 20, borderRadius: DESIGN_TOKENS.controlRadius, fontSize: 13 }}
          />
        )}

        {activeTab === 'login' && (
          <Form
            form={loginForm}
            layout="vertical"
            onFinish={handleLoginSubmit}
            requiredMark={false}
          >
            <Form.Item
              name="username"
              rules={[{ required: true, message: '请输入用户名或邮箱' }]}
            >
              <Input
                prefix={<UserOutlined style={{ color: DESIGN_TOKENS.textDisabled }} />}
                placeholder="用户名或电子邮箱"
                size="large"
                autoComplete="username"
                style={{ borderRadius: DESIGN_TOKENS.controlRadius }}
              />
            </Form.Item>

            <Form.Item
              name="password"
              rules={[{ required: true, message: '请输入登录密码' }]}
            >
              <Input.Password
                prefix={<LockOutlined style={{ color: DESIGN_TOKENS.textDisabled }} />}
                placeholder="登录密码"
                size="large"
                autoComplete="current-password"
                style={{ borderRadius: DESIGN_TOKENS.controlRadius }}
              />
            </Form.Item>

            <Form.Item style={{ marginBottom: 12 }}>
              <Button
                type="primary"
                htmlType="submit"
                size="large"
                loading={submitting}
                block
                style={{
                  borderRadius: DESIGN_TOKENS.controlRadius,
                  fontWeight: 600,
                  height: 42,
                  boxShadow: '0 2px 6px rgba(37, 99, 235, 0.2)',
                }}
              >
                立即登录
              </Button>
            </Form.Item>

            {!allowRegistration && (
              <div
                style={{
                  marginTop: 16,
                  padding: '8px 12px',
                  backgroundColor: DESIGN_TOKENS.surfaceBg,
                  borderRadius: DESIGN_TOKENS.controlRadius,
                  display: 'flex',
                  alignItems: 'center',
                  gap: 8,
                }}
              >
                <InfoCircleOutlined style={{ color: DESIGN_TOKENS.textMuted, fontSize: 13 }} />
                <Text style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>
                  公开注册暂未开放，如需使用请向管理员申请账号。
                </Text>
              </div>
            )}
          </Form>
        )}

        {activeTab === 'register' && allowRegistration && (
          <Form
            form={registerForm}
            layout="vertical"
            onFinish={handleRegisterSubmit}
            requiredMark={false}
          >
            <Form.Item
              name="username"
              rules={[
                { required: true, message: '请输入用户名' },
                { min: 3, message: '用户名至少3位字符' },
                { max: 32, message: '用户名最多32位字符' },
                {
                  pattern: /^[a-zA-Z0-9_\-]+$/,
                  message: '用户名仅支持英文字母、数字、下划线与连字符',
                },
              ]}
            >
              <Input
                prefix={<UserOutlined style={{ color: DESIGN_TOKENS.textDisabled }} />}
                placeholder="用户名 (3-32位字母数字)"
                size="large"
                autoComplete="username"
                style={{ borderRadius: DESIGN_TOKENS.controlRadius }}
              />
            </Form.Item>

            <Form.Item
              name="email"
              rules={[
                {
                  type: 'email',
                  message: '请输入合法的电子邮箱格式',
                },
              ]}
            >
              <Input
                prefix={<MailOutlined style={{ color: DESIGN_TOKENS.textDisabled }} />}
                placeholder="电子邮箱 (选填)"
                size="large"
                autoComplete="email"
                style={{ borderRadius: DESIGN_TOKENS.controlRadius }}
              />
            </Form.Item>

            <Form.Item
              name="password"
              rules={[
                { required: true, message: '请输入密码' },
                { min: 6, message: '密码长度至少6位' },
              ]}
            >
              <Input.Password
                prefix={<LockOutlined style={{ color: DESIGN_TOKENS.textDisabled }} />}
                placeholder="设置登录密码 (至少6位)"
                size="large"
                autoComplete="new-password"
                style={{ borderRadius: DESIGN_TOKENS.controlRadius }}
              />
            </Form.Item>

            <Form.Item
              name="confirmPassword"
              rules={[{ required: true, message: '请再次输入密码以确认' }]}
            >
              <Input.Password
                prefix={<SafetyCertificateOutlined style={{ color: DESIGN_TOKENS.textDisabled }} />}
                placeholder="确认登录密码"
                size="large"
                autoComplete="new-password"
                style={{ borderRadius: DESIGN_TOKENS.controlRadius }}
              />
            </Form.Item>

            <div
              style={{
                marginBottom: 16,
                padding: '8px 12px',
                backgroundColor: DESIGN_TOKENS.surfaceBg,
                borderRadius: DESIGN_TOKENS.controlRadius,
                fontSize: 12,
                color: DESIGN_TOKENS.textMuted,
                lineHeight: 1.4,
              }}
            >
              公开注册用户默认获得标准用户权限。如需管理员权限，请联系系统管理员使用服务端 CLI 工具分配。
            </div>

            <Form.Item style={{ marginBottom: 0 }}>
              <Button
                type="primary"
                htmlType="submit"
                size="large"
                loading={submitting}
                block
                style={{
                  borderRadius: DESIGN_TOKENS.controlRadius,
                  fontWeight: 600,
                  height: 42,
                }}
              >
                完成注册
              </Button>
            </Form.Item>
          </Form>
        )}
      </Card>

      {/* Footer Info */}
      <div style={{ marginTop: 24, textAlign: 'center' }}>
        <Space size={12} split={<span style={{ color: DESIGN_TOKENS.borderRegular }}>|</span>}>
          <Text style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>服务器多租户模式</Text>
          <Text style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>安全隔离存储</Text>
          <Text style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>PBKDF2-SHA256 加密</Text>
        </Space>
      </div>
    </div>
  );
};
