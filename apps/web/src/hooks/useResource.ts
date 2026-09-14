/**
 * 带取消与重新加载的资源读取。
 *
 * 切换项目、环境或用例时，旧请求必须真正取消：仅靠“丢弃晚到的响应”会让上一
 * 范围的错误提示仍停留在一个已经无关的界面上。
 *
 * 更关键的是**数据必须带着自己的范围**：状态里同时保存“这份内容是哪个 key 的
 * 结果”，对外只在两者一致时才交出 data／error。否则组件在新范围的第一帧拿到的
 * 仍是上一个范围的数据，把它当成“已经加载好的新范围内容”去回填，就会把 A 项目
 * 的内容按 B 项目的身份显示甚至保存回去——仅仅清空数据是拦不住这一帧的，因为
 * 清空要等到 effect 执行之后。当前 key 与数据 key 不符时一律按“尚未加载”处理。
 *
 * 调用方可以据此按 scope key 重挂载组件：key 变了就是另一份资源，草稿状态不该复用。
 */
import { useCallback, useEffect, useRef, useState } from "react";

export interface ResourceState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  reload: () => void;
}

/** 一次加载结果连同它所属的范围；key 是这份数据唯一有效的范围标识。 */
interface Snapshot<T> {
  key: string | null;
  data: T | null;
  error: Error | null;
  loading: boolean;
}

export function useResource<T>(
  key: string | null,
  loader: (signal: AbortSignal) => Promise<T>,
): ResourceState<T> {
  const [snapshot, setSnapshot] = useState<Snapshot<T>>({
    key,
    data: null,
    error: null,
    loading: key !== null,
  });
  const [revision, setRevision] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  useEffect(() => {
    if (key === null) {
      setSnapshot({ key: null, data: null, error: null, loading: false });
      return;
    }

    const controller = new AbortController();
    setSnapshot((previous) => ({
      key,
      // 同一范围内的刷新保留当前内容；跨范围切换先清空，不显示上一范围的结果。
      data: previous.key === key ? previous.data : null,
      error: null,
      loading: true,
    }));

    void (async () => {
      try {
        const data = await loaderRef.current(controller.signal);
        if (!controller.signal.aborted) setSnapshot({ key, data, error: null, loading: false });
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (cause instanceof DOMException && cause.name === "AbortError") return;
        setSnapshot({
          key,
          data: null,
          error: cause instanceof Error ? cause : new Error("加载失败，请稍后重试"),
          loading: false,
        });
      }
    })();

    return () => controller.abort();
  }, [key, revision]);

  const reload = useCallback(() => setRevision((value) => value + 1), []);

  // 快照的 key 与当前 key 不一致：这份内容属于别的范围，不算已加载。
  const current = snapshot.key === key;
  return {
    data: current ? snapshot.data : null,
    error: current ? snapshot.error : null,
    loading: current ? snapshot.loading : key !== null,
    reload,
  };
}
