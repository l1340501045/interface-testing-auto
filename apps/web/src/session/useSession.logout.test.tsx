import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiGet: vi.fn(), apiSend: vi.fn(), invalidateClientSession: vi.fn(), onUnauthorized: vi.fn(() => vi.fn()) };
});

import { apiGet, apiSend, invalidateClientSession } from "../api/client";
import { useSession } from "./useSession";

describe("主动退出同步清理", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiGet).mockResolvedValue({ user: { user_id: "u1", username: "tester", display_name: "测试员", is_admin: true }, workspaces: [] });
  });

  it("退出 API 未返回时主体和业务 UI 已清", async () => {
    let finish!: () => void;
    vi.mocked(apiSend).mockImplementation(() => new Promise((resolve) => { finish = () => resolve(null); }));
    const { result } = renderHook(() => useSession());
    await waitFor(() => expect(result.current.session?.user.user_id).toBe("u1"));

    let pending!: Promise<void>;
    act(() => { pending = result.current.logout(); });
    expect(result.current.session).toBeNull();
    expect(invalidateClientSession).toHaveBeenCalledTimes(1);

    await act(async () => { finish(); await pending; });
    expect(result.current.session).toBeNull();
  });
});
