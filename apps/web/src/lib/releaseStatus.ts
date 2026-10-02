import type { Asset, QuickAction } from "../api/types";

export type CodeOpenStatus =
  | "official_open"
  | "official_announced"
  | "official_placeholder"
  | "unavailable"
  | "third_party_only"
  | "unknown";
export type CodeLocalStatus = "full" | "partial" | "absent" | "stale" | "unknown";
export type CheckpointOpenStatus =
  | "open"
  | "announced"
  | "not_found"
  | "not_applicable"
  | "unknown";

export interface ReleaseStatus {
  code_open: CodeOpenStatus;
  code_local: CodeLocalStatus;
  checkpoint_open: CheckpointOpenStatus;
  repository_url?: string;
  checkpoint_urls: string[];
  code_verified_at?: string;
  checkpoint_verified_at?: string;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function webUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.toString()
      : undefined;
  } catch {
    return undefined;
  }
}

function enumValue<T extends string>(
  value: unknown,
  allowed: readonly T[],
  fallback: T,
): T {
  return typeof value === "string" && allowed.includes(value as T)
    ? (value as T)
    : fallback;
}

export function releaseStatusForAsset(
  asset: Asset,
  quickActions: QuickAction[] = asset.quick_actions ?? [],
  relatedRepositories: Asset[] = asset.related_repositories ?? [],
): ReleaseStatus {
  const configured = record(asset.attributes?.release_status);
  const codeAction = quickActions.find((item) => item.kind === "code");
  const inferredCodeOpen: CodeOpenStatus = relatedRepositories.length
    ? "official_open"
    : "unknown";
  const inferredCodeLocal: CodeLocalStatus =
    codeAction?.status === "available"
      ? "full"
      : codeAction?.status === "network_required"
        ? "absent"
        : "unknown";
  const checkpointValues = Array.isArray(configured?.checkpoint_urls)
    ? configured.checkpoint_urls
    : [];
  const legacyVerifiedAt =
    typeof configured?.verified_at === "string" ? configured.verified_at : undefined;
  return {
    code_open: enumValue(
      configured?.code_open,
      [
        "official_open",
        "official_announced",
        "official_placeholder",
        "unavailable",
        "third_party_only",
        "unknown",
      ] as const,
      inferredCodeOpen,
    ),
    code_local: enumValue(
      configured?.code_local,
      ["full", "partial", "absent", "stale", "unknown"] as const,
      inferredCodeLocal,
    ),
    checkpoint_open: enumValue(
      configured?.checkpoint_open,
      ["open", "announced", "not_found", "not_applicable", "unknown"] as const,
      "unknown",
    ),
    repository_url:
      webUrl(configured?.repository_url) ??
      relatedRepositories
        .flatMap((item) => item.source_urls ?? [])
        .map(webUrl)
        .find((value): value is string => Boolean(value)),
    checkpoint_urls: checkpointValues
      .map(webUrl)
      .filter((value): value is string => Boolean(value)),
    code_verified_at:
      typeof configured?.code_verified_at === "string"
        ? configured.code_verified_at
        : legacyVerifiedAt,
    checkpoint_verified_at:
      typeof configured?.checkpoint_verified_at === "string"
        ? configured.checkpoint_verified_at
        : legacyVerifiedAt,
  };
}
