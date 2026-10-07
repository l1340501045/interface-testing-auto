import type { RunReport, TargetRefV2 } from "../api/types";

/** 只有冻结来源与真实 worker 证明都指向同一预览 HMAC，才可贴回当前输入。 */
export function matchesResolutionEvidence(
  report: RunReport,
  contextFingerprint: string | null | undefined,
  targetRef?: TargetRefV2 | null,
): boolean {
  if (!contextFingerprint) return false;
  if (report.resolution?.context_fingerprint !== contextFingerprint || report.context?.resolution?.context_fingerprint !== contextFingerprint) return false;
  if (report.resolution.schema_version === 1) return (targetRef === null || targetRef === undefined)
    && report.context.resolution.schema_version === 1
    && report.context.resolution.guard === "ordinary_binding_enforced_v1";
  return targetRef !== null && targetRef !== undefined
    && report.context.resolution.schema_version === 2
    && report.context.resolution.guard === "selected_target_binding_enforced_v1"
    && JSON.stringify(report.resolution.target_ref) === JSON.stringify(targetRef);
}
