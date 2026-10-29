import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  BarChart3,
  BookOpen,
  Clock3,
  FolderKanban,
  LayoutDashboard,
  LibraryBig,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { DomainEditor } from "../components/DomainEditor";
import { PackageActions } from "../components/PackageActions";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import { getAssetDomains } from "../api/types";
import { DomainOverviewPanel } from "../components/DomainOverviewPanel";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { ResearchUnitCard } from "../components/ResearchUnitCard";

const DomainPortalArticle = lazy(() =>
  import("../components/domain-portal/DomainPortalArticle").then((module) => ({
    default: module.DomainPortalArticle,
  })),
);

export function DomainsPage() {
  const [editing, setEditing] = useState(false);
  const query = useQuery({
    queryKey: ["domains"],
    queryFn: ({ signal }) => api.domains(signal),
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="动态目录"
        title="研究领域"
        description="按研究方向组织资料。创建领域后即可添加 PDF，逐步建立阅读目录。"
        actions={
          <button
            className="button primary"
            type="button"
            onClick={() => setEditing(true)}
          >
            新建领域
          </button>
        }
      />
      <PackageActions />
      {editing && <DomainEditor onClose={() => setEditing(false)} />}
      {query.isPending ? (
        <LoadingState label="正在加载领域…" />
      ) : query.isError ? (
        <ErrorState error={query.error} retry={() => void query.refetch()} />
      ) : query.data.items.length ? (
        <section className="domain-grid" aria-label="研究领域">
          {query.data.items.map((domain) => (
            <article className="domain-card" key={domain.slug}>
              <div className="domain-card-icon" aria-hidden="true">
                <FolderKanban size={21} />
              </div>
              <div>
                <div className="domain-card-kicker">
                  <p className="eyebrow">{domain.slug}</p>
                  <span
                    className={`overview-status is-${domain.overview_status ?? "unavailable"}`}
                  >
                    {domain.overview_status === "available"
                      ? "总览可用"
                      : domain.overview_status === "network_required"
                        ? "总览需联网"
                        : domain.overview_status === "missing"
                          ? "总览缺失"
                          : "暂无总览"}
                  </span>
                </div>
                <h2>{domain.label ?? domain.name ?? domain.slug}</h2>
                <p>{domain.description || "暂无领域说明。"}</p>
              </div>
              {!!domain.tags?.length && (
                <div className="tag-row">
                  {domain.tags.slice(0, 4).map((tag) => (
                    <span key={tag}>{tag}</span>
                  ))}
                </div>
              )}
              <dl className="domain-counts">
                <div>
                  <dt>论文</dt>
                  <dd>{domain.paper_count ?? 0}</dd>
                </div>
                <div>
                  <dt>仓库</dt>
                  <dd>{domain.repository_count ?? 0}</dd>
                </div>
                <div>
                  <dt>研究单元</dt>
                  <dd>
                    {domain.research_unit_count ?? domain.paper_count ?? 0}
                  </dd>
                </div>
                <div>
                  <dt>阅读中</dt>
                  <dd>{domain.reading_counts?.reading ?? 0}</dd>
                </div>
              </dl>
              <Link
                className="text-link"
                to={`/domains/${encodeURIComponent(domain.slug)}`}
              >
                查看领域 <ArrowRight size={14} aria-hidden="true" />
              </Link>
            </article>
          ))}
        </section>
      ) : (
        <EmptyState
          title="尚未注册领域"
          detail="点击新建领域，再添加第一篇 PDF。"
          kind="index"
        />
      )}
    </div>
  );
}

export function DomainDetailPage() {
  const { slug = "" } = useParams();
  const [editing, setEditing] = useState(false);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedView = searchParams.get("view");
  const activeTab: "overview" | "units" | "recent" | "stats" =
    requestedView === "units" ||
    requestedView === "recent" ||
    requestedView === "stats"
      ? requestedView
      : "overview";
  const setActiveTab = (view: "overview" | "units" | "recent" | "stats") => {
    const next = new URLSearchParams(searchParams);
    if (view === "overview") next.delete("view");
    else next.set("view", view);
    setSearchParams(next, { replace: true });
  };
  const metadata = useQuery({
    queryKey: ["domain", slug],
    queryFn: ({ signal }) => api.domain(slug, signal),
    enabled: Boolean(slug),
  });
  const domain = useQuery({
    queryKey: ["domain-portal", slug],
    queryFn: ({ signal }) => api.domainPortal(slug, signal),
    enabled: Boolean(slug) && activeTab === "overview",
  });
  const unitsQuery = useInfiniteQuery({
    queryKey: ["search", "domain", slug, "paper"],
    queryFn: ({ pageParam, signal }) =>
      api.search(
        {
          q: "",
          mode: "keyword",
          group_by: "paper",
          domain: slug,
          include_facets: false,
          cursor: pageParam,
        },
        signal,
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: Boolean(slug) && activeTab === "units",
  });

  if (metadata.isPending && !domain.data)
    return (
      <div className="page">
        <LoadingState label="正在加载领域…" />
      </div>
    );
  if (metadata.isError && !domain.data) {
    return (
      <div className="page">
        <ErrorState
          error={metadata.error}
          retry={() => void metadata.refetch()}
        />
      </div>
    );
  }

  const data = { ...domain.data?.domain, ...metadata.data } as NonNullable<
    typeof metadata.data
  >;
  const portal = domain.data?.portal;
  const units = unitsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const coveredAssetIds = new Set(
    units.flatMap((unit) =>
      [unit.primary_asset, ...unit.related_repositories]
        .filter((asset) => getAssetDomains(asset).includes(slug))
        .map((asset) => asset.id),
    ),
  );
  const coveredAssets = coveredAssetIds.size;
  const domainAssetCount = data.asset_count ?? coveredAssets;
  const domainName = data.label ?? data.name ?? data.slug;
  const unitsSection = (
    <section className="section-block" aria-labelledby="domain-units-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">聚合目录</p>
          <h2 id="domain-units-heading">领域研究单元</h2>
        </div>
        {!unitsQuery.isPending && (
          <p className="loaded-unit-summary" aria-live="polite">
            已加载 <strong>{units.length}</strong> 篇论文 · 关联覆盖{" "}
            <strong>{coveredAssets}</strong> / {domainAssetCount} 项底层资产
          </p>
        )}
      </div>
      {unitsQuery.isPending ? (
        <LoadingState label="正在加载领域研究单元…" />
      ) : unitsQuery.isError ? (
        <ErrorState
          error={unitsQuery.error}
          retry={() => void unitsQuery.refetch()}
        />
      ) : units.length ? (
        <>
          <div className="asset-list">
            {units.map((unit) => (
              <ResearchUnitCard unit={unit} key={unit.unit_id} />
            ))}
          </div>
          {unitsQuery.hasNextPage && (
            <div className="load-more">
              <button
                className="button secondary"
                type="button"
                disabled={unitsQuery.isFetchingNextPage}
                onClick={() => void unitsQuery.fetchNextPage()}
              >
                {unitsQuery.isFetchingNextPage ? "正在加载…" : "加载更多"}
              </button>
            </div>
          )}
        </>
      ) : (
        <EmptyState
          title="这个领域还没有研究单元"
          detail="完成对应来源的导入后，论文及其关联代码会显示在这里。"
        />
      )}
    </section>
  );
  const recentSection = (
    <section className="section-block" aria-labelledby="domain-recent-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">快速回到最新资料</p>
          <h2 id="domain-recent-heading">最近更新</h2>
        </div>
        <p className="muted">展示最近 5 项；完整内容位于研究单元目录。</p>
      </div>
      {data.recent_assets?.length ? (
        <div className="domain-mini-list">
          {data.recent_assets.slice(0, 5).map((asset) => (
            <Link to={`/assets/${encodeURIComponent(asset.id)}`} key={asset.id}>
              <span>{asset.title_zh || asset.title}</span>
              <span>
                {[asset.year, asset.asset_type ?? asset.type]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </Link>
          ))}
        </div>
      ) : (
        <EmptyState
          title="暂无最近更新"
          detail="该领域完成资料导入后，最近更新会显示在这里。"
        />
      )}
    </section>
  );
  const statsSection = (
    <section className="section-block" aria-labelledby="domain-stats-heading">
      <div className="section-heading">
        <div>
          <p className="eyebrow">动态聚合</p>
          <h2 id="domain-stats-heading">领域统计</h2>
        </div>
      </div>
      <dl className="domain-stat-grid">
        <div>
          <dt>论文（主口径）</dt>
          <dd>{data.paper_count ?? 0}</dd>
        </div>
        <div>
          <dt>代码仓库（单独统计）</dt>
          <dd>{data.repository_count ?? 0}</dd>
        </div>
        <div>
          <dt>研究单元（聚合视图）</dt>
          <dd>
            {data.research_unit_count ?? unitsQuery.data?.pages[0]?.total ?? 0}
          </dd>
        </div>
        <div>
          <dt>底层资产（技术统计）</dt>
          <dd>{domainAssetCount}</dd>
        </div>
        <div>
          <dt>未读</dt>
          <dd>{data.reading_counts?.unread ?? 0}</dd>
        </div>
        <div>
          <dt>阅读中</dt>
          <dd>{data.reading_counts?.reading ?? 0}</dd>
        </div>
        <div>
          <dt>已读</dt>
          <dd>{data.reading_counts?.read ?? 0}</dd>
        </div>
        <div>
          <dt>批注</dt>
          <dd>{data.annotation_count ?? 0}</dd>
        </div>
      </dl>
    </section>
  );

  if (portal && domain.data?.renderer === "v2") {
    return (
      <div className="page domain-portal-page">
        <Link className="back-link" to="/domains">
          <ArrowLeft size={15} aria-hidden="true" /> 返回领域
        </Link>
        <div className="domain-product-actions">
          <Link
            className="button primary compact"
            to={`/intake?domain=${encodeURIComponent(slug)}`}
          >
            添加论文
          </Link>
          <button
            className="button secondary compact"
            type="button"
            onClick={() => setEditing(true)}
          >
            编辑领域
          </button>
          <PackageActions domain={slug} />
        </div>
        {editing && (
          <DomainEditor domain={data} onClose={() => setEditing(false)} />
        )}
        <Suspense fallback={<LoadingState label="正在加载领域总览视图…" />}>
          <DomainPortalArticle
            portal={portal}
            domain={data}
            activeView={activeTab}
            onViewChange={setActiveTab}
            units={unitsSection}
            recent={recentSection}
            stats={statsSection}
          />
        </Suspense>
      </div>
    );
  }

  return (
    <div className="page">
      <Link className="back-link" to="/domains">
        <ArrowLeft size={15} aria-hidden="true" /> 返回领域
      </Link>
      <PageHeader
        eyebrow={data.slug}
        title={data.label ?? data.name ?? data.slug}
        description={data.description ?? "该领域暂无说明。"}
        actions={
          <Link
            className="button secondary"
            to={`/search?domain=${encodeURIComponent(data.slug)}`}
          >
            浏览或搜索此领域
          </Link>
        }
      />
      <div className="domain-product-actions">
        <Link
          className="button primary compact"
          to={`/intake?domain=${encodeURIComponent(slug)}`}
        >
          添加论文
        </Link>
        <button
          className="button secondary compact"
          type="button"
          onClick={() => setEditing(true)}
        >
          编辑领域
        </button>
        <PackageActions domain={slug} />
      </div>
      {editing && (
        <DomainEditor domain={data} onClose={() => setEditing(false)} />
      )}
      <section className="domain-hero-stats" aria-label="领域概况">
        <div>
          <BookOpen size={18} aria-hidden="true" />
          <span>论文</span>
          <strong>{data.paper_count ?? 0}</strong>
        </div>
        <div>
          <LibraryBig size={18} aria-hidden="true" />
          <span>仓库</span>
          <strong>{data.repository_count ?? 0}</strong>
        </div>
        <div>
          <FolderKanban size={18} aria-hidden="true" />
          <span>研究单元</span>
          <strong>
            {data.research_unit_count ?? unitsQuery.data?.pages[0]?.total ?? 0}
          </strong>
        </div>
        <div>
          <span>阅读中</span>
          <strong>{data.reading_counts?.reading ?? 0}</strong>
        </div>
      </section>
      <div className="domain-tabs" role="tablist" aria-label="领域内容">
        {[
          {
            id: "overview" as const,
            label: "领域总览",
            icon: LayoutDashboard,
          },
          { id: "units" as const, label: "研究单元", icon: FolderKanban },
          { id: "recent" as const, label: "最近更新", icon: Clock3 },
          { id: "stats" as const, label: "统计", icon: BarChart3 },
        ].map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              id={`domain-tab-${tab.id}`}
              className={activeTab === tab.id ? "is-active" : undefined}
              type="button"
              role="tab"
              aria-selected={activeTab === tab.id}
              aria-controls={`domain-panel-${tab.id}`}
              onClick={() => setActiveTab(tab.id)}
              key={tab.id}
            >
              <Icon size={15} aria-hidden="true" />
              {tab.label}
            </button>
          );
        })}
      </div>
      <div
        id={`domain-panel-${activeTab}`}
        className="domain-tab-panel"
        role="tabpanel"
        aria-labelledby={`domain-tab-${activeTab}`}
      >
        {activeTab === "overview" &&
          (domain.isPending ? (
            <LoadingState label="正在加载领域总览…" />
          ) : domain.isError ? (
            <ErrorState
              error={domain.error}
              retry={() => void domain.refetch()}
            />
          ) : (
            <DomainOverviewPanel
              overview={data.overview}
              domainName={domainName}
              fallback={
                <div className="section-block">
                  <h2>开始建立这个领域</h2>
                  <p>
                    {data.description ??
                      "添加论文后，研究单元与阅读统计会自动更新。"}
                  </p>
                  <Link
                    className="button secondary"
                    to={`/domains/${encodeURIComponent(slug)}?view=units`}
                  >
                    浏览研究单元
                  </Link>
                </div>
              }
            />
          ))}
        {activeTab === "units" && unitsSection}
        {activeTab === "recent" && recentSection}
        {activeTab === "stats" && statsSection}
      </div>
    </div>
  );
}
