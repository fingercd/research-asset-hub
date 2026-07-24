import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  BotMessageSquare,
  CalendarDays,
  Code2,
  FileText,
  GitCompareArrows,
  Network,
  Quote,
  ScrollText,
  Sparkles,
  StickyNote,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import {
  getAssetDomains,
  getReadingStatus,
  getRepresentationType,
  type GraphEdge,
  type GraphNode,
  type Representation,
} from "../api/types";
import { AnnotationComposer } from "../components/AnnotationComposer";
import { AssetCard } from "../components/AssetCard";
import {
  CodePanel,
  GuidePanel,
  HtmlPanel,
} from "../components/DocumentPanels";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { ReadingStatusControl } from "../components/ReadingStatusControl";
import { RepresentationActions } from "../components/RepresentationActions";
import {
  annotationBody,
  formatAuthors,
  formatBytes,
  formatDate,
  representationLabel,
} from "../lib/format";
import { useCompare } from "../state/CompareContext";

const PdfPanel = lazy(() =>
  import("../components/PdfPanel").then((module) => ({
    default: module.PdfPanel,
  })),
);

type DetailTab =
  | "overview"
  | "pdf"
  | "code"
  | "guide"
  | "relations"
  | "similar"
  | "html";

const tabs: Array<{
  id: DetailTab;
  label: string;
  icon: typeof FileText;
}> = [
  { id: "overview", label: "概览与批注", icon: ScrollText },
  { id: "pdf", label: "PDF", icon: FileText },
  { id: "code", label: "代码", icon: Code2 },
  { id: "guide", label: "导读", icon: ScrollText },
  { id: "relations", label: "关系", icon: Network },
  { id: "similar", label: "相似论文", icon: Sparkles },
  { id: "html", label: "原始 HTML", icon: BotMessageSquare },
];

function firstRepresentation(
  representations: Representation[],
  predicate: (item: Representation) => boolean,
) {
  return representations.find(
    (item) => item.availability !== "missing" && predicate(item),
  );
}

function graphParts(value: unknown): { nodes: GraphNode[]; edges: GraphEdge[] } {
  if (!value || typeof value !== "object") return { nodes: [], edges: [] };
  const graph = value as {
    nodes?: GraphNode[];
    assets?: GraphNode[];
    edges?: GraphEdge[];
    relations?: GraphEdge[];
  };
  return {
    nodes: graph.nodes ?? graph.assets ?? [],
    edges: graph.edges ?? graph.relations ?? [],
  };
}

