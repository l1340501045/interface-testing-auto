import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../theme/AppProviders";
import { CaseHeading } from "./CaseHeading";

describe("编辑器目录可用性", () => {
  it("只把available目录作为新目标，原不可用归属仍以失效占位保留", async () => {
    render(
      <AppProviders>
        <CaseHeading
          name="旧目录用例"
          onNameChange={() => {}}
          folderId="blocked"
          onFolderChange={() => {}}
          folders={[
            { id: "available", parent_id: null, name: "可用目录", archived_at: null, rev: 1, availability: "available" },
            { id: "blocked", parent_id: "available", name: "旧归档祖先下目录", archived_at: null, rev: 2, availability: "ancestor_archived" },
            { id: "invalid", parent_id: null, name: "关系异常目录", archived_at: null, rev: 1, availability: "invalid_parent_chain" },
          ]}
          folderUnavailable
          folderPlaceholder="原目录已失效"
          dirty={false}
          busy={false}
          readOnly={false}
          creating={false}
          isNew={false}
          versionCount={0}
          onSave={vi.fn()}
          onPublish={vi.fn()}
          onClose={vi.fn()}
        />
      </AppProviders>,
    );
    expect(screen.getByLabelText("所属目录").closest(".ant-select")?.textContent).toContain("原目录已失效");
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "所属目录" }));
    expect(await screen.findByRole("option", { name: "可用目录" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "旧归档祖先下目录" })).toBeNull();
    expect(screen.queryByRole("option", { name: "关系异常目录" })).toBeNull();
  });
});
