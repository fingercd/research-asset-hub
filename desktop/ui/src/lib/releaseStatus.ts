import { getRepresentationType, type Asset, type QuickAction, type Representation } from "../api/types";

export type CodeOpenStatus =
  | "official_open"
  | "official_announced"
  | "official_placeholder"
  | "unavailable"
  | "third_party_only"
  | "unknown";
export type CodeLocalStatus = "available" | "full" | "partial" | "absent" | "stale" | "unknown";
export type CheckpointOpenStatus =
  | "open"
  | "announced"
  | "not_found"
  | "not_applicable"
  | "unknown";

export interface ReleaseStatus {
  code_open: CodeOpenStatus;
  code_local: CodeLocalStatus;
  code_local_at_verification: CodeLocalStatus;
  checkpoint_open: CheckpointOpenStatus;
  repository_url?: string;
  checkpoint_urls: string[];
  code_verified_at?: string;
  checkpoint_verified_at?: string;
}

function codeRepresentations(asset: Asset) {
  return [asset, ...(asset.related_repositories ?? [])].flatMap((owner) =>
    (owner.representations ?? []).filter((item) =>
      ["repository", "source_code", "notebook"].includes(getRepresentationType(item)),
    ),
  );
}

function localCodeAvailable(item: Representation): boolean {
  return item.availability === "available" && item.local_available !== false &&
    (item.local_available === true || Boolean(item.path_ref && item.materialization_kind !== "url"));
}

function currentLocalStatus(asset: Asset, quickActions: QuickAction[]): CodeLocalStatus {
  const action = quickActions.find((item) => item.kind === "code");
  if (action) {
    return action.status === "available" ? "available" : "absent";
  }
  const representations = codeRepresentations(asset);
  if (representations.some(localCodeAvailable)) return "available";
  return representations.length ? "absent" : "unknown";
}

export function codeSummaryForAsset(asset: Asset): string {
  const action = asset.quick_actions?.find((item) => item.kind === "code");
  const owner = action?.owner_asset_id === asset.id ? asset :
    asset.related_repositories?.find((item) => item.id === action?.owner_asset_id);
  const representation = codeRepresentations(asset).find((item) => item.id === action?.representation_id);
  const name = representation?.label || representation?.external_url || owner?.title;
  if (action) {
    const labels = {
      available: "本地可读",
      network_required: "远程代码，需联网",
      missing: "已登记代码当前不可用",
      not_provided: "无已确认代码",
    };
    return `${labels[action.status]}${name ? `：${name}` : ""}`;
  }
  const representations = codeRepresentations(asset);
  if (representations.length) {
    return representations.map((item) => {
      const status = item.availability !== "available" ? "已登记代码当前不可用" :
        localCodeAvailable(item) ? "本地可读" : "远程代码，需联网";
      return `${status}：${item.label || item.external_url || getRepresentationType(item)}`;
    }).join("；");
  }
  const source = [asset, ...(asset.related_repositories ?? [])]
    .flatMap((item) => item.source_urls ?? [])
    .find((url) => /github|gitlab|gitee|codeberg/i.test(url));
  return source ? `远程仓库（本地状态待核验）：${source}` : "无";
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
  const inferredCodeOpen: CodeOpenStatus = relatedRepositories.length
    ? "official_open"
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
    code_local: currentLocalStatus({ ...asset, related_repositories: relatedRepositories }, quickActions),
    code_local_at_verification: enumValue(
      configured?.code_local,
      ["full", "partial", "absent", "stale", "unknown"] as const,
      "unknown",
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
