import React, { useState } from 'react';
import { Layout, Menu, Badge, Space, Button, Tooltip, Spin } from 'antd';
import {
  MessageOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  SettingOutlined,
} from '@ant-design/icons';
import { ChatPage } from './pages/ChatPage';
import { KnowledgePage } from './pages/KnowledgePage';
import { RetrievalDebugPage } from './pages/RetrievalDebugPage';
import { ModelsPage } from './pages/ModelsPage';
import { LoginPage } from './pages/LoginPage';
import { HeaderUserMenu } from './components/HeaderUserMenu';
import { AuthProvider, useAuth } from './context/AuthContext';
import { DESIGN_TOKENS } from './theme';

const { Header, Content } = Layout;

const MainLayout: React.FC = () => {
  const { currentUser, isAuthenticated, isLoading, deploymentMode } = useAuth();
  const [currentTab, setCurrentTab] = useState<string>('chat');
  const [debugKbId, setDebugKbId] = useState<string>('');

  // Primary business navigation items
  const primaryMenuItems = [
    {
      key: 'chat',
      icon: <MessageOutlined />,
      label: '智能知识问答',
    },
    {
      key: 'knowledge',
      icon: <DatabaseOutlined />,
      label: '知识库管理',
    },
    {
      key: 'models',
      icon: <SettingOutlined />,
      label: '模型管理中心',
    },
  ];

  const handleNavigateToDebug = (kbId: string) => {
    setDebugKbId(kbId);
    setCurrentTab('retrieval');
  };

  // 1. Initial Auth Discovery & Token Verification Spinner
  if (isLoading) {
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          backgroundColor: DESIGN_TOKENS.pageBg,
          gap: 16,
        }}
      >
        <Spin size="large" />
        <span style={{ fontSize: 13, color: DESIGN_TOKENS.textMuted }}>
          正在加载 AgentStudio-RAG 环境配置...
        </span>
      </div>
    );
  }

  // 2. Server Mode Authentication Gating
  if (deploymentMode === 'server' && !isAuthenticated) {
    return <LoginPage />;
  }

  // 3. Authenticated Workbench (Local mode or authenticated server session)
  return (
    <Layout style={{ minHeight: '100vh', backgroundColor: DESIGN_TOKENS.pageBg }}>
      <Header
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          backgroundColor: '#ffffff',
          borderBottom: `1px solid ${DESIGN_TOKENS.borderRegular}`,
          padding: '0 24px',
          height: 60,
          position: 'sticky',
          top: 0,
          zIndex: 100,
        }}
      >
        {/* Brand Area */}
        <div
          style={{ display: 'flex', alignItems: 'center', gap: 12, cursor: 'pointer' }}
          onClick={() => setCurrentTab('chat')}
        >
          <div
            style={{
              width: 32,
              height: 32,
              borderRadius: DESIGN_TOKENS.controlRadius,
              backgroundColor: DESIGN_TOKENS.interactiveBlue,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: '#ffffff',
              fontSize: 16,
              fontWeight: 700,
              boxShadow: '0 2px 8px rgba(37, 99, 235, 0.25)',
            }}
          >
            ⚡
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span
                style={{
                  fontWeight: 700,
                  fontSize: 15,
                  color: DESIGN_TOKENS.brandDark,
                  letterSpacing: -0.2,
                  lineHeight: 1.2,
                }}
              >
                AgentStudio
              </span>
              <span
                style={{
                  fontSize: 10,
                  fontWeight: 600,
                  backgroundColor: DESIGN_TOKENS.surfaceBg,
                  color: DESIGN_TOKENS.interactiveBlue,
                  padding: '1px 5px',
                  borderRadius: 4,
                  lineHeight: '14px',
                }}
              >
                RAG
              </span>
            </div>
            <div style={{ fontSize: 11, color: DESIGN_TOKENS.textMuted, lineHeight: 1.2 }}>
              企业级智能知识助手
            </div>
          </div>
        </div>

        {/* Primary Navigation Menu */}
        <Menu
          mode="horizontal"
          selectedKeys={['chat', 'knowledge', 'models'].includes(currentTab) ? [currentTab] : []}
          onClick={({ key }) => setCurrentTab(key)}
          items={primaryMenuItems}
          style={{
            flex: 1,
            justifyContent: 'center',
            borderBottom: 'none',
            fontSize: 14,
            fontWeight: 500,
          }}
        />

        {/* Right Secondary Actions & User Profile */}
        <Space size={14} align="center">
          {/* Secondary Entry: Retrieval Quality Debug Workbench */}
          <Tooltip title="检索召回与重排质量调试台">
            <Button
              type={currentTab === 'retrieval' ? 'primary' : 'default'}
              icon={<ThunderboltOutlined />}
              onClick={() => setCurrentTab('retrieval')}
              style={{
                fontSize: 12,
                fontWeight: 500,
                height: 32,
                padding: '0 12px',
                borderRadius: DESIGN_TOKENS.controlRadius,
                backgroundColor: currentTab === 'retrieval' ? undefined : DESIGN_TOKENS.surfaceBg,
                borderColor: currentTab === 'retrieval' ? undefined : DESIGN_TOKENS.borderRegular,
                color: currentTab === 'retrieval' ? undefined : DESIGN_TOKENS.textSecondary,
              }}
            >
              检索调试
            </Button>
          </Tooltip>

          {/* Clean Corporate Status Indicator */}
          <Badge
            status="success"
            text={<span style={{ fontSize: 12, color: DESIGN_TOKENS.textMuted }}>系统就绪</span>}
          />

          {/* User Profile & Account Action Dropdown / Local Mode Badge */}
          <HeaderUserMenu />
        </Space>
      </Header>

      {/*
        Keyed Content Container:
        Using key={currentUser?.id || 'anonymous'} ensures that whenever an account switch
        or logout/login occurs, React completely destroys previous component instances and
        remounts fresh state, completely preventing cross-tenant cached state leaks.
      */}
      <Content key={currentUser?.id || 'anonymous'} style={{ backgroundColor: DESIGN_TOKENS.pageBg }}>
        {currentTab === 'chat' && <ChatPage />}
        {currentTab === 'knowledge' && (
          <KnowledgePage onNavigateToDebug={handleNavigateToDebug} />
        )}
        {currentTab === 'retrieval' && (
          <RetrievalDebugPage initialKbId={debugKbId} />
        )}
        {currentTab === 'models' && <ModelsPage />}
      </Content>
    </Layout>
  );
};

export const App: React.FC = () => {
  return (
    <AuthProvider>
      <MainLayout />
    </AuthProvider>
  );
};
