import { ApiError } from "../api/client";

/** 仅这些精确 status/code 能证明首次受理前已经拒绝。 */
const INITIAL_RUN_REJECTIONS = new Map<string, number>([
  ["target_required", 400],
  ["case_invalid", 400],
  ["environment_url_invalid", 400],
  ["target_not_allowed", 400],
  ["variable_undefined", 400],
  ["binding_invalid", 400],
  ["credential_slot_conflict", 400],
  ["production_blocked", 403],
  ["pool_unavailable", 400],
  ["pool_not_granted", 403],
  ["pool_config_invalid", 400],
  ["not_found", 404],
  ["forbidden", 403],
  ["client_contract_required", 409],
  ["resolution_context_changed", 409],
  ["config_inconsistent", 409],
]);

export function isInitialRunRejection(cause: unknown): cause is ApiError {
  return cause instanceof ApiError && INITIAL_RUN_REJECTIONS.get(cause.code) === cause.status;
}
