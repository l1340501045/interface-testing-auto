export const APP_PAGES = ["workbench", "cases", "environments", "tasks", "reports"] as const;

export type AppPage = (typeof APP_PAGES)[number];

export const PAGE_LABEL: Record<AppPage, string> = {
  workbench: "接口工作台",
  cases: "用例库",
  environments: "环境配置",
  tasks: "任务中心",
  reports: "测试报告",
};

export function pageFromHash(hash: string): AppPage {
  const value = hash.replace(/^#\/?/, "").split(/[/?]/, 1)[0];
  return APP_PAGES.find((page) => page === value) ?? "workbench";
}

export function pageHash(page: AppPage): string {
  return `#/${page}`;
}
