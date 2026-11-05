import React, { useState } from 'react';
import {
  Dropdown,
  Avatar,
  Space,
  Tag,
  Modal,
  Form,
  Input,
  Button,
  message,
  Typography,
} from 'antd';
import type { MenuProps } from 'antd';
import {
  KeyOutlined,
  LogoutOutlined,
  DesktopOutlined,
  CrownOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons';
import { useAuth } from '../context/AuthContext';
import { DESIGN_TOKENS } from '../theme';

const { Text } = Typography;

export const HeaderUserMenu: React.FC = () => {
  const { currentUser, deploymentMode, logout, changePassword } = useAuth();
  const [changePwdOpen, setChangePwdOpen] = useState<boolean>(false);
  const [submittingPwd, setSubmittingPwd] = useState<boolean>(false);
  const [pwdForm] = Form.useForm();

  // In Local mode, display clean local tag with zero login requirement
  if (deploymentMode === 'local') {
    return (
      <Space size={8} align="center">
        <Tag
          icon={<DesktopOutlined />}
          color="default"
          style={{
            margin: 0,
            borderRadius: DESIGN_TOKENS.pillRadius,
            padding: '2px 10px',
            fontSize: 12,
            backgroundColor: DESIGN_TOKENS.surfaceBg,
            borderColor: DESIGN_TOKENS.borderRegular,
            color: DESIGN_TOKENS.textSecondary,
          }}
        >
          本地模式 (免登录)
        </Tag>
      </Space>
    );
  }

  // If no user profile loaded in server mode
  if (!currentUser) {
    return null;
  }

  const handlePasswordChangeSubmit = async (values: any) => {
    if (values.new_password !== values.confirm_new_password) {
      message.error('两次输入的新密码不一致，请重新输入。');
      return;
    }
    setSubmittingPwd(true);
    try {
      await changePassword({
        old_password: values.old_password,
        new_password: values.new_password,
      });
      message.success('密码修改成功！所有既有设备会话已失效，请重新登录。');
      setChangePwdOpen(false);
      pwdForm.resetFields();
    } catch (err: any) {
      message.error(err.message || '修改密码失败，请检查旧密码是否正确。');
    } finally {
      setSubmittingPwd(false);
    }
  };

  const handleLogout = () => {
    Modal.confirm({
      title: '确认退出登录？',
      content: '退出登录后将清除当前会话状态。',
      okText: '确认退出',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        try {
          await logout();
          message.info('已安全退出登录');
        } catch {
          // Ignore
        }
      },
    });
  };

  const menuItems: MenuProps['items'] = [
    {
      key: 'user-info',
      label: (
        <div style={{ padding: '4px 0', minWidth: 180 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <Text strong style={{ fontSize: 14, color: DESIGN_TOKENS.textMain }}>
              {currentUser.username}
            </Text>
            {currentUser.role === 'admin' ? (
              <Tag
                color="gold"
                icon={<CrownOutlined />}
                style={{ margin: 0, borderRadius: DESIGN_TOKENS.pillRadius, fontSize: 11 }}
              >
                管理员
              </Tag>
            ) : (
              <Tag
                color="blue"
                style={{ margin: 0, borderRadius: DESIGN_TOKENS.pillRadius, fontSize: 11 }}
              >
                标准用户
              </Tag>
            )}
          </div>
          {currentUser.email && (
            <div style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>
              {currentUser.email}
            </div>
          )}
          <div style={{ fontSize: 11, color: DESIGN_TOKENS.textDisabled, marginTop: 2 }}>
            ID: {currentUser.id}
          </div>
        </div>
      ),
      disabled: true,
    },
    {
      type: 'divider',
    },
    {
      key: 'change-password',
      icon: <KeyOutlined />,
      label: '修改登录密码',
      onClick: () => setChangePwdOpen(true),
    },
    {
      type: 'divider',
    },
    {
      key: 'logout',
      icon: <LogoutOutlined />,
      danger: true,
      label: '退出登录',
      onClick: handleLogout,
    },
  ];

  return (
    <>
      <Dropdown menu={{ items: menuItems }} placement="bottomRight" trigger={['click']}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            cursor: 'pointer',
            padding: '4px 8px',
            borderRadius: DESIGN_TOKENS.controlRadius,
            transition: 'background-color 0.2s',
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.backgroundColor = DESIGN_TOKENS.surfaceBg;
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.backgroundColor = 'transparent';
          }}
        >
          <Avatar
            size={28}
            style={{
              backgroundColor:
                currentUser.role === 'admin'
                  ? DESIGN_TOKENS.interactiveBlue
                  : DESIGN_TOKENS.brandPrimary,
              color: '#ffffff',
              fontWeight: 600,
              fontSize: 13,
            }}
          >
            {currentUser.username.charAt(0).toUpperCase()}
          </Avatar>
          <div style={{ display: 'flex', flexDirection: 'column', textAlign: 'left' }}>
            <span
              style={{
                fontSize: 13,
                fontWeight: 600,
                color: DESIGN_TOKENS.textMain,
                lineHeight: 1.2,
              }}
            >
              {currentUser.username}
            </span>
            <span style={{ fontSize: 10, color: DESIGN_TOKENS.textMuted, lineHeight: 1.2 }}>
              {currentUser.role === 'admin' ? '系统管理员' : '标准用户'}
            </span>
          </div>
        </div>
      </Dropdown>

      {/* Password Change Modal */}
      <Modal
        title={
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <SafetyCertificateOutlined style={{ color: DESIGN_TOKENS.interactiveBlue }} />
            <span>修改登录密码</span>
          </div>
        }
        open={changePwdOpen}
        onCancel={() => {
          setChangePwdOpen(false);
          pwdForm.resetFields();
        }}
        footer={null}
        destroyOnClose
        centered
        width={420}
      >
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
          修改密码后，后端将即时吊销所有设备的现有会话。完成后需要使用新密码重新登录。
        </div>

        <Form form={pwdForm} layout="vertical" onFinish={handlePasswordChangeSubmit}>
          <Form.Item
            name="old_password"
            label="当前原密码"
            rules={[{ required: true, message: '请输入当前密码' }]}
          >
            <Input.Password placeholder="输入当前使用的密码" />
          </Form.Item>

          <Form.Item
            name="new_password"
            label="新密码"
            rules={[
              { required: true, message: '请输入新密码' },
              { min: 6, message: '新密码长度至少6位' },
            ]}
          >
            <Input.Password placeholder="输入至少6位新密码" />
          </Form.Item>

          <Form.Item
            name="confirm_new_password"
            label="确认新密码"
            rules={[{ required: true, message: '请再次输入新密码' }]}
          >
            <Input.Password placeholder="再次输入新密码以确认" />
          </Form.Item>

          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 20 }}>
            <Button
              onClick={() => {
                setChangePwdOpen(false);
                pwdForm.resetFields();
              }}
            >
              取消
            </Button>
            <Button type="primary" htmlType="submit" loading={submittingPwd}>
              确认修改
            </Button>
          </div>
        </Form>
      </Modal>
    </>
  );
};
