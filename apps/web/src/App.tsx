import { useEffect, useState } from "react";

type Health = { status: "ok" | "error"; database: string; message: string };
const PAGE_TITLE = "开发环境已启动";

function isHealth(value: unknown): value is Health {
  if (!value || typeof value !== "object") return false;
  const data = value as Record<string, unknown>;
  return (data.status === "ok" || data.status === "error") && typeof data.database === "string" && typeof data.message === "string";
}

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [count, setCount] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setHealth(null);
    setError("");
    async function load() {
      try {
        const response = await fetch("/api/health", { signal: controller.signal });
        const data: unknown = await response.json();
        if (!isHealth(data)) throw new Error("健康检查返回格式不正确");
        if (!response.ok || data.status !== "ok") throw new Error(data.message);
        setHealth(data);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "后端暂不可达");
      }
    }
    void load();
    return () => controller.abort();
  }, [revision]);

  return (
    <main>
      <span className="eyebrow">接口自动化测试与巡检平台</span>
      <h1>{PAGE_TITLE}</h1>
      <p>前端、后端与数据库在开发容器中运行。当前页面用于验证开发环境，业务功能尚未开始交付。</p>
      <section aria-label="连接状态">
        <div><span>前端</span><strong>页面可访问</strong></div>
        <div><span>后端</span><strong aria-live="polite">{error || health?.message || "正在检查…"}</strong></div>
        <div><span>数据库</span><strong>{health?.database === "connected" ? "连接正常" : "等待后端确认"}</strong></div>
      </section>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="actions">
        <button onClick={() => setRevision((value) => value + 1)}>重新检测连接</button>
        <button onClick={() => setCount((value) => value + 1)}>交互计数：{count}</button>
      </div>
      <p className="caption">源码更新后页面与服务自动更新；交互计数可用于观察前端热更新是否保留状态。</p>
    </main>
  );
}
