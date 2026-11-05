import type { ThemeConfig } from 'antd';

/**
 * AgentStudio 2.0 Design Tokens
 * Unified design variables for corporate AI SaaS aesthetics
 */
export const DESIGN_TOKENS = {
  // Brand & Accent Colors
  brandPrimary: '#1e293b', // Slate 800 - Deep corporate brand slate
  brandDark: '#0f172a', // Slate 900 - Dark text / brand contrast
  interactiveBlue: '#2563eb', // Royal Blue 600 - Primary actions & interactive elements
  interactiveHover: '#1d4ed8', // Blue 700 - Hover state
  interactiveLight: '#eff6ff', // Blue 50 - Subtle active selection

  // Surface & Layout Backgrounds
  pageBg: '#f8fafc', // Slate 50 - Global calm canvas
  surfaceBg: '#f1f5f9', // Slate 100 - Secondary containers & cards
  cardBg: '#ffffff', // Pure white card surfaces
  sidebarBg: '#ffffff', // Left sidebar background

  // Borders & Dividers
  borderRegular: '#e2e8f0', // Slate 200 - Clean 1px card/divider borders
  borderLight: '#f1f5f9', // Slate 100 - Subtle item dividers
  borderFocus: '#2563eb', // Input focus border

  // Text Hierarchy
  textMain: '#0f172a', // Slate 900 - High-contrast readable body text
  textSecondary: '#475569', // Slate 600 - Secondary labels & headings
  textMuted: '#64748b', // Slate 500 - Metadata & timestamps
  textDisabled: '#94a3b8', // Slate 400 - Placeholder text

  // Semantic Status Colors (Restrained Palette)
  statusSuccess: '#10b981', // Emerald 500
  statusWarning: '#f59e0b', // Amber 500
  statusError: '#ef4444', // Red 500
  statusInfo: '#2563eb', // Blue 600
  statusAgent: '#8b5cf6', // Purple 500

  // Geometry
  cardRadius: 12,
  controlRadius: 8,
  pillRadius: 9999,
} as const;

export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: DESIGN_TOKENS.interactiveBlue,
    colorInfo: DESIGN_TOKENS.interactiveBlue,
    colorSuccess: DESIGN_TOKENS.statusSuccess,
    colorWarning: DESIGN_TOKENS.statusWarning,
    colorError: DESIGN_TOKENS.statusError,
    colorTextBase: DESIGN_TOKENS.textMain,
    colorTextSecondary: DESIGN_TOKENS.textMuted,
    colorBgBase: DESIGN_TOKENS.cardBg,
    colorBgLayout: DESIGN_TOKENS.pageBg,
    borderRadius: DESIGN_TOKENS.controlRadius,
    borderRadiusLG: DESIGN_TOKENS.cardRadius,
    borderRadiusSM: 6,
    fontFamily:
      '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif',
    boxShadowSecondary: '0 4px 16px rgba(15, 23, 42, 0.04)',
    boxShadow: '0 1px 3px rgba(15, 23, 42, 0.05), 0 1px 2px rgba(15, 23, 42, 0.03)',
    wireframe: false,
  },
  components: {
    Button: {
      borderRadius: DESIGN_TOKENS.controlRadius,
      controlHeight: 36,
      fontWeight: 500,
    },
    Card: {
      borderRadiusLG: DESIGN_TOKENS.cardRadius,
      boxShadowTertiary: '0 1px 3px rgba(15, 23, 42, 0.03)',
    },
    Menu: {
      itemColor: DESIGN_TOKENS.textMuted,
      itemSelectedColor: DESIGN_TOKENS.interactiveBlue,
      itemHoverColor: DESIGN_TOKENS.brandPrimary,
      horizontalItemSelectedColor: DESIGN_TOKENS.interactiveBlue,
      fontSize: 14,
    },
    Tag: {
      borderRadiusSM: 6,
    },
    Input: {
      borderRadius: DESIGN_TOKENS.controlRadius,
      activeBorderColor: DESIGN_TOKENS.interactiveBlue,
      hoverBorderColor: '#3b82f6',
    },
    Select: {
      borderRadius: DESIGN_TOKENS.controlRadius,
    },
    Modal: {
      borderRadiusLG: DESIGN_TOKENS.cardRadius,
    },
    Drawer: {
      borderRadiusSM: DESIGN_TOKENS.cardRadius,
    },
  },
};
