import { useQuery } from "@tanstack/react-query";
import {
  ArrowUpRight,
  Braces,
  ExternalLink,
  Eye,
  EyeOff,
  FolderGit2,
  RefreshCw,
  Server,
  ServerOff,
} from "lucide-react";
import { Link } from "react-router-dom";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type {
  CodeGraphDomain,
  CodeGraphRepository,
} from "../api/types";
import { CodeGraphViewer } from "../components/CodeGraphViewer";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";

const GITNEXUS_FALLBACK_URL = "http://127.0.0.1:4747";
type ServiceStatus = "checking" | "online" | "offline";
type ViewerMode = "gitnexus" | "hub";

function serviceStatusLabel(status: ServiceStatus) {
  if (status === "online") return "服务在线";
  if (status === "checking") return "正在检查";
  return "服务未启动";
}

function RepositoryRow({
  repository,
  gitnexusUrl,
  selected,
  onSelect,
}: {
  repository: CodeGraphRepository;
  gitnexusUrl: string;
  selected: boolean;
  onSelect: (repository: CodeGraphRepository) => void;
}) {
  const title = repository.title_zh || repository.title;
  const primaryPath = repository.paths?.[0];
  const repoName = repository.gitnexus_repo_name ?? null;
  const projectUrl = repoName
    ? `${gitnexusUrl}/?project=${encodeURIComponent(repoName)}`
    : gitnexusUrl;

  return (
    <div className={`code-graph-repo ${selected ? "is-selected" : ""}`}>
      <button
        className="code-graph-repo-main"
        type="button"
        disabled={!repoName}
        title={
          repoName
            ? `在页面内打开 ${title} 的代码图谱`
            : "该仓库尚未索引，先运行索引脚本"
        }
        onClick={() => onSelect(repository)}
      >
        <FolderGit2 size={15} aria-hidden="true" />
        <span>
          <strong>{title}</strong>
          {repository.hidden && <small className="is-hidden-label">已隐藏</small>}
          {primaryPath && <code>{primaryPath}</code>}
        </span>
      </button>
      <div className="code-graph-repo-actions">
        {repository.gitnexus_indexed ? (
          <span className="status-chip is-indexed">已索引</span>
        ) : (
          <span className="status-chip">未索引</span>
        )}
        <button
          className="button primary compact"
          type="button"
          disabled={!repoName}
          onClick={() => onSelect(repository)}
        >
          <Braces size={13} aria-hidden="true" /> 打开图谱
        </button>
        <a
          className="button secondary compact"
          href={projectUrl}
          target="_blank"
          rel="noreferrer"
          title={`在新窗口打开 ${title} 的 GitNexus 图谱`}
        >
          <ExternalLink size={12} aria-hidden="true" /> 新窗口
        </a>
        <Link
          className="button ghost compact"
          to={`/assets/${encodeURIComponent(repository.id)}`}
        >
          资产详情 <ArrowUpRight size={12} aria-hidden="true" />
        </Link>
      </div>
    </div>
  );
}

function DomainWindow({
  domain,
  gitnexusUrl,
  showHidden,
  selectedName,
  onSelect,
}: {
  domain: CodeGraphDomain;
  gitnexusUrl: string;
  showHidden: boolean;
  selectedName: string | null;
  onSelect: (repository: CodeGraphRepository) => void;
}) {
  const visible = domain.repositories ?? [];
  const hidden = domain.hidden_repositories ?? [];
  const repositories = showHidden
    ? [...visible, ...hidden]
    : visible;
  const totalVisible = visible.length + hidden.length;

  return (
    <section className="section-block code-graph-window">
      <header className="code-graph-window-head">
        <div>
          <p className="eyebrow">研究方向</p>
          <h2>{domain.label}</h2>
        </div>
        <span className="code-graph-domain-count">
          {domain.indexed_count}/{totalVisible} 已索引
        </span>
      </header>
      <div className="code-graph-window-scroll">
        {repositories.length ? (
          repositories.map((repository) => (
            <RepositoryRow
              repository={repository}
              gitnexusUrl={gitnexusUrl}
              selected={repository.gitnexus_repo_name === selectedName}
              onSelect={onSelect}
              key={repository.id}
            />
          ))
        ) : (
          <p className="muted code-graph-window-empty">该方向暂无代码仓库。</p>
        )}
      </div>
    </section>
  );
}

