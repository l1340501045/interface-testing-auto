/**
 * 登录页：本地账号密码登录，不提供自助注册，也不预填任何默认凭据。
 */
import { useState } from "react";
import { Button, Form, Input, Typography } from "antd";

import { ErrorText } from "../components/Feedback";

export function LoginPage({
  onSubmit,
  error,
  notice,
  busy,
}: {
  onSubmit: (username: string, password: string) => void;
  error: string | null;
  notice?: string | null;
  busy: boolean;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  return (
    <main className="login">
      <Form
        layout="vertical"
        aria-label="登录"
        onFinish={() => onSubmit(username, password)}
      >
        <span className="eyebrow">接口自动化测试与巡检平台</span>
        <Typography.Title level={1}>登录</Typography.Title>
        <Form.Item label="账号" required htmlFor="login-username">
          <Input id="login-username" name="username" value={username} autoComplete="username" required onChange={(event) => setUsername(event.target.value)} />
        </Form.Item>
        <Form.Item label="密码" required htmlFor="login-password">
          <Input.Password id="login-password" name="password" value={password} autoComplete="current-password" required onChange={(event) => setPassword(event.target.value)} />
        </Form.Item>
        {notice ? <p className="hint">{notice}</p> : null}
        {error ? <ErrorText message={error} /> : null}
        <Button type="primary" htmlType="submit" loading={busy} block>
          {busy ? "正在登录…" : "登录"}
        </Button>
        <p className="caption">账号由管理员在本机初始化命令中创建，平台不提供公开注册。</p>
      </Form>
    </main>
  );
}
