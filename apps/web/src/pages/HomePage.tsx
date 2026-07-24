import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  BookOpen,
  LibraryBig,
  Network,
  Search,
  StickyNote,
} from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { Dashboard } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { EmptyState, ErrorState, IndexNotice, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";

function count(dashboard: Dashboard, key: keyof NonNullable<Dashboard["stats"]>) {
  const direct: Partial<Record<typeof key, number | undefined>> = {
    assets: dashboard.total_assets,
    papers: dashboard.total_papers,
    unread: dashboard.unread_count,
    reading: dashboard.reading_count,
    read: dashboard.read_count,
    domains: dashboard.domain_count,
    relations: dashboard.relation_count,
    annotations: dashboard.annotation_count,
  };
  return dashboard.stats?.[key] ?? direct[key] ?? 0;
}

export function HomePage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: ({ signal }) => api.dashboard(signal),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = query.trim();
    navigate(value ? `/search?q=${encodeURIComponent(value)}` : "/search");
  };

  const data = dashboard.data;
  const recent = data?.continue_reading?.length
    ? data.continue_reading
    : (data?.recent_assets ?? []);
  const index = data?.index ?? data?.index_state;

  return (
    <div className="page">
      <PageHeader
        eyebrow="本地研究工作台"
        title="你的科研资产，一处整理"
        description="检索论文、代码和导读，跟踪阅读进度，并沿关系图谱继续探索。"
      />

      <form className="home-search" onSubmit={submit} role="search">
        <Search size={20} aria-hidden="true" />
        <label className="sr-only" htmlFor="home-search">
          搜索全部科研资产
        </label>
        <input
          id="home-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索论文、方法、代码、导读或批注…"
        />
        <button className="button primary" type="submit">
          搜索
        </button>
        <kbd>Ctrl K</kbd>
      </form>

      {dashboard.isPending ? (
        <LoadingState label="正在汇总资产…" />
      ) : dashboard.isError ? (
        <ErrorState error={dashboard.error} retry={() => void dashboard.refetch()} />
      ) : data ? (
        <>
          <IndexNotice status={index?.status} detail={index?.detail} />
          <section className="metric-grid" aria-label="资产概况">
            <article>
              <span className="metric-icon">
                <LibraryBig size={18} aria-hidden="true" />
              </span>
              <div>
                <span>全部资产</span>
                <strong>{count(data, "assets").toLocaleString()}</strong>
                <small>{count(data, "papers")} 篇论文</small>
              </div>
            </article>
            <article>
              <span className="metric-icon">
                <BookOpen size={18} aria-hidden="true" />
              </span>
              <div>
                <span>阅读中</span>
                <strong>{count(data, "reading").toLocaleString()}</strong>
                <small>{count(data, "unread")} 篇未读</small>
              </div>
            </article>
            <article>
              <span className="metric-icon">
                <Network size={18} aria-hidden="true" />
              </span>
              <div>
                <span>知识关系</span>
                <strong>{count(data, "relations").toLocaleString()}</strong>
                <small>{count(data, "domains")} 个领域</small>
              </div>
            </article>
            <article>
              <span className="metric-icon">
                <StickyNote size={18} aria-hidden="true" />
              </span>
              <div>
                <span>个人批注</span>
                <strong>{count(data, "annotations").toLocaleString()}</strong>
                <small>{count(data, "read")} 篇已读</small>
              </div>
            </article>
          </section>

          <div className="dashboard-grid">
            <section className="section-block">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">继续工作</p>
                  <h2>{data.continue_reading?.length ? "继续阅读" : "最近资产"}</h2>
                </div>
                <Link className="text-link" to="/search">
                  查看全部 <ArrowRight size={14} aria-hidden="true" />
                </Link>
              </div>
              {recent.length ? (
                <div className="asset-list">
                  {recent.slice(0, 4).map((asset) => (
                    <AssetCard asset={asset} compact key={asset.id} />
                  ))}
                </div>
              ) : (
                <EmptyState
                  title="还没有可展示的资产"
                  detail="完成首次只读导入后，最近论文与阅读进度会出现在这里。"
                  kind="index"
                  action={
                    <Link className="button secondary" to="/jobs">
                      查看导入任务
                    </Link>
                  }
                />
              )}
            </section>

            <aside className="section-block domain-overview">
              <div className="section-heading">
                <div>
                  <p className="eyebrow">领域</p>
                  <h2>研究版图</h2>
                </div>
                <Link className="text-link" to="/domains">
                  管理领域
                </Link>
              </div>
              {data.domains?.length ? (
                <div className="domain-mini-list">
                  {data.domains.slice(0, 6).map((domain) => (
                    <Link to={`/domains/${encodeURIComponent(domain.slug)}`} key={domain.slug}>
                      <span>{domain.label ?? domain.name ?? domain.slug}</span>
                      <strong>{domain.asset_count ?? 0}</strong>
                    </Link>
                  ))}
                </div>
              ) : (
                <EmptyState
                  title="尚未连接领域"
                  detail="领域会随已注册来源动态出现，无需修改前端。"
                />
              )}
            </aside>
          </div>
        </>
      ) : null}
    </div>
  );
}
