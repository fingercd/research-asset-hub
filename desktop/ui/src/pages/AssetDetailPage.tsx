import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  BotMessageSquare,
  CalendarDays,
  Code2,
  FileText,
  GitCompareArrows,
  Quote,
  ScrollText,
  Sparkles,
  StickyNote,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import {
  getAssetDomains,
  getReadingStatus,
  getRepresentationType,
  type Representation,
} from "../api/types";
import { AssetCollectionControl } from "../components/AssetCollectionControl";
import { AssetMetadataEditor } from "../components/AssetMetadataEditor";
import { CodeLinkControl } from "../components/CodeLinkControl";
import { AnnotationComposer } from "../components/AnnotationComposer";
import { AssetCard } from "../components/AssetCard";
import { CodePanel, GuidePanel, HtmlPanel } from "../components/DocumentPanels";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { ReadingStatusControl } from "../components/ReadingStatusControl";
import { ReleaseStatusBadges } from "../components/ReleaseStatusBadges";
import { RepresentationActions } from "../components/RepresentationActions";
import { ResearchUnitActions } from "../components/ResearchUnitActions";
import {
  annotationBody,
  formatAuthors,
  formatBytes,
  formatDate,
  representationLabel,
} from "../lib/format";
import { preferGuide } from "../lib/preferredGuide";
import { pdfRepresentationLabel } from "../lib/pdfRepresentationLabel";
import "../styles/reading-workspace.css";
import { useCompare } from "../state/CompareContext";

const PdfPanel = lazy(() =>
  import("../components/PdfPanel").then((module) => ({
    default: module.PdfPanel,
  })),
);

type DetailTab = "overview" | "pdf" | "code" | "guide" | "similar" | "html";

const tabs: Array<{
  id: DetailTab;
  label: string;
  icon: typeof FileText;
}> = [
  { id: "overview", label: "概览与批注", icon: ScrollText },
  { id: "pdf", label: "PDF", icon: FileText },
  { id: "code", label: "代码", icon: Code2 },
  { id: "guide", label: "导读", icon: ScrollText },
  { id: "similar", label: "相似论文", icon: Sparkles },
  { id: "html", label: "原始 HTML", icon: BotMessageSquare },
];

function firstRepresentation(
  representations: Representation[],
  predicate: (item: Representation) => boolean,
) {
  return representations.find(
    (item) => (item.availability === undefined || item.availability === "available") && predicate(item),
  );
}

