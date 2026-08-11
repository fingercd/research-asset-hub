import {
  getRepresentationType,
  type Asset,
  type QuickAction,
  type QuickActionKind,
  type ResearchUnitSummary,
} from "../api/types";

const representationKinds: Record<
  QuickActionKind,
  (representationType: string, role?: string) => boolean
> = {
  pdf: (type) => type === "pdf",
  html: (type, role) =>
    ["html", "html_document"].includes(type) && role !== "guide",
  code: (type) => ["repository", "source_code", "notebook"].includes(type),
};

function quickActionsForAsset(asset: Asset): QuickAction[] {
  return (Object.keys(representationKinds) as QuickActionKind[]).map((kind) => {
    const representation = asset.representations?.find((item) =>
      representationKinds[kind](getRepresentationType(item), item.role) &&
      (item.availability === undefined || item.availability === "available"),
    );
    const availability = representation?.availability;
    const status =
      availability === "missing" ||
      availability === "not_provided" ||
      availability === "network_required"
        ? availability
        : representation
          ? "available"
          : "not_provided";
    return {
      kind,
      status,
      representation_id: representation?.id,
      owner_asset_id: asset.id,
      reason:
        status === "not_provided" && kind === "pdf"
          ? "该资产没有可用 PDF。"
          : undefined,
      alternatives: [],
    };
  });
}

export function researchUnitFromAsset(asset: Asset): ResearchUnitSummary {
  return {
    unit_id: asset.id,
    unit_kind: asset.asset_type ?? asset.type ?? "asset",
    primary_asset: asset,
    related_repositories: [],
    quick_actions: quickActionsForAsset(asset),
    matched_assets: [asset],
    covered_asset_count: 1,
    score: asset.score,
    snippet: typeof asset.snippet === "string" ? asset.snippet : undefined,
    match_source:
      (asset.asset_type ?? asset.type) === "repository" ? "repository" : "paper",
  };
}