export function AssetDetailPage() {
  const { assetId = "" } = useParams();
  const compare = useCompare();
  const [activeTab, setActiveTab] = useState<DetailTab>("overview");
  const asset = useQuery({
    queryKey: ["asset", assetId],
    queryFn: ({ signal }) => api.asset(assetId, signal),
    enabled: Boolean(assetId),
  });
  const representations = useQuery({
    queryKey: ["representations", assetId],
    queryFn: ({ signal }) => api.representations(assetId, signal),
    enabled: Boolean(assetId),
  });
  const annotations = useQuery({
    queryKey: ["annotations", assetId],
    queryFn: ({ signal }) => api.annotations(assetId, signal),
    enabled: Boolean(assetId),
  });
  const similar = useQuery({
    queryKey: ["similar", assetId],
    queryFn: ({ signal }) => api.similar(assetId, signal),
    enabled: Boolean(assetId) && activeTab === "similar",
    retry: false,
  });
  const relations = useQuery({
    queryKey: ["graph", "neighbors", assetId, 1],
    queryFn: ({ signal }) => api.graphNeighbors(assetId, 1, signal),
    enabled: Boolean(assetId) && activeTab === "relations",
  });

  if (asset.isPending) {
    return (
      <div className="page">
        <LoadingState label="正在加载资产详情…" />
      </div>
    );
  }
  if (asset.isError) {
    return (
      <div className="page">
        <Link className="back-link" to="/search">
          <ArrowLeft size={15} aria-hidden="true" /> 返回搜索
        </Link>
        <ErrorState error={asset.error} retry={() => void asset.refetch()} />
      </div>
    );
  }

  const data = asset.data;
  const reps = representations.data ?? data.representations ?? [];
  const notes = annotations.data?.items ?? data.annotations ?? [];
  const domains = getAssetDomains(data);
  const selected = compare.has(data.id);
  const pdf = firstRepresentation(
    reps,
    (item) => getRepresentationType(item) === "pdf",
  );
  const code = firstRepresentation(reps, (item) =>
    ["repository", "source_code", "notebook"].includes(
      getRepresentationType(item),
    ),
  );
  const guide = firstRepresentation(
    reps,
    (item) =>
      ["guide", "analysis", "markdown"].includes(getRepresentationType(item)) ||
      item.role === "guide",
  );
  const html = firstRepresentation(
    reps,
    (item) =>
      ["html", "html_document"].includes(getRepresentationType(item)) &&
      item.role !== "guide",
  );
  const related = graphParts(relations.data);

  return (
    <div className="page asset-detail-page">
      <Link className="back-link" to="/search">
        <ArrowLeft size={15} aria-hidden="true" /> 返回搜索
      </Link>

      <header className="asset-hero">
        <div className="asset-hero-main">
          <div className="asset-card-kicker">
            <span className="asset-type">
              {data.asset_type ?? data.type ?? "asset"}
            </span>
            {data.year && <span>{data.year}</span>}
            {data.venue && <span>{data.venue}</span>}
          </div>
          <h1>{data.title_zh || data.title}</h1>
          {data.title_zh && data.title_zh !== data.title && (
            <p className="asset-hero-original">{data.title}</p>
          )}
          <p className="asset-hero-authors">{formatAuthors(data.authors)}</p>
          {!!domains.length && (
            <div className="tag-row">
              {domains.map((domain) => (
                <Link
                  to={`/domains/${encodeURIComponent(domain)}`}
                  key={domain}
                >
                  {domain}
                </Link>
              ))}
              {data.tags?.map((tag) => <span key={tag}>#{tag}</span>)}
            </div>
          )}
        </div>
        <div className="asset-hero-actions">
          <ReadingStatusControl
            assetId={data.id}
            value={getReadingStatus(data)}
          />
          <button
            className={`button secondary ${selected ? "is-selected" : ""}`}
            type="button"
            onClick={() => compare.toggle(data)}
            disabled={!selected && compare.assets.length >= 4}
          >
            <GitCompareArrows size={15} aria-hidden="true" />
            {selected ? "已加入对比" : "加入对比"}
          </button>
        </div>
        <RepresentationActions representations={reps} />
      </header>

      <div className="asset-tabs" role="tablist" aria-label="资产阅读视图">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            id={`detail-tab-${id}`}
            className={activeTab === id ? "is-active" : ""}
            type="button"
            role="tab"
            aria-selected={activeTab === id}
            aria-controls={`detail-panel-${id}`}
            onClick={() => setActiveTab(id)}
            key={id}
          >
            <Icon size={15} aria-hidden="true" />
            {label}
          </button>
        ))}
      </div>

      <div
        id={`detail-panel-${activeTab}`}
        className="asset-tab-panel"
        role="tabpanel"
        aria-labelledby={`detail-tab-${activeTab}`}
      >
        {activeTab === "overview" && (
          <div className="detail-layout">
            <div className="detail-main">
              <section className="section-block">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">内容概览</p>
                    <h2>摘要与导读</h2>
                  </div>
                </div>
                {data.summary && (
                  <div className="summary-callout">
                    <Quote size={19} aria-hidden="true" />
                    <p>{data.summary}</p>
                  </div>
                )}
                <div className="prose">
                  {data.abstract || data.content ? (
                    <p>{data.abstract || data.content}</p>
                  ) : (
                    <p className="muted">暂无可展示的摘要或正文。</p>
                  )}
                </div>
              </section>

              <section className="section-block" id="annotations">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">个人知识</p>
                    <h2>批注</h2>
                  </div>
                  <span className="count-badge">{notes.length}</span>
                </div>
                {annotations.isPending ? (
                  <LoadingState label="正在加载批注…" />
                ) : annotations.isError ? (
                  <ErrorState
                    error={annotations.error}
                    retry={() => void annotations.refetch()}
                  />
                ) : notes.length ? (
                  <div className="annotation-list">
                    {notes.map((annotation) => (
                      <article className="annotation-card" key={annotation.id}>
                        <div>
                          <span className="annotation-kind">
                            {annotation.kind ?? "comment"}
                          </span>
                          <time>
                            {formatDate(
                              annotation.updated_at ?? annotation.created_at,
                            )}
                          </time>
                        </div>
                        {annotation.title && <h3>{annotation.title}</h3>}
                        <p>{annotationBody(annotation) || "空批注"}</p>
                      </article>
                    ))}
                  </div>
                ) : (
                  <EmptyState
                    title="还没有批注"
                    detail="在下方记下要点，搜索结果会显示约 60 个中文字符的批注摘要。"
                  />
                )}
                <AnnotationComposer assetId={data.id} />
              </section>
            </div>

            <aside className="detail-sidebar" aria-label="资产元数据与探索">
              <section className="section-block">
                <div className="section-heading">
                  <div>
                    <p className="eyebrow">文件</p>
                    <h2>可用表示</h2>
                  </div>
                </div>
                {representations.isPending ? (
                  <LoadingState />
                ) : representations.isError ? (
                  <ErrorState
                    error={representations.error}
                    retry={() => void representations.refetch()}
                  />
                ) : reps.length ? (
                  <div className="representation-list">
                    {reps.map((representation) => (
                      <div key={representation.id}>
                        <span>
                          <strong>{representationLabel(representation)}</strong>
                          <small>
                            {[
                              representation.role,
                              formatBytes(representation.size_bytes),
                            ]
                              .filter(Boolean)
                              .join(" · ") || "本地资产"}
                          </small>
                        </span>
                        <span className="state-pill">
                          {representation.availability ?? "available"}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <EmptyState
                    title="暂无文件"
                    detail="该资产没有已注册的本地或外部表示。"
                  />
                )}
              </section>

              <section className="section-block detail-links">
                <h2>探索</h2>
                <button type="button" onClick={() => setActiveTab("relations")}>
                  <Network size={17} aria-hidden="true" />
                  查看关系
                </button>
                <Link
                  to={`/chat?asset_id=${encodeURIComponent(data.id)}`}
                >
                  <BotMessageSquare size={17} aria-hidden="true" />
                  基于此资产提问
                </Link>
                <Link to="#annotations">
                  <StickyNote size={17} aria-hidden="true" />
                  添加批注
                </Link>
              </section>

              <section className="section-block metadata-list">
                <h2>元数据</h2>
                <dl>
                  <div>
                    <dt>
                      <CalendarDays size={14} aria-hidden="true" /> 年份
                    </dt>
                    <dd>{data.year ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>语言</dt>
                    <dd>{data.language ?? "—"}</dd>
                  </div>
                  <div>
                    <dt>更新时间</dt>
                    <dd>{formatDate(data.updated_at)}</dd>
                  </div>
                  <div>
                    <dt>资产 ID</dt>
                    <dd className="mono">{data.id}</dd>
                  </div>
                </dl>
              </section>
            </aside>
          </div>
        )}

        {activeTab === "pdf" && (
          <Suspense fallback={<LoadingState label="正在加载 PDF 阅读器…" />}>
            <PdfPanel representation={pdf} />
          </Suspense>
        )}
        {activeTab === "code" && <CodePanel representation={code} />}
        {activeTab === "guide" && <GuidePanel representation={guide} />}
        {activeTab === "html" && <HtmlPanel representation={html} />}

        {activeTab === "relations" &&
          (relations.isPending ? (
            <LoadingState label="正在加载关系…" />
          ) : relations.isError ? (
            <ErrorState
              error={relations.error}
              retry={() => void relations.refetch()}
            />
          ) : related.nodes.length ? (
            <section className="section-block relation-summary">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">一跳邻域</p>
                  <h2>关系资产</h2>
                </div>
                <Link
                  className="button secondary compact"
                  to={`/graph?asset_id=${encodeURIComponent(data.id)}`}
                >
                  打开完整图谱
                </Link>
              </div>
              <p className="muted">
                {related.nodes.length} 个节点 · {related.edges.length} 条关系
              </p>
              <div className="relation-node-grid">
                {related.nodes
                  .filter((node) => node.id !== data.id)
                  .map((node) => (
                    <Link
                      to={`/assets/${encodeURIComponent(node.id)}`}
                      key={node.id}
                    >
                      <strong>{node.title ?? node.label ?? node.id}</strong>
                      <span>
                        {[node.asset_type, node.year].filter(Boolean).join(" · ") ||
                          "资产"}
                      </span>
                    </Link>
                  ))}
              </div>
            </section>
          ) : (
            <EmptyState
              title="该资产暂无关系"
              detail="关系候选确认后会在此处展示；也可以打开完整图谱继续探索。"
              kind="index"
              action={
                <Link
                  className="button secondary"
                  to={`/graph?asset_id=${encodeURIComponent(data.id)}`}
                >
                  打开关系图谱
                </Link>
              }
            />
          ))}

        {activeTab === "similar" &&
          (similar.isPending ? (
            <LoadingState label="正在计算相似论文…" />
          ) : similar.isError ? (
            <EmptyState
              title="相似度索引暂不可用"
              detail="这不影响资产详情、文件打开与批注功能。"
              kind="index"
            />
          ) : similar.data.items.length ? (
            <section className="section-block">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">语义相似</p>
                  <h2>相似论文</h2>
                </div>
              </div>
              <div className="asset-list">
                {similar.data.items.map((item) => (
                  <AssetCard asset={item} key={item.id} />
                ))}
              </div>
            </section>
          ) : (
            <EmptyState
              title="暂无相似论文"
              detail="向量索引建立后会给出延伸阅读建议。"
            />
          ))}
      </div>
    </div>
  );
}
