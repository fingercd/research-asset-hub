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
import { CopyIdButton } from "./CopyIdButton";
import { ReadingStatusControl } from "./ReadingStatusControl";
import { ResearchUnitActions } from "./ResearchUnitActions";

function assetType(asset: Asset): string {
  return asset.asset_type ?? asset.type ?? "asset";
}

export function ResearchUnitCard({ unit }: { unit: ResearchUnitSummary }) {
  const primary = unit.primary_asset;
  const compare = useCompare();
  const selected = compare.has(primary.id);
  const domains = getAssetDomains(primary);
  const remainingDomains = domains.slice(1, 4);
  const isRepository =
    unit.unit_kind === "repository" || assetType(primary) === "repository";
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
          {domains[0] && (
            <Link
              className="asset-domain-chip"
              to={`/domains/${encodeURIComponent(domains[0])}`}
            >
              {domains[0]}
            </Link>
          )}
          {primary.year && <span>{primary.year}</span>}
          {primary.venue && <span>{primary.venue}</span>}
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

        {remainingDomains.length > 0 && (
          <div className="tag-row" aria-label="所属领域">
            {remainingDomains.map((domain) => (
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
        <CopyIdButton assetId={primary.id} />
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
          repositoryLabel={
            isRepository
              ? primary.title
              : (unit.related_repositories[0]?.title ?? primary.title)
          }
        />
        {percentage !== null && <span className="score">相关度 {percentage}%</span>}
      </div>
    </article>
  );
}
