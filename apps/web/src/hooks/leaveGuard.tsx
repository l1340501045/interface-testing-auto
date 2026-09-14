/**
 * 离开保护的统一登记处。
 *
 * 切换工作空间／项目／用例、关闭编辑器与退出登录都会卸载正在编辑的表单。仅由用例
 * 编辑器上报状态是不够的：项目变量、执行池白名单、环境编辑和凭证表单各有自己的
 * 草稿，其中包括密码框里的秘密值。任何一处没登记，用户就会在这些表单里丢掉
 * 没保存的内容——而秘密连“再输一次”都不一定做得到。
 *
 * 每个表单用 `useLeaveReport(key, {dirty, busy})` 登记自己，卸载即自动撤销，
 * 外壳读聚合结果决定要不要先问一次。登记表只是一个布尔汇总，不承载表单内容，
 * 因此秘密不会因为离开保护而被复制到别处。
 */
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

export interface LeaveState {
  /** 内容与基线不同，尚未保存。 */
  dirty: boolean;
  /** 有保存、发布或请求正在进行，此时离开会丢掉尚未看到的结果。 */
  busy: boolean;
}

interface Registry {
  report: (key: string, state: LeaveState) => void;
  clear: (key: string) => void;
}

/** 汇总结果，外加“排除某一项之后”的重算：见 `without`。 */
export interface LeaveAggregate extends LeaveState {
  /**
   * 排除某个登记项的汇总。
   *
   * 有一类离开是**某个表单自己发起**的：新建项目成功后自动切到新项目，就是刚提交的
   * 那张创建表单在要求离开。它自己的草稿这时已经清掉、结果也已经看到，却因为登记表
   * 还停在上一帧而把这次切换判成“有未保存修改”，弹出一个无所指的确认框。按 key 排除
   * 掉它，才问得出“除了它以外还有没有别的表单要丢”。
   */
  without: (key: string) => LeaveState;
}

const RegistryContext = createContext<Registry | null>(null);
const NOTHING_TO_LEAVE: LeaveAggregate = { dirty: false, busy: false, without: () => ({ dirty: false, busy: false }) };
const AggregateContext = createContext<LeaveAggregate>(NOTHING_TO_LEAVE);

export function LeaveGuardProvider({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<Record<string, LeaveState>>({});

  const registry = useMemo<Registry>(
    () => ({
      report(key, state) {
        setEntries((current) => {
          const previous = current[key];
          if (previous !== undefined && previous.dirty === state.dirty && previous.busy === state.busy) {
            return current;
          }
          return { ...current, [key]: state };
        });
      },
      clear(key) {
        setEntries((current) => {
          if (!(key in current)) return current;
          const next = { ...current };
          delete next[key];
          return next;
        });
      },
    }),
    [],
  );

  const aggregate = useMemo<LeaveAggregate>(() => {
    const summarize = (exclude: string | null): LeaveState =>
      Object.entries(entries).reduce<LeaveState>(
        (accumulated, [key, item]) =>
          key === exclude
            ? accumulated
            : {
                dirty: accumulated.dirty || item.dirty,
                busy: accumulated.busy || item.busy,
              },
        { dirty: false, busy: false },
      );
    return { ...summarize(null), without: (key) => summarize(key) };
  }, [entries]);

  return (
    <RegistryContext.Provider value={registry}>
      <AggregateContext.Provider value={aggregate}>{children}</AggregateContext.Provider>
    </RegistryContext.Provider>
  );
}

/** 全部已登记表单的汇总状态；没有 Provider 时返回“无未保存内容”。 */
export function useLeaveAggregate(): LeaveAggregate {
  return useContext(AggregateContext);
}

/** 登记一份表单的离开状态；卸载或 key 变化时自动撤销，不会留下过期的拦截。 */
export function useLeaveReport(key: string, state: LeaveState): void {
  const registry = useContext(RegistryContext);
  const { dirty, busy } = state;

  useEffect(() => {
    if (registry === null) return;
    registry.report(key, { dirty, busy });
    return () => registry.clear(key);
  }, [registry, key, dirty, busy]);
}
