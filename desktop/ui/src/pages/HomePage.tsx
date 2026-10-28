import { useQuery } from "@tanstack/react-query";
import {
  ArrowRight,
  BookOpen,
  Clock3,
  LibraryBig,
  Search,
  StickyNote,
} from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Dashboard } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import {
  EmptyState,
  ErrorState,
  IndexNotice,
  LoadingState,
} from "../components/PageState";

function count(
  dashboard: Dashboard,
  key: keyof NonNullable<Dashboard["stats"]>,
) {
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
  const continueReading = data?.continue_reading ?? [];
  const continueIds = new Set(continueReading.map((asset) => asset.id));
  const recentAssets = (data?.recent_assets ?? []).filter(
    (asset) => !continueIds.has(asset.id),
  );
  const index = data?.index ?? data?.index_state;

  return (
    <div className="page library-home">
      <section className="library-hero" aria-labelledby="library-heading">
        <p className="eyebrow">本地科研资料库</p>
        <h1 id="library-heading">今天想研究什么？</h1>
        <p>在论文、代码和个人笔记中快速找到线索，然后回到上次阅读的位置。</p>

        <form className="library-search" onSubmit={submit} role="search">
          <Search size={22} strokeWidth={1.8} aria-hidden="true" />
          <label className="sr-only" htmlFor="library-search">
            搜索论文、代码或笔记
          </label>
          <input
            id="library-search"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索论文、代码或笔记"
          />
          <kbd>Ctrl K</kbd>
          <button
            className="button primary library-search-submit"
            type="submit"
          >
            搜索
            <ArrowRight size={16} aria-hidden="true" />
          </button>
        </form>
      </section>

      <div className="home-product-actions">
        <Link className="button primary" to="/intake">
          ＋ 添加论文
        </Link>
        <Link className="button secondary" to="/collections">
          收藏集合
        </Link>
      </div>
      {dashboard.isPending ? (
        <LoadingState label="正在整理你的资料库…" />
      ) : dashboard.isError ? (
        <ErrorState
          error={dashboard.error}
          retry={() => void dashboard.refetch()}
        />
      ) : data ? (
        <>
          <IndexNotice status={index?.status} detail={index?.detail} />

          <div className="library-home-grid">
            <div className="library-main-column">
              {continueReading.length > 0 && (
                <section
                  className="library-section"
                  aria-labelledby="continue-reading-heading"
                >
                  <div className="library-section-heading">
                    <div>
                      <span className="section-icon" aria-hidden="true">
                        <BookOpen size={17} />
                      </span>
                      <div>
                        <p className="eyebrow">继续工作</p>
                        <h2 id="continue-reading-heading">继续阅读</h2>
                      </div>
                    </div>
                    <Link className="text-link" to="/search?status=reading">
                      查看全部 <ArrowRight size={14} aria-hidden="true" />
                    </Link>
                  </div>
                  <div className="continue-reading-grid">
                    {continueReading.slice(0, 2).map((asset) => (
                      <AssetCard asset={asset} compact continueReading key={asset.id} />
                    ))}
                  </div>
                </section>
              )}

              <section
                className="library-section"
                aria-labelledby="recent-assets-heading"
              >
                <div className="library-section-heading">
                  <div>
                    <span
                      className="section-icon is-neutral"
                      aria-hidden="true"
                    >
                      <Clock3 size={17} />
                    </span>
                    <div>
                      <p className="eyebrow">资料动态</p>
                      <h2 id="recent-assets-heading">最近资料</h2>
                    </div>
                  </div>
                  <Link className="text-link" to="/search">
                    浏览资料库 <ArrowRight size={14} aria-hidden="true" />
                  </Link>
                </div>

                {recentAssets.length ? (
                  <div className="asset-list library-asset-list">
                    {recentAssets.slice(0, 5).map((asset) => (
                      <AssetCard asset={asset} compact key={asset.id} />
                    ))}
                  </div>
                ) : (
                  <EmptyState
                    title={
                      continueReading.length
                        ? "没有其他最近资料"
                        : "还没有可展示的资料"
                    }
                    detail={
                      continueReading.length
                        ? "正在阅读的资料已放在上方，这里不再重复。"
                        : "点击添加论文，上传 PDF 并确认题名即可开始使用。"
                    }
                    kind="index"
                    action={
                      <Link className="button secondary" to="/jobs">
                        查看导入状态
                      </Link>
                    }
                  />
                )}
              </section>
            </div>

            <aside className="library-side-column" aria-label="资料库概览">
              <section className="library-overview-card">
                <div className="library-overview-title">
                  <span className="section-icon" aria-hidden="true">
                    <LibraryBig size={17} />
                  </span>
                  <div>
                    <p className="eyebrow">你的资料库</p>
                    <h2>研究概览</h2>
                  </div>
                </div>
                <dl className="library-metrics">
                  <div>
                    <dt>论文</dt>
                    <dd>{count(data, "papers").toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>阅读中</dt>
                    <dd>{count(data, "reading").toLocaleString()}</dd>
                  </div>
                  <div>
                    <dt>笔记</dt>
                    <dd>{count(data, "annotations").toLocaleString()}</dd>
                  </div>
                </dl>
              </section>

              <section className="library-domains-card">
                <div className="library-overview-title">
                  <span className="section-icon is-amber" aria-hidden="true">
                    <StickyNote size={17} />
                  </span>
                  <div>
                    <p className="eyebrow">研究方向</p>
                    <h2>常用领域</h2>
                  </div>
                </div>
                <nav aria-label="常用领域">
                  {data.domains?.slice(0, 5).map((domain) => (
                    <Link
                      to={`/domains/${encodeURIComponent(domain.slug)}`}
                      key={domain.slug}
                    >
                      <span>{domain.label ?? domain.name ?? domain.slug}</span>
                      <small>{domain.paper_count ?? 0} 篇</small>
                      <ArrowRight size={14} aria-hidden="true" />
                    </Link>
                  ))}
                </nav>
                <Link className="library-card-footer" to="/domains">
                  查看全部领域 <ArrowRight size={14} aria-hidden="true" />
                </Link>
              </section>
            </aside>
          </div>
        </>
      ) : null}
    </div>
  );
}
