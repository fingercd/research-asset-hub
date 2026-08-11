import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  Check,
  GitCompareArrows,
  LoaderCircle,
  X,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import {
  getAssetDomains,
  getReadingStatus,
  type Asset,
  type GraphNode,
} from "../api/types";
import { annotationSnippet, formatAuthors } from "../lib/format";
import { useCompare } from "../state/CompareContext";
import { CopyIdButton } from "./CopyIdButton";

function assetFromNode(node: GraphNode): Asset {
  return {
    id: node.id,
    asset_type: node.asset_type,
    title: node.title ?? node.label ?? node.id,
    year: node.year,
    domain_slugs: node.domain_slugs,
    reading_status: node.reading_status,
  };
}

export function GraphNodeDrawer({
  node,
  onClose,
}: {
  node: GraphNode;
  onClose: () => void;
}) {
  const compare = useCompare();
  const asset = useQuery({
    queryKey: ["asset", node.id],
    queryFn: ({ signal }) => api.asset(node.id, signal),
    enabled: Boolean(node.id),
    retry: false,
  });
  const neighbors = useQuery({
    queryKey: ["graph", "neighbors", node.id, 1],
    queryFn: ({ signal }) => api.graphNeighbors(node.id, 1, signal),
    enabled: Boolean(node.id),
    retry: false,
  });
  const details = asset.data ?? assetFromNode(node);
  const selected = compare.has(node.id);
  const atLimit = !selected && compare.assets.length >= 4;
  const neighborNodes = (neighbors.data?.nodes ?? []).filter(
    (item) => item.id !== node.id,
  );

  return (
    <aside className="section-block graph-node-drawer" aria-label="节点详情">
      <div className="graph-node-drawer-head">
        <div>
          <p className="eyebrow">节点详情</p>
          <h2>{details.title_zh || details.title}</h2>
        </div>
        <button
          className="icon-button"
          type="button"
          aria-label="关闭节点详情"
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      {asset.isPending && (
        <p className="muted graph-node-drawer-loading">
          <LoaderCircle size={14} aria-hidden="true" /> 正在加载完整详情…
        </p>
      )}
      {asset.isError && (
        <p className="muted">完整详情暂不可用，当前显示图谱节点信息。</p>
      )}

      <dl className="graph-node-drawer-fields">
        <div>
          <dt>类型</dt>
          <dd>{details.asset_type ?? details.type ?? "资产"}</dd>
        </div>
        {!!details.authors?.length && (
          <div>
            <dt>作者</dt>
            <dd>{formatAuthors(details.authors)}</dd>
          </div>
        )}
        {(details.year || details.venue) && (
          <div>
            <dt>年份 / 会议</dt>
            <dd>
              {[details.year, details.venue].filter(Boolean).join(" · ") || "—"}
            </dd>
          </div>
        )}
        <div>
          <dt>阅读状态</dt>
          <dd>{getReadingStatus(details)}</dd>
        </div>
        {!!getAssetDomains(details).length && (
          <div>
            <dt>领域</dt>
            <dd>{getAssetDomains(details).join("、")}</dd>
          </div>
        )}
      </dl>

      {(details.summary || details.abstract) && (
        <p className="graph-node-drawer-summary">
          {details.summary || details.abstract}
        </p>
      )}
      <p className="annotation-snippet">
        <strong>批注：</strong>
        {annotationSnippet(details)}
      </p>

      {neighborNodes.length > 0 && (
        <div className="graph-node-drawer-relations">
          <strong>一跳关系（{neighborNodes.length}）</strong>
          <div>
            {neighborNodes.slice(0, 8).map((neighbor) => (
              <Link
                to={`/assets/${encodeURIComponent(neighbor.id)}`}
                key={neighbor.id}
              >
                <span>{neighbor.title ?? neighbor.label ?? neighbor.id}</span>
                <small>{neighbor.asset_type ?? "资产"}</small>
              </Link>
            ))}
          </div>
        </div>
      )}

      <div className="graph-node-drawer-actions">
        <CopyIdButton assetId={node.id} />
        <Link
          className="button secondary compact"
          to={`/assets/${encodeURIComponent(node.id)}`}
        >
          打开详情 <ArrowRight size={13} aria-hidden="true" />
        </Link>
        <button
          className={`button compact ${selected ? "ghost" : "primary"}`}
          type="button"
          disabled={atLimit}
          title={atLimit ? "最多对比 4 篇" : undefined}
          onClick={() => compare.toggle(details)}
        >
          {selected ? (
            <Check size={14} aria-hidden="true" />
          ) : (
            <GitCompareArrows size={14} aria-hidden="true" />
          )}
          {selected ? "已加入对比" : "加入对比"}
        </button>
      </div>
    </aside>
  );
}
