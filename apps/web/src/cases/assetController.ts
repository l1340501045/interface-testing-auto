import type { AssetMetadata, AssetOperationItem } from "../api/types";

export interface AssetEditorSnapshot {
  caseId: string;
  rev: number;
  inputRevision: number;
  folderId: string | null;
  dirty: boolean;
  busy: boolean;
}

export interface AssetEditorController {
  snapshot: () => AssetEditorSnapshot | null;
  lock: (token: string, expectedRev: number) => AssetEditorSnapshot | null;
  accept: (token: string, sourceRev: number, inputRevision: number, metadata: AssetMetadata) => boolean;
  release: (token: string) => void;
}

export interface AssetOperationLease {
  accept: (item: AssetOperationItem) => "accepted" | "stale" | "ignored";
  release: () => void;
}

export interface AssetCaseTarget { caseId: string; sourceRev: number; }
export interface AssetAcquireFailure { message: string; locate?: () => void; }
export type AcquireAssetOperation = (targets: AssetCaseTarget[], token: string) => AssetOperationLease | AssetAcquireFailure;
