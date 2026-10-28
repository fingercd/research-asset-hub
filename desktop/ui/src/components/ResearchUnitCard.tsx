import {
  ArrowRight,
  Check,
  GitCompareArrows,
  Link2,
  MoreHorizontal,
} from "lucide-react";
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
import { ReleaseStatusBadges } from "./ReleaseStatusBadges";
import { ResearchUnitActions } from "./ResearchUnitActions";

function assetType(asset: Asset): string {
  return asset.asset_type ?? asset.type ?? "asset";
}

export function ResearchUnitCard({
  unit,
  compact = false,
}: {
  unit: ResearchUnitSummary;
  compact?: boolean;
}) {
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
  const relationCount = isRepository
    ? relatedPapers.length
    : unit.related_repositories.length;
  const annotation = annotationSnippet(primary);
  const prominentVenue = Boolean(primary.venue && !/arxiv/i.test(primary.venue));
  const matchLabels: Record<string, string> = {
    paper: "命中论文",
    related_repository: "命中关联代码",
    paper_and_related_repository: "命中论文与关联代码",
    repository: "命中代码仓库",
  };
  const matchLabel = matchLabels[unit.match_source ?? ""];

  const quickActions = (
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
  );
  const summaryContent = (
    <>
      <p className="asset-authors">{formatAuthors(primary.authors)}</p>
      <p className="asset-summary">
        {unit.snippet || primary.summary || primary.abstract || "暂无摘要。"}
      </p>
    </>
  );

  return (
    <article
      className={`asset-card research-unit-card ${compact ? "is-catalog-compact" : ""} ${
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
          {primary.year && !prominentVenue && <span>{primary.year}</span>}
          {primary.venue && (
            <span className={prominentVenue ? "research-unit-venue" : undefined}>
              {primary.venue}
              {prominentVenue && primary.year && !primary.venue.includes(String(primary.year))
                ? ` ${primary.year}`
                : ""}
            </span>
          )}
        </div>
        <h3>
          <Link to={`/assets/${encodeURIComponent(primary.id)}`}>
            {primary.title_zh || primary.title}
          </Link>
        </h3>
        {primary.title_zh && primary.title_zh !== primary.title && (
          <p className="original-title">{primary.title}</p>
        )}
        {compact ? (
          <details className="compact-summary">
            <summary>摘要与作者</summary>
            {summaryContent}
          </details>
        ) : summaryContent}
        {compact && (
          <span className="compact-reading-status">
            {({ unread: "未读", reading: "阅读中", read: "已读" })[getReadingStatus(primary)]}
          </span>
        )}
        {!compact && !isRepository && (
          <ReleaseStatusBadges
            asset={primary}
            quickActions={unit.quick_actions}
            relatedRepositories={unit.related_repositories}
          />
        )}
        {!compact && annotation !== "无" && (
          <p className="annotation-snippet">
            <strong>批注：</strong>
            {annotation}
          </p>
        )}

        {relationCount > 0 && (
          <p className="research-unit-context">
            <Link2 size={14} aria-hidden="true" />
            {isRepository
              ? `关联 ${relationCount} 篇论文`
              : `关联 ${relationCount} 个代码仓库`}
          </p>
        )}

        {!compact && remainingDomains.length > 0 && (
          <div className="tag-row" aria-label="所属领域">
            {remainingDomains.map((domain) => (
              <Link to={`/domains/${encodeURIComponent(domain)}`} key={domain}>
                {domain}
              </Link>
            ))}
          </div>
        )}
      </div>

      <div className="asset-card-footer">
        <div className="card-primary-actions">
          <Link
            className="button primary compact"
            to={`/assets/${encodeURIComponent(primary.id)}`}
          >
            打开资料 <ArrowRight size={14} aria-hidden="true" />
          </Link>
          <details className="card-more-menu">
            <summary className="button secondary compact" role="button">
              <MoreHorizontal size={15} aria-hidden="true" />
              更多
            </summary>
            <div className="card-more-popover">
              <div className="card-more-section">
                <span>阅读状态</span>
                <ReadingStatusControl
                  assetId={primary.id}
                  value={getReadingStatus(primary)}
                  compact
                />
              </div>
              {!compact && (
                <div className="card-more-section">
                  <span>打开内容</span>
                  {quickActions}
                </div>
              )}
              <div className="card-more-utilities">
                <CopyIdButton assetId={primary.id} />
                {!isRepository && (
                  <button
                    className={`compare-toggle ${selected ? "is-selected" : ""}`}
                    type="button"
                    aria-pressed={selected}
                    title={
                      !selected && compare.assets.length >= 4
                        ? "最多对比 4 篇"
                        : undefined
                    }
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
                )}
              </div>
            </div>
          </details>
        </div>
        {compact && quickActions}
        {matchLabel ? (
          <span className="score">{matchLabel}</span>
        ) : typeof unit.score === "number" && unit.match_source !== "browse" ? (
          <span className="score" title="用于结果排序，不表示匹配概率。">
            排序分数 {unit.score.toPrecision(3)}
          </span>
        ) : null}
      </div>
    </article>
  );
}
