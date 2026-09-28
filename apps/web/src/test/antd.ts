import { fireEvent, screen, waitFor, within } from "@testing-library/react";

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** 通过真实 combobox 弹层选择一项，供迁移后的组件测试复用。 */
export async function selectAntOption(label: string, optionLabel: string): Promise<void> {
  const combobox = screen.getByRole("combobox", { name: label });
  fireEvent.mouseDown(combobox);
  const listId = combobox.getAttribute("aria-controls");
  if (listId === null) throw new Error(`下拉框“${label}”没有关联选项列表`);
  const listbox = await waitFor(() => {
    const current = document.getElementById(listId);
    if (current === null) throw new Error(`下拉框“${label}”的选项列表不存在`);
    return current;
  });
  const popup = listbox.closest<HTMLElement>(".ant-select-dropdown");
  if (popup === null) throw new Error(`下拉框“${label}”的选项列表不在当前弹层中`);
  const matches = await within(popup).findAllByText(new RegExp(`^${escapeRegExp(optionLabel)}$`));
  const optionText = matches.find((node) =>
    node.closest(".ant-select-item-option") !== null
    && node.closest('[hidden], [aria-hidden="true"], [style*="display: none"]') === null
  );
  if (optionText === undefined) throw new Error(`下拉框“${label}”中没有选项“${optionLabel}”`);
  fireEvent.click(optionText);
}

/** 读取业务显式放在 Select 根节点上的受控值，不依赖库内部 input 的搜索文本。 */
export function antSelectedValue(label: string): string {
  const combobox = screen.getByRole("combobox", { name: label });
  const root = combobox.closest<HTMLElement>("[data-selected-value]");
  if (root === null) throw new Error(`下拉框“${label}”没有受控值标记`);
  return root.dataset.selectedValue ?? "";
}

/** 读取当前可见选项文案，用于失效占位等不能只比较 value 的场景。 */
export function antSelectedLabel(label: string): string {
  const combobox = screen.getByRole("combobox", { name: label });
  const root = combobox.closest<HTMLElement>("[data-selected-value]");
  if (root === null) throw new Error(`下拉框“${label}”没有受控值标记`);
  return root.textContent?.trim() ?? "";
}
