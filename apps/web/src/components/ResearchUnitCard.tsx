import { Check, GitCompareArrows } from "lucide-react";
import { Link } from "react-router-dom";
import {
  getAssetDomains,
  getReadingStatus,
  type Asset,
  type ResearchUnitSummary,
} from "../api/types";
import { annotationSnippet, formatAuthors } from "../lib/format";
import { useCompare } from "../state/CompareContext";
import { ReadingStatusControl } from "./ReadingStatusControl";
import { ResearchUnitActions } from "./ResearchUnitActions";

function matchSourceLabel(unit: ResearchUnitSummary): string {
  if (
    unit.match_source === "related_repository" ||
    unit.match_source === "paper_and_related_repository"
  ) {
    return "命中关联代码";
  }
  if (unit.match_source === "paper") return "命中主论文";
  if (unit.match_source === "repository") return "命中代码仓库";
  if (unit.match_source === "browse") return "领域浏览";
  return unit.match_source || "聚合匹配";
}

function assetType(asset: Asset): string {
  return asset.asset_type ?? asset.type ?? "asset";
}

export function ResearchUnitCard({ unit }: { unit: ResearchUnitSummary }) {
  const primary = unit.primary_asset;
  const compare = useCompare();
  const selected = compare.has(primary.id);
  const domains = getAssetDomains(primary);
  const isRepository =
    unit.unit_kind === "repository" || assetType(primary) === "repository";
  const primaryKindLabel =
    assetType(primary) === "paper" ? "主论文" : assetType(primary);
  const relatedPapers = unit.matched_assets.filter(
    (asset) => asset.id !== primary.id && assetType(asset) === "paper",
  );
  const percentage =
    typeof unit.score === "number"
      ? Math.round(unit.score <= 1 ? unit.score * 100 : unit.score)
      : null;

  return (
    <article
      className={`asset-card research-unit-card ${
        isRepository ? "is-repository" : ""
      }`}
      data-unit-id={unit.unit_id}
    >
      <div className="asset-card-main">
        <div className="asset-card-kicker">
          <span className="asset-type">
            {isRepository ? "独立仓库" : primaryKindLabel}
          </span>
          {primary.year && <span>{primary.year}</span>}
          {primary.venue && <span>{primary.venue}</span>}
          <span className="match-source">
            匹配来源：<strong>{matchSourceLabel(unit)}</strong>
          </span>
          <span>{unit.covered_asset_count} 项资产</span>
        </div>
        <h3>
          <Link to={`/assets/${encodeURIComponent(primary.id)}`}>
            {primary.title_zh || primary.title}
          </Link>
        </h3>
        {primary.title_zh && primary.title_zh !== primary.title && (
          <p className="original-title">{primary.title}</p>
        )}
        <p className="asset-authors">{formatAuthors(primary.authors)}</p>
        <p className="asset-summary">
          {unit.snippet || primary.summary || primary.abstract || "暂无摘要。"}
        </p>
        <p className="annotation-snippet">
          <strong>批注：</strong>
          {annotationSnippet(primary)}
        </p>

        {isRepository ? (
          <div className="research-unit-relations">
            <strong>关联论文</strong>
            {relatedPapers.length ? (
              <div className="related-asset-links">
                {relatedPapers.map((paper) => (
                  <Link
                    to={`/assets/${encodeURIComponent(paper.id)}`}
                    key={paper.id}
                  >
                    {paper.title_zh || paper.title}
                  </Link>
                ))}
              </div>
            ) : (
              <span>暂无已确认关联论文</span>
            )}
          </div>
        ) : (
          unit.related_repositories.length > 0 && (
            <div className="research-unit-relations">
              <strong>关联仓库</strong>
              <div className="related-asset-links">
                {unit.related_repositories.map((repository) => (
                  <Link
                    to={`/assets/${encodeURIComponent(repository.id)}`}
                    key={repository.id}
                  >
                    {repository.title_zh || repository.title}
                  </Link>
                ))}
              </div>
            </div>
          )
        )}

        {!!domains.length && (
          <div className="tag-row" aria-label="所属领域">
            {domains.slice(0, 4).map((domain) => (
              <Link to={`/domains/${encodeURIComponent(domain)}`} key={domain}>
                {domain}
              </Link>
            ))}
          </div>
        )}
      </div>

      <div className="asset-card-side">
        <ReadingStatusControl
          assetId={primary.id}
          value={getReadingStatus(primary)}
          compact
        />
        <button
          className={`compare-toggle ${selected ? "is-selected" : ""}`}
          type="button"
          aria-pressed={selected}
          title={!selected && compare.assets.length >= 4 ? "最多对比 4 篇" : undefined}
          onClick={() => compare.toggle(primary)}
          disabled={!selected && compare.assets.length >= 4}
        >
          {selected ? (
            <Check size={14} aria-hidden="true" />
          ) : (
            <GitCompareArrows size={14} aria-hidden="true" />
          )}
          {selected ? "已加入对比" : "加入对比"}
        </button>
      </div>

      <div className="asset-card-footer">
        <ResearchUnitActions
          actions={unit.quick_actions}
          ownerAssetId={primary.id}
          repositoryOnly={isRepository}
        />
        {percentage !== null && <span className="score">相关度 {percentage}%</span>}
      </div>
    </article>
  );
}