export function AssetDetailPage() {
  const { assetId = "" } = useParams();
  const compare = useCompare();
  const [editingMetadata, setEditingMetadata] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const pageParam = Number(searchParams.get("page"));
  const requestedPage = Number.isInteger(pageParam) && pageParam > 0 ? pageParam : undefined;
  const requestedRepresentation = searchParams.get("representation");
  const activeTab: DetailTab = tabs.find((tab) => tab.id === searchParams.get("view"))?.id ?? "overview";
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
  const readingPosition = useQuery({
    queryKey: ["reading-position", assetId],
    queryFn: ({ signal }) => api.readingPosition(assetId, signal),
    enabled: Boolean(assetId) && activeTab === "pdf",
    retry: false,
    staleTime: 30_000,
    refetchOnWindowFocus: false,
  });
  const annotations = useInfiniteQuery({
    queryKey: ["annotations", assetId],
    queryFn: ({ signal, pageParam }) => api.annotations(assetId, signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: Boolean(assetId),
  });
  const similar = useQuery({
    queryKey: ["similar", assetId],
    queryFn: ({ signal }) => api.similar(assetId, signal),
    enabled: Boolean(assetId) && activeTab === "similar",
    retry: false,
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
  const notes = annotations.data?.pages.flatMap((page) => page.items) ?? data.annotations ?? [];
  const domains = getAssetDomains(data);
  const selected = compare.has(data.id);
  const pdfs = reps.filter((item) => getRepresentationType(item) === "pdf");
  const savedRepresentation = readingPosition.data?.position?.representation_id;
  const pdf = requestedRepresentation
    ? firstRepresentation(pdfs, (item) => item.id === requestedRepresentation)
    : firstRepresentation(pdfs, (item) => item.id === savedRepresentation)
      ?? firstRepresentation(pdfs, () => true);
  const code =
    firstRepresentation(reps, (item) =>
      ["repository", "source_code", "notebook"].includes(
        getRepresentationType(item),
      ),
    ) ??
    (() => {
      const action = data.quick_actions?.find(
        (item) => item.kind === "code" && item.status === "available",
      );
      return action?.representation_id
        ? {
            id: action.representation_id,
            asset_id: action.owner_asset_id ?? undefined,
            representation_type: "repository",
            availability: "available",
          }
        : undefined;
    })();
  const guide = preferGuide(reps);
  const html = firstRepresentation(
    reps,
    (item) =>
      ["html", "html_document"].includes(getRepresentationType(item)) &&
      item.role !== "guide",
  );
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
          <ReleaseStatusBadges
            asset={data}
            quickActions={data.quick_actions}
            relatedRepositories={data.related_repositories}
          />
          {(!!domains.length || !!data.tags?.length) && (
            <details className="detail-topics">
              <summary>领域与标签（{domains.length + (data.tags?.length ?? 0)}）</summary>
            <div className="tag-row">
              {domains.map((domain) => (
                <Link
                  to={`/domains/${encodeURIComponent(domain)}`}
                  key={domain}
                >
                  {domain}
                </Link>
              ))}
              {data.tags?.map((tag) => (
                <span key={tag}>#{tag}</span>
              ))}
            </div>
            </details>
          )}
        </div>
        <div className="asset-hero-actions">
          {(data.asset_type ?? data.type) === "paper" && (
            <button className="button secondary" type="button"
              onClick={() => setEditingMetadata(true)}>编辑元数据</button>
          )}
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
        {data.quick_actions?.length ? (
          <ResearchUnitActions
            actions={data.quick_actions}
            ownerAssetId={data.id}
            repositoryLabel={
              data.related_repositories?.[0]?.title ?? data.title
            }
          />
        ) : (
          <RepresentationActions representations={reps} />
        )}
      </header>

      {editingMetadata && (
        <AssetMetadataEditor key={data.id} asset={data}
          onClose={() => setEditingMetadata(false)} />
      )}

      <AssetCollectionControl assetId={data.id} />
      <div className="asset-tabs" role="tablist" aria-label="资产阅读视图">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            id={`detail-tab-${id}`}
            className={activeTab === id ? "is-active" : ""}
            type="button"
            role="tab"
            aria-selected={activeTab === id}
            aria-controls={`detail-panel-${id}`}
            onClick={() => setSearchParams({ view: id })}
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
                  <span className="count-badge">已加载 {notes.length}</span>
                </div>
                {annotations.isPending ? (
                  <LoadingState label="正在加载批注…" />
                ) : annotations.isError && !annotations.data ? (
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
                        {annotation.anchor?.quote &&
                          annotation.anchor.quote !==
                            annotationBody(annotation) && (
                            <blockquote>{annotation.anchor.quote}</blockquote>
                          )}
                        <p>{annotationBody(annotation) || "空批注"}</p>
                        {annotation.anchor?.page && (
                          <button
                            type="button"
                            className="text-link"
                            onClick={() => {
                              setSearchParams({
                                view: "pdf",
                                page: String(annotation.anchor!.page),
                                ...(annotation.representation_id
                                  ? {
                                      representation:
                                        annotation.representation_id,
                                    }
                                  : {}),
                              });
                            }}
                          >
                            回到 PDF 第 {annotation.anchor.page} 页
                          </button>
                        )}
                      </article>
                    ))}
                  </div>
                ) : (
                  <EmptyState
                    title="还没有批注"
                    detail="在下方记下要点，搜索结果会显示约 60 个中文字符的批注摘要。"
                  />
                )}
                {annotations.isFetchNextPageError && (
                  <p className="inline-error" role="alert">后续批注加载失败，请重试。</p>
                )}
                {annotations.hasNextPage && (
                  <button className="button secondary compact" type="button"
                    disabled={annotations.isFetchingNextPage}
                    onClick={() => void annotations.fetchNextPage()}>
                    {annotations.isFetchingNextPage ? "加载中…" : annotations.isFetchNextPageError ? "重试加载更多批注" : "加载更多批注"}
                  </button>
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
            {pdfs.length > 1 && (
              <label className="pdf-representation-picker">
                <span>选择 PDF 版本</span>
                <select
                  aria-label="选择 PDF 版本"
                  value={pdf?.id ?? ""}
                  onChange={(event) =>
                    setSearchParams({
                      view: "pdf",
                      representation: event.target.value,
                    })
                  }
                >
                  {pdfs
                    .map((item) => (
                      <option key={item.id} value={item.id} disabled={item.availability !== undefined && item.availability !== "available"}>
                        {pdfRepresentationLabel(item)}{item.availability === "missing" ? "（文件缺失）" : item.availability && item.availability !== "available" ? "（不可用）" : ""}
                      </option>
                    ))}
                </select>
              </label>
            )}
            {requestedRepresentation && !pdf && (
              <p className="inline-warning">
                这条批注所对应的 PDF 当前不可用。可在概览中核对原文件与版本。
              </p>
            )}
            {!requestedRepresentation && savedRepresentation && pdf?.id !== savedRepresentation && !readingPosition.isPending && (
              <p className="inline-warning">上次阅读的 PDF 版本当前不可用。{pdf ? "已选择另一可用版本，将从第一页开始，不会套用其他文件的页码。" : "请在概览中核对文件与版本。"}</p>
            )}
            {readingPosition.isPending ? <LoadingState label="正在恢复上次阅读版本…" /> : <PdfPanel
              key={`${data.id}:${pdf?.id ?? "none"}`}
              representation={pdf}
              assetId={data.id}
              initialPage={requestedPage}
              annotations={notes}
            />}
          </Suspense>
        )}
        {activeTab === "code" && (
          <>
            <CodeLinkControl
              paperId={data.id}
              repositoryId={
                code?.asset_id ??
                (data.asset_type === "repository" ? data.id : undefined)
              }
            />
            <CodePanel representation={code} />
          </>
        )}
        {activeTab === "guide" && <GuidePanel representation={guide} />}
        {activeTab === "html" && <HtmlPanel representation={html} />}

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
                  <p className="eyebrow">相似论文</p>
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
              detail="关键词索引建立后会给出延伸阅读建议。"
            />
          ))}
      </div>
    </div>
  );
}