export function CodeGraphPage() {
  const codeGraph = useQuery({
    queryKey: ["code-graph"],
    queryFn: ({ signal }) => api.codeGraph(signal),
    retry: false,
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: ({ signal }) => api.settings(signal),
    staleTime: Infinity,
    retry: false,
  });
  const gitnexusUrl =
    settings.data?.app?.gitnexus_url ?? GITNEXUS_FALLBACK_URL;
  const [showHidden, setShowHidden] = useState(false);
  const [selectedRepo, setSelectedRepo] = useState<{
    name: string;
    title: string;
    hidden: boolean;
  } | null>(null);
  const [viewerMode, setViewerMode] = useState<ViewerMode>("gitnexus");
  const [viewerNonce, setViewerNonce] = useState(0);
  const [viewerLoading, setViewerLoading] = useState(true);
  const [serviceStatus, setServiceStatus] =
    useState<ServiceStatus>("checking");

  const hiddenCount = codeGraph.data?.hidden_count ?? 0;
  const viewerSrc = selectedRepo
    ? `${gitnexusUrl}/?project=${encodeURIComponent(selectedRepo.name)}`
    : gitnexusUrl;
  const selectedName = selectedRepo?.name ?? null;

  const allRepositories = useMemo(
    () =>
      (codeGraph.data?.domains ?? []).flatMap((domain) => [
        ...(domain.repositories ?? []),
        ...(domain.hidden_repositories ?? []),
      ]),
    [codeGraph.data],
  );

  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 3000);
    setServiceStatus("checking");
    fetch(`${gitnexusUrl}/api/repos`, { signal: controller.signal })
      .then((response) =>
        setServiceStatus(response.ok ? "online" : "offline"),
      )
      .catch(() => setServiceStatus("offline"))
      .finally(() => window.clearTimeout(timer));
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [gitnexusUrl]);

  useEffect(() => {
    setViewerLoading(true);
    const timer = window.setTimeout(() => setViewerLoading(false), 8000);
    return () => window.clearTimeout(timer);
  }, [viewerSrc, viewerNonce, viewerMode]);

  const selectRepository = useCallback((repository: CodeGraphRepository) => {
    const name = repository.gitnexus_repo_name;
    if (!name) return;
    setSelectedRepo({
      name,
      title: repository.title_zh || repository.title,
      hidden: Boolean(repository.hidden),
    });
  }, []);

  const reloadViewer = useCallback(
    () => setViewerNonce((value) => value + 1),
    [],
  );

  return (
    <div className="page">
      <PageHeader
        eyebrow="本地代码图谱"
        title="代码图谱"
        description="按研究方向浏览仓库，点击打开图谱后在当前页面查看对应代码图谱。"
      />

      {codeGraph.isPending ? (
        <LoadingState label="正在加载代码仓库…" />
      ) : codeGraph.isError ? (
        <ErrorState
          error={codeGraph.error}
          retry={() => void codeGraph.refetch()}
        />
      ) : codeGraph.data && codeGraph.data.domains.length ? (
        <>
          <div className="code-graph-summary">
            <div>
              <strong>{codeGraph.data.total_count}</strong>
              <span>代码仓库</span>
            </div>
            <div>
              <strong>{codeGraph.data.indexed_count}</strong>
              <span>GitNexus 已索引</span>
            </div>
            <div>
              <strong>{codeGraph.data.domains.length}</strong>
              <span>研究方向</span>
            </div>
          </div>

          <div className="section-block code-graph-toolbar">
            <div className="code-graph-service-status">
              <span
                className={`status-chip ${
                  serviceStatus === "online" ? "is-indexed" : ""
                }`}
              >
                {serviceStatus === "online" ? (
                  <Server size={12} aria-hidden="true" />
                ) : (
                  <ServerOff size={12} aria-hidden="true" />
                )}
                {serviceStatusLabel(serviceStatus)}
              </span>
              <p className="muted">
                {selectedRepo
                  ? `当前：${selectedRepo.title}`
                  : "当前：GitNexus 全部仓库"}
              </p>
            </div>
            <div className="code-graph-toolbar-actions">
              {hiddenCount > 0 && (
                <button
                  className="button secondary compact"
                  type="button"
                  onClick={() => setShowHidden((value) => !value)}
                >
                  {showHidden ? (
                    <EyeOff size={13} aria-hidden="true" />
                  ) : (
                    <Eye size={13} aria-hidden="true" />
                  )}
                  {showHidden
                    ? "隐藏已显示"
                    : `显示已隐藏仓库（${hiddenCount}）`}
                </button>
              )}
              <div className="code-graph-viewer-mode" role="group" aria-label="图谱前端">
                <button
                  className={viewerMode === "gitnexus" ? "is-active" : ""}
                  type="button"
                  onClick={() => setViewerMode("gitnexus")}
                >
                  GitNexus 界面
                </button>
                <button
                  className={viewerMode === "hub" ? "is-active" : ""}
                  type="button"
                  onClick={() => setViewerMode("hub")}
                >
                  Hub 图谱
                </button>
              </div>
              <button
                className="button secondary compact"
                type="button"
                onClick={reloadViewer}
              >
                <RefreshCw size={13} aria-hidden="true" /> 重新加载图谱
              </button>
              <a
                className="button secondary compact"
                href={viewerSrc}
                target="_blank"
                rel="noreferrer"
              >
                <ExternalLink size={13} aria-hidden="true" /> 新窗口打开
              </a>
            </div>
          </div>

          <div className="code-graph-window-grid">
            {codeGraph.data.domains.map((domain) => (
              <DomainWindow
                domain={domain}
                gitnexusUrl={gitnexusUrl}
                showHidden={showHidden}
                selectedName={selectedName}
                onSelect={selectRepository}
                key={domain.slug}
              />
            ))}
          </div>

          <section className="gitnexus-viewer" aria-label="代码图谱查看器">
            <header className="gitnexus-viewer-head">
              <div>
                <strong>
                  {selectedRepo ? selectedRepo.title : "代码图谱查看器"}
                </strong>
                <span>
                  {selectedRepo
                    ? viewerMode === "hub"
                      ? "Hub 渲染图谱"
                      : "GitNexus 官方界面"
                    : "请选择仓库"}
                </span>
              </div>
              <span
                className={`status-chip ${
                  serviceStatus === "online" ? "is-indexed" : ""
                }`}
              >
                {serviceStatusLabel(serviceStatus)}
              </span>
            </header>
            <div className="gitnexus-viewer-frame">
              {viewerMode === "hub" ? (
                <CodeGraphViewer
                  gitnexusUrl={gitnexusUrl}
                  repoName={selectedRepo?.name ?? null}
                />
              ) : viewerLoading ? (
                <div className="gitnexus-viewer-overlay" role="status">
                  正在加载 GitNexus 图谱…
                </div>
              ) : null}
              {viewerMode === "gitnexus" && (
                <>
                  {serviceStatus !== "offline" ? (
                    <iframe
                      key={`${viewerSrc}:${viewerNonce}`}
                      title="GitNexus 代码图谱"
                      src={viewerSrc}
                      onLoad={() => setViewerLoading(false)}
                      onError={() => setServiceStatus("offline")}
                    />
                  ) : (
                    <div className="gitnexus-viewer-offline" role="status">
                      <ServerOff size={26} aria-hidden="true" />
                      <strong>GitNexus 服务未启动</strong>
                      <span>
                        仓库列表仍可浏览；运行 `.\scripts\hub.ps1 start`
                        后点击“重新加载图谱”。
                      </span>
                    </div>
                  )}
                </>
              )}
            </div>
            {allRepositories.length === 0 && (
              <p className="muted gitnexus-viewer-hint">
                仓库尚未导入或未建立 GitNexus 索引。
              </p>
            )}
          </section>
        </>
      ) : (
        <EmptyState
          title="还没有代码仓库"
          detail="正式导入带代码表示的资料后，这里会按研究方向展示仓库。"
          kind="index"
        />
      )}
    </div>
  );
}
