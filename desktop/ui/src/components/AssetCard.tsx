import {
  ArrowRight,
  Check,
  GitCompareArrows,
  MoreHorizontal,
} from "lucide-react";
import { Link } from "react-router-dom";
import { getAssetDomains, getReadingStatus, getRepresentationType, type Asset } from "../api/types";
import { annotationSnippet, formatAuthors } from "../lib/format";
import { useCompare } from "../state/CompareContext";
import { CopyIdButton } from "./CopyIdButton";
import { ReadingStatusControl } from "./ReadingStatusControl";
import { RepresentationActions } from "./RepresentationActions";

export function AssetCard({
  asset,
  compact = false,
  continueReading = false,
}: {
  asset: Asset;
  compact?: boolean;
  continueReading?: boolean;
}) {
  const compare = useCompare();
  const selected = compare.has(asset.id);
  const domains = getAssetDomains(asset);
  const annotation = annotationSnippet(asset);
  const isPaper = (asset.asset_type ?? asset.type) === "paper";
  const resumePdf = continueReading && (
    asset.representations?.some((item) => getRepresentationType(item) === "pdf" && item.availability === "available") ||
    asset.quick_actions?.some((item) => item.kind === "pdf" && item.status === "available")
  );

  return (
    <article className={`asset-card ${compact ? "is-compact" : ""}`}>
      <div className="asset-card-main">
        <div className="asset-card-kicker">
          <span className="asset-type">{asset.asset_type ?? asset.type ?? "asset"}</span>
          {asset.year && <span>{asset.year}</span>}
          {asset.venue && <span>{asset.venue}</span>}
        </div>
        <h3>
          <Link to={`/assets/${encodeURIComponent(asset.id)}`}>
            {asset.title_zh || asset.title}
          </Link>
        </h3>
        {asset.title_zh && asset.title_zh !== asset.title && (
          <p className="original-title">{asset.title}</p>
        )}
        <p className="asset-authors">{formatAuthors(asset.authors)}</p>
        {!compact && (
          <p className="asset-summary">
            {asset.summary || asset.abstract || "暂无摘要。"}
          </p>
        )}
        {annotation !== "无" && (
          <p className="annotation-snippet">
            <strong>批注：</strong>
            {annotation}
          </p>
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
      <div className="asset-card-footer">
        <div className="card-primary-actions">
          <Link
            className="button primary compact"
            to={`/assets/${encodeURIComponent(asset.id)}${resumePdf ? "?view=pdf" : ""}`}
          >
            {resumePdf ? "继续阅读 PDF" : "打开资料"} <ArrowRight size={14} aria-hidden="true" />
          </Link>
          {resumePdf && <Link className="text-link" to={`/assets/${encodeURIComponent(asset.id)}`}>查看资料</Link>}
          <details className="card-more-menu">
            <summary className="button secondary compact" role="button">
              <MoreHorizontal size={15} aria-hidden="true" /> 更多
            </summary>
            <div className="card-more-popover">
              <div className="card-more-section">
                <span>阅读状态</span>
                <ReadingStatusControl
                  assetId={asset.id}
                  value={getReadingStatus(asset)}
                  compact
                />
              </div>
              {!compact && (
                <div className="card-more-section">
                  <span>打开内容</span>
                  <RepresentationActions representations={asset.representations} />
                </div>
              )}
              <div className="card-more-utilities">
                <CopyIdButton assetId={asset.id} />
                {isPaper && (
                  <button
                    className={`compare-toggle ${selected ? "is-selected" : ""}`}
                    type="button"
                    aria-pressed={selected}
                    title={
                      !selected && compare.assets.length >= 4
                        ? "最多对比 4 篇"
                        : undefined
                    }
                    onClick={() => compare.toggle(asset)}
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
        {typeof asset.score === "number" && (
          <span className="score" title="用于结果排序，不表示匹配概率。">
            排序分数 {asset.score.toPrecision(3)}
          </span>
        )}
      </div>
    </article>
  );
}
