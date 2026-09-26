/**
 * 会话状态：登录、退出、当前身份与可访问工作空间。
 *
 * 页面加载时先问一次 `/me`，而不是把身份缓存在 localStorage：会话是否仍然有效
 * 只有服务端知道，本地缓存会让已撤销的会话继续显示业务界面。
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, apiGet, apiSend, invalidateClientSession, onUnauthorized } from "../api/client";
import { toSession } from "../api/guards";
import type { SessionInfo } from "../api/types";

export interface SessionState {
  session: SessionInfo | null;
  loading: boolean;
  error: string | null;
  /** 会话是在使用过程中被服务端判定失效的，用于提示用户重新登录。 */
  expired: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

export function useSession(): SessionState {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expired, setExpired] = useState(false);
  const active = useRef(true);
  const known = useRef<SessionInfo | null>(null);

  useEffect(() => {
    active.current = true;
    const controller = new AbortController();
    void (async () => {
      try {
        const current = await apiGet("/me", toSession, controller.signal);
        known.current = current;
        if (active.current) setSession(current);
      } catch (cause) {
        // 未登录是正常初始状态，不当作错误提示；网络故障才需要告知用户。
        if (!controller.signal.aborted && !(cause instanceof ApiError && cause.isUnauthenticated)) {
          setError(cause instanceof Error ? cause.message : "无法确认登录状态");
        }
      } finally {
        if (active.current) setLoading(false);
      }
    })();
    return () => {
      active.current = false;
      controller.abort();
    };
  }, []);

  // 任何请求收到 401 都说明服务端已不认这个会话。这里清掉身份并回到登录页，
  // 而不是留着业务界面继续显示可能已经无权的数据。
  useEffect(
    () =>
      onUnauthorized(() => {
        if (known.current !== null) setExpired(true);
        known.current = null;
        setSession(null);
      }),
    [],
  );

  const login = useCallback(async (username: string, password: string) => {
    setError(null);
    try {
      const info = await apiSend("/auth/login", "POST", { username, password }, toSession);
      known.current = info;
      setExpired(false);
      setSession(info);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登录失败");
      throw cause;
    }
  }, []);

  const logout = useCallback(async () => {
    // 点击退出的同一帧先清主体与敏感 UI；远端撤销独立完成，不让网络等待保留旧编辑会话。
    invalidateClientSession();
    known.current = null;
    setExpired(false);
    setSession(null);
    try {
      await apiSend("/auth/logout", "POST", undefined, () => null);
    } catch (cause) {
      // 退出失败也要清掉本地身份：服务端会话可能已不可达，继续展示业务界面更危险。
      if (!(cause instanceof ApiError) && !(cause instanceof DOMException)) {
        setError(cause instanceof Error ? cause.message : "退出登录时出错");
      }
    } finally {
      known.current = null;
    }
  }, []);

  return { session, loading, error, expired, login, logout };
}
