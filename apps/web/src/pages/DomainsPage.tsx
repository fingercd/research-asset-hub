import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, BookOpen, FolderKanban } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api/client";
import { AssetCard } from "../components/AssetCard";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";

export function DomainsPage() {
  const query = useQuery({
    queryKey: ["domains"],
    queryFn: ({ signal }) => api.domains(signal),
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="动态目录"
        title="研究领域"
        description="领域由来源配置动态生成；新增领域后会自动出现在这里。"
      />
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
                <p className="eyebrow">{domain.slug}</p>
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
                  <dt>资产</dt>
                  <dd>{domain.asset_count ?? 0}</dd>
                </div>
                <div>
                  <dt>论文</dt>
                  <dd>{domain.paper_count ?? 0}</dd>
                </div>
                <div>
                  <dt>阅读中</dt>
                  <dd>{domain.reading_counts?.reading ?? 0}</dd>
                </div>
              </dl>
              <Link className="text-link" to={`/domains/${encodeURIComponent(domain.slug)}`}>
                查看领域 <ArrowRight size={14} aria-hidden="true" />
              </Link>
            </article>
          ))}
        </section>
      ) : (
        <EmptyState
          title="尚未注册领域"
          detail="在来源配置中启用领域并完成导入后，此处会显示动态目录。"
          kind="index"
        />
      )}
    </div>
  );
}

export function DomainDetailPage() {
  const { slug = "" } = useParams();
  const domain = useQuery({
    queryKey: ["domain", slug],
    queryFn: ({ signal }) => api.domain(slug, signal),
    enabled: Boolean(slug),
  });
  const assets = useQuery({
    queryKey: ["assets", { domain: slug }],
    queryFn: ({ signal }) => api.assets({ domain: slug }, signal),
    enabled: Boolean(slug),
  });

  if (domain.isPending) return <div className="page"><LoadingState label="正在加载领域…" /></div>;
  if (domain.isError) {
    return (
      <div className="page">
        <ErrorState error={domain.error} retry={() => void domain.refetch()} />
      </div>
    );
  }

  const data = domain.data;
  const items = data.recent_assets?.length ? data.recent_assets : assets.data?.items ?? [];

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
            搜索此领域
          </Link>
        }
      />
      <section className="domain-hero-stats" aria-label="领域概况">
        <div>
          <FolderKanban size={18} aria-hidden="true" />
          <span>资产</span>
          <strong>{data.asset_count ?? items.length}</strong>
        </div>
        <div>
          <BookOpen size={18} aria-hidden="true" />
          <span>阅读中</span>
          <strong>{data.reading_counts?.reading ?? 0}</strong>
        </div>
        <div>
          <span>已读</span>
          <strong>{data.reading_counts?.read ?? 0}</strong>
        </div>
      </section>
      <section className="section-block">
        <div className="section-heading">
          <div>
            <p className="eyebrow">资产目录</p>
            <h2>领域内容</h2>
          </div>
        </div>
        {assets.isPending && !data.recent_assets?.length ? (
          <LoadingState />
        ) : assets.isError && !data.recent_assets?.length ? (
          <ErrorState error={assets.error} retry={() => void assets.refetch()} />
        ) : items.length ? (
          <div className="asset-list">
            {items.map((asset) => (
              <AssetCard asset={asset} key={asset.id} />
            ))}
          </div>
        ) : (
          <EmptyState
            title="这个领域还没有资产"
            detail="完成对应来源的导入后，论文和研究材料会显示在这里。"
          />
        )}
      </section>
    </div>
  );
}
