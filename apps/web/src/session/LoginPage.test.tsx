import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../theme/AppProviders";
import { LoginPage } from "./LoginPage";

describe("登录页", () => {
  it("账号密码由现有会话入口提交且一次表单提交只调用一次", async () => {
    const onSubmit = vi.fn();
    render(
      <AppProviders>
        <LoginPage onSubmit={onSubmit} error={null} notice={null} busy={false} />
      </AppProviders>,
    );

    fireEvent.change(screen.getByLabelText("账号"), { target: { value: "tester" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "secret" } });
    fireEvent.submit(screen.getByRole("form", { name: "登录" }));

    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    expect(onSubmit).toHaveBeenCalledWith("tester", "secret");
  });

  it("登录错误保持为持久中文表单反馈", () => {
    render(
      <AppProviders>
        <LoginPage onSubmit={vi.fn()} error="账号或密码错误" notice={null} busy={false} />
      </AppProviders>,
    );

    expect(screen.getByRole("alert").textContent).toContain("账号或密码错误");
  });
});
