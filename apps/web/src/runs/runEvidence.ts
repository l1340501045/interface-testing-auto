import type { RunReport } from "../api/types";

/** 只有冻结来源与真实 worker 证明都指向同一预览 HMAC，才可贴回当前输入。 */
export function matchesResolutionEvidence(report: RunReport, contextFingerprint: string | null | undefined): boolean {
  if (!contextFingerprint) return false;
  return report.resolution?.context_fingerprint === contextFingerprint
    && report.context?.resolution?.guard === "ordinary_binding_enforced_v1"
    && report.context.resolution.context_fingerprint === contextFingerprint;
}
