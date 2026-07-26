import {
  ArrowRight,
  BarChart3,
  BookOpen,
  Clock3,
  ExternalLink,
  FolderKanban,
  LibraryBig,
  Map as MapIcon,
  Route,
  ShieldCheck,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import { Link } from "react-router-dom";
import { api } from "../../api/client";
import type {
  Domain,
  DomainOverview,
  DomainPortal,
  DomainPortalSection,
} from "../../api/types";

type PortalView = "overview" | "units" | "recent" | "stats";

const sectionIcons: Record<DomainPortalSection["id"], typeof MapIcon> = {
  scope: MapIcon,
  timeline: Clock3,
  "capability-method-map": MapIcon,
  landmarks: BookOpen,
  benchmarks: BarChart3,
  "reading-routes": Route,
  catalog: LibraryBig,
  frontiers: ArrowRight,
  evidence: ShieldCheck,
};

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function records(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter(
        (item): item is Record<string, unknown> =>
          Boolean(item) && typeof item === "object" && !Array.isArray(item),
      )
    : [];
}

function strings(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];
}

function AssetLinks({ ids }: { ids: string[] }) {
  if (!ids.length) return null;
  return (
    <div className="portal-asset-links" aria-label="关联资料">
      {ids.slice(0, 8).map((id, index) => (
        <Link to={`/assets/${encodeURIComponent(id)}`} key={id}>
          资料 {index + 1}
        </Link>
      ))}
    </div>
  );
}

function ScopeSection({ section }: { section: DomainPortalSection }) {
  return (
    <div className="portal-prose">
      <ReactMarkdown rehypePlugins={[rehypeSanitize]}>
        {text(section.content.markdown)}
      </ReactMarkdown>
      <AssetLinks ids={strings(section.content.asset_ids)} />
    </div>
  );
}

function TimelineSection({ section }: { section: DomainPortalSection }) {
  return (
    <ol className="portal-timeline">
      {records(section.content.stages).map((stage) => (
        <li key={text(stage.id)}>
          <div className="portal-timeline-period">{text(stage.period)}</div>
          <div>
            <h3>{text(stage.title)}</h3>
            <p>{text(stage.summary)}</p>
            <AssetLinks ids={strings(stage.asset_ids)} />
          </div>
        </li>
      ))}
    </ol>
  );
}

function CapabilityMapSection({ section }: { section: DomainPortalSection }) {
  const capabilities = records(section.content.capabilities);
  const methods = records(section.content.methods);
  const cells = records(section.content.cells);
  const methodNames = new Map(methods.map((item) => [text(item.id), text(item.title)]));
  return (
    <div className="portal-capability-grid">
      {capabilities.map((capability) => {
        const matches = cells.filter(
          (cell) => text(cell.capability_id) === text(capability.id),
        );
        return (
          <article key={text(capability.id)}>
            <h3>{text(capability.title)}</h3>
            <p>{text(capability.summary)}</p>
            {matches.map((cell) => (
              <div className="portal-method-cell" key={`${text(capability.id)}-${strings(cell.method_ids).join("-")}`}>
                <div className="tag-row">
                  {strings(cell.method_ids).map((id) => (
                    <span key={id}>{methodNames.get(id) ?? id}</span>
                  ))}
                </div>
                <p>{text(cell.summary)}</p>
                <AssetLinks ids={strings(cell.asset_ids)} />
              </div>
            ))}
          </article>
        );
      })}
    </div>
  );
}

function LandmarkSection({ section }: { section: DomainPortalSection }) {
  return (
    <div className="portal-landmark-grid">
      {records(section.content.items).map((item, index) => (
        <article key={text(item.id)}>
          <span>{String(index + 1).padStart(2, "0")}</span>
          <h3>{text(item.title)}</h3>
          <p>{text(item.summary)}</p>
          {text(item.asset_id) && (
            <Link className="text-link" to={`/assets/${encodeURIComponent(text(item.asset_id))}`}>
              查看资产 <ArrowRight size={14} aria-hidden="true" />
            </Link>
          )}
        </article>
      ))}
    </div>
  );
}

