/**
 * 登录页：本地账号密码登录，不提供自助注册，也不预填任何默认凭据。
 */
import { useState } from "react";

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
      <form
        aria-label="登录"
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit(username, password);
        }}
      >
        <span className="eyebrow">接口自动化测试与巡检平台</span>
        <h1>登录</h1>
        <label htmlFor="login-username">账号</label>
        <input
          id="login-username"
          name="username"
          value={username}
          autoComplete="username"
          required
          onChange={(event) => setUsername(event.target.value)}
        />
        <label htmlFor="login-password">密码</label>
        <input
          id="login-password"
          name="password"
          type="password"
          value={password}
          autoComplete="current-password"
          required
          onChange={(event) => setPassword(event.target.value)}
        />
        {notice ? <p className="hint">{notice}</p> : null}
        {error ? <ErrorText message={error} /> : null}
        <button type="submit" disabled={busy}>
          {busy ? "正在登录…" : "登录"}
        </button>
        <p className="caption">账号由管理员在本机初始化命令中创建，平台不提供公开注册。</p>
      </form>
    </main>
  );
}
