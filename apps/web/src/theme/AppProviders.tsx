import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import type { ReactNode } from "react";

/**
 * 前端唯一的 Ant Design 上下文入口。
 *
 * 主题、中文和弹层上下文集中在这里，业务组件只组合真实控件，不各自复制一套 Provider。
 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ConfigProvider
      locale={zhCN}
      componentSize="middle"
      button={{ autoInsertSpace: false }}
      theme={{
        token: {
          colorPrimary: "#2f6fed",
          colorInfo: "#2f6fed",
          colorSuccess: "#1c6b47",
          colorWarning: "#8a5a12",
          colorError: "#a12632",
          colorText: "#17283c",
          colorTextSecondary: "#52667c",
          colorBorder: "#dce5ef",
          colorBgLayout: "#f5f7fa",
          borderRadius: 8,
          motion: false,
          fontFamily: 'system-ui, -apple-system, "PingFang SC", sans-serif',
        },
        components: {
          Layout: { headerBg: "#ffffff", bodyBg: "#f5f7fa" },
          Menu: { itemBg: "transparent", horizontalItemSelectedColor: "#2f6fed" },
          Table: { cellPaddingBlockSM: 6, cellPaddingInlineSM: 8 },
        },
      }}
    >
      <AntdApp>{children}</AntdApp>
    </ConfigProvider>
  );
}