function BenchmarkSection({ section }: { section: DomainPortalSection }) {
  const rows = records(section.content.rows);
  if (!rows.length) {
    return <p className="muted">{text(section.content.not_applicable_reason) || "本领域暂不适用统一 benchmark 表。"}</p>;
  }
  return (
    <div className="portal-table-wrap">
      <table className="portal-table">
        <thead>
          <tr>
            <th>任务</th>
            <th>数据集 / 环境</th>
            <th>指标</th>
            <th>比较注意事项</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={text(row.id)}>
              <th scope="row">{text(row.task)}</th>
              <td>{strings(row.datasets_or_environments).join("、")}</td>
              <td>{strings(row.metrics).join("、")}</td>
              <td>{text(row.caution)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ReadingRoutesSection({ section }: { section: DomainPortalSection }) {
  return (
    <div className="portal-route-grid">
      {records(section.content.routes).map((route) => (
        <article key={text(route.id)}>
          <p className="eyebrow">推荐路线</p>
          <h3>{text(route.title)}</h3>
          <p>{text(route.goal)}</p>
          <ol>
            {records(route.steps).map((step) => (
              <li key={`${text(route.id)}-${String(step.order)}`}>
                <Link to={`/assets/${encodeURIComponent(text(step.asset_id))}`}>
                  {text(step.title)}
                </Link>
                <span>{text(step.rationale)}</span>
              </li>
            ))}
          </ol>
        </article>
      ))}
    </div>
  );
}

function MarkdownSection({ section }: { section: DomainPortalSection }) {
  return (
    <div className="portal-prose">
      <ReactMarkdown rehypePlugins={[rehypeSanitize]}>
        {text(section.content.markdown)}
      </ReactMarkdown>
      <AssetLinks ids={strings(section.content.asset_ids)} />
    </div>
  );
}

function EvidenceSection({
  section,
  portal,
}: {
  section: DomainPortalSection;
  portal: DomainPortal;
}) {
  return (
    <div className="portal-evidence">
      <dl>
        <div>
          <dt>内容版本</dt>
          <dd>{portal.content_version}</dd>
        </div>
        <div>
          <dt>内容摘要</dt>
          <dd><code>{portal.content_digest.slice(0, 16)}</code></dd>
        </div>
        <div>
          <dt>审阅状态</dt>
          <dd>{portal.review_status === "reviewed" ? "已核对" : "需要复核"}</dd>
        </div>
        <div>
          <dt>来源状态</dt>
          <dd>{portal.stale ? "来源发生变化" : "来源指纹一致"}</dd>
        </div>
      </dl>
      <p className="muted">
        已登记 {records(section.content.source_inputs).length} 个原始领域 HTML 输入；
        统一页面是派生视图，源文件保持只读。
      </p>
    </div>
  );
}

function PortalSectionView({
  section,
  portal,
  catalog,
}: {
  section: DomainPortalSection;
  portal: DomainPortal;
  catalog: ReactNode;
}) {
  const Icon = sectionIcons[section.id];
  return (
    <section
      className={`portal-section portal-section-${section.id}`}
      id={`portal-${section.id}`}
      aria-labelledby={`portal-${section.id}-title`}
    >
      <header className="portal-section-heading">
        <Icon size={19} aria-hidden="true" />
        <div>
          <p className="eyebrow">{section.id}</p>
          <h2 id={`portal-${section.id}-title`}>{section.title}</h2>
        </div>
      </header>
      {section.id === "scope" && <ScopeSection section={section} />}
      {section.id === "timeline" && <TimelineSection section={section} />}
      {section.id === "capability-method-map" && <CapabilityMapSection section={section} />}
      {section.id === "landmarks" && <LandmarkSection section={section} />}
      {section.id === "benchmarks" && <BenchmarkSection section={section} />}
      {section.id === "reading-routes" && <ReadingRoutesSection section={section} />}
      {section.id === "catalog" && catalog}
      {section.id === "frontiers" && <MarkdownSection section={section} />}
      {section.id === "evidence" && <EvidenceSection section={section} portal={portal} />}
    </section>
  );
}

function OriginalHtmlButton({ overview }: { overview?: DomainOverview | null }) {
  const [opening, setOpening] = useState(false);
  const canOpen = Boolean(
    overview?.representation_id &&
      ["available", "network_required"].includes(overview.status),
  );
  const openOriginal = async () => {
    if (!overview?.representation_id || !canOpen || opening) return;
    const target = window.open("about:blank", "_blank");
    if (target) target.opener = null;
    setOpening(true);
    try {
      const action = await api.openRepresentation(overview.representation_id, {
        presentation: "original",
        embed: false,
      });
      if (target) target.location.href = action.url;
      else window.open(action.url, "_blank", "noopener,noreferrer");
    } catch {
      target?.close();
    } finally {
      setOpening(false);
    }
  };
  return (
    <button
      className="button secondary"
      type="button"
      disabled={!canOpen || opening}
      title={canOpen ? "在隔离网关中打开只读原始 HTML" : overview?.warning ?? "原始 HTML 不可用"}
      onClick={() => void openOriginal()}
    >
      <ExternalLink size={14} aria-hidden="true" />
      {opening ? "正在打开…" : "原始 HTML"}
    </button>
  );
}

export function DomainPortalArticle({
  portal,
  domain,
  activeView,
  onViewChange,
  units,
  recent,
  stats,
}: {
  portal: DomainPortal;
  domain: Domain;
  activeView: PortalView;
  onViewChange: (view: PortalView) => void;
  units: ReactNode;
  recent: ReactNode;
  stats: ReactNode;
}) {
  const tabs: Array<{ id: PortalView; label: string; icon: typeof MapIcon }> = [
    { id: "overview", label: "领域总览", icon: MapIcon },
    { id: "units", label: "研究单元", icon: FolderKanban },
    { id: "recent", label: "最近更新", icon: Clock3 },
    { id: "stats", label: "统计", icon: BarChart3 },
  ];
  const catalog = (
    <div className="portal-catalog-summary">
      <p>{text(portal.sections.find((item) => item.id === "catalog")?.content.summary)}</p>
      <button className="button primary" type="button" onClick={() => onViewChange("units")}>
        浏览全部研究单元 <ArrowRight size={14} aria-hidden="true" />
      </button>
    </div>
  );
  return (
    <article className="domain-portal" data-domain-portal data-version="v2">
      <header className="domain-portal-hero">
        <div>
          <p className="eyebrow">{portal.slug}</p>
          <h1>{portal.label}</h1>
          <p>{portal.description}</p>
        </div>
        <div className="domain-portal-actions">
          <Link className="button secondary" to={`/search?domain=${encodeURIComponent(domain.slug)}`}>
            搜索此领域
          </Link>
          <OriginalHtmlButton overview={domain.overview} />
        </div>
      </header>
      <dl className="domain-portal-metrics" aria-label="领域概况">
        <div><dt>论文</dt><dd>{domain.paper_count ?? 0}</dd></div>
        <div><dt>仓库</dt><dd>{domain.repository_count ?? 0}</dd></div>
        <div><dt>研究单元</dt><dd>{domain.research_unit_count ?? 0}</dd></div>
        <div><dt>阅读中</dt><dd>{domain.reading_counts?.reading ?? 0}</dd></div>
      </dl>
      <nav
        className="domain-portal-tabs"
        role="tablist"
        aria-label="领域内容"
      >
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              className={activeView === tab.id ? "is-active" : undefined}
              type="button"
              role="tab"
              aria-selected={activeView === tab.id}
              onClick={() => onViewChange(tab.id)}
              key={tab.id}
            >
              <Icon size={15} aria-hidden="true" />
              {tab.label}
            </button>
          );
        })}
      </nav>
      <div className="domain-portal-body">
        {activeView === "overview" &&
          portal.sections.map((section) => (
            <PortalSectionView
              section={section}
              portal={portal}
              catalog={catalog}
              key={section.id}
            />
          ))}
        {activeView === "units" && units}
        {activeView === "recent" && recent}
        {activeView === "stats" && stats}
      </div>
    </article>
  );
}
