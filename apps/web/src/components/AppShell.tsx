import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  BriefcaseBusiness,
  ChevronRight,
  GitCompareArrows,
  LibraryBig,
  Menu,
  RefreshCcw,
  Search,
  Settings,
  StickyNote,
  X,
} from "lucide-react";
import { Suspense, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError, getApiErrorMessage } from "../api/client";
import { useCompare } from "../state/CompareContext";
import { useDialogFocus } from "../lib/useDialogFocus";
import { usePageHistory } from "../lib/usePageHistory";

const primaryNavigation = [
  { to: "/", label: "资料库", icon: LibraryBig, end: true },
  { to: "/domains", label: "领域", icon: BookOpen },
  { to: "/annotations", label: "笔记", icon: StickyNote },
];

const systemLinks = [
  { to: "/jobs", label: "后台任务", icon: BriefcaseBusiness },
  { to: "/conflicts", label: "同步冲突", icon: RefreshCcw },
  { to: "/settings", label: "诊断信息", icon: Settings },
];

function NavigationItem({
  to,
  label,
  icon: Icon,
  end,
}: {
  to: string;
  label: string;
  icon: typeof LibraryBig;
  end?: boolean;
}) {
  const location = useLocation();
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => {
        const inLibrary =
          to === "/" &&
          (location.pathname === "/search" ||
            location.pathname.startsWith("/assets/"));
        return `nav-item ${isActive || inLibrary ? "is-active" : ""}`;
      }}
    >
      <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
      <span>{label}</span>
    </NavLink>
  );
}

function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.health(signal),
    refetchInterval: 30_000,
    retry: false,
  });
}

function HealthBadge() {
  const health = useHealth();
  const healthy = !health.isError && health.data?.status === "healthy";
  const label = health.isPending
    ? "正在检查"
    : health.isError
      ? health.error instanceof ApiError && health.error.title === "请求超时"
        ? "状态检查超时"
        : "无法确认状态"
      : healthy
        ? "系统就绪"
        : "系统需要检查";

  return (
    <span
      className={`health-badge ${health.isPending ? "is-checking" : healthy ? "is-healthy" : "is-degraded"}`}
      title={health.isError ? getApiErrorMessage(health.error) : undefined}
    >
      <span className="health-dot" aria-hidden="true" />
      {label}
    </span>
  );
}

function CommandPalette({
  open,
  close,
}: {
  open: boolean;
  close: () => void;
}) {
  const navigate = useNavigate();
  const dialogRef = useDialogFocus(open);
  const [value, setValue] = useState("");

  useEffect(() => {
    if (open) {
      setValue("");
    }
  }, [open]);

  if (!open) return null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const query = value.trim();
    navigate(query ? `/search?q=${encodeURIComponent(query)}` : "/search");
    close();
  };

  return (
    <div className="command-backdrop" role="presentation" onMouseDown={close}>
      <section
        ref={dialogRef}
        className="command-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="快速搜索"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <form onSubmit={submit}>
          <Search size={19} aria-hidden="true" />
          <input
            type="search"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="搜索论文、代码或笔记…"
            aria-label="全局搜索关键词"
          />
          <kbd>Esc</kbd>
        </form>
        <div className="command-hints">
          <span>输入关键词后按 Enter 搜索</span>
          <div>
            <button type="button" onClick={() => (navigate("/domains"), close())}>
              领域
            </button>
            <button
              type="button"
              onClick={() => (navigate("/annotations"), close())}
            >
              笔记
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

function SystemStatusPanel({ close }: { close: () => void }) {
  const dialogRef = useDialogFocus();
  const health = useHealth();
  const componentLabels: Record<string, string> = {
    api: "应用服务", database: "数据库", postgres: "数据库", postgresql: "数据库",
    meilisearch: "关键词搜索", keyword: "关键词搜索", search: "搜索索引",
    full_text_index: "全文索引",
    gateway: "文件服务", asset_gateway: "文件服务", worker: "后台任务",
  };
  return (
    <div className="system-panel-backdrop" role="presentation" onMouseDown={close}>
      <aside
        ref={dialogRef}
        className="system-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="system-panel-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header>
          <div>
            <span className="system-panel-icon" aria-hidden="true">
              <Activity size={18} />
            </span>
            <div>
              <p className="eyebrow">本地运行状态</p>
              <h2 id="system-panel-title">系统状态</h2>
            </div>
          </div>
          <button className="icon-button" type="button" onClick={close}>
            <X size={18} aria-hidden="true" />
            <span className="sr-only">关闭系统状态</span>
          </button>
        </header>

        <section className="system-health-card" aria-label="服务状态">
          <HealthBadge />
          <p>资料与索引只在当前设备处理。</p>
          {health.isError ? (
            <p className="inline-error" role="alert">{getApiErrorMessage(health.error)} 可重新检查；如果资料也无法读取，可从托盘重试启动。</p>
          ) : (
            <dl className="service-status-list">
              {Object.entries(health.data?.components ?? {}).map(([name, component]) => (
                <div key={name}>
                  <dt>{componentLabels[name] ?? name}</dt>
                  <dd className={component.status === "healthy" ? "is-healthy" : "is-degraded"}>
                    {component.status === "healthy" ? "正常" : component.status === "stale" ? "待更新" : component.status === "disabled" ? "未启用" : "需检查"}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          <button className="button secondary compact" type="button" disabled={health.isFetching} onClick={() => void health.refetch()}>
            <RefreshCcw size={14} className={health.isFetching ? "spin" : undefined} aria-hidden="true" />
            {health.isFetching ? "正在检查…" : "重新检查"}
          </button>
        </section>

        <nav aria-label="工具与诊断">
          {systemLinks.map(({ to, label, icon: Icon }) => (
            <NavLink to={to} key={to} onClick={close}>
              <span>
                <Icon size={17} aria-hidden="true" />
                {label}
              </span>
              <ChevronRight size={16} aria-hidden="true" />
            </NavLink>
          ))}
        </nav>
      </aside>
    </div>
  );
}

function CompareTray() {
  const compare = useCompare();
  if (!compare.assets.length) return null;

  return (
    <aside className="compare-tray" aria-label="待对比论文">
      <div>
        <GitCompareArrows size={17} aria-hidden="true" />
        <strong>{compare.assets.length}/4 篇待对比</strong>
        <span>{compare.assets.map((asset) => asset.title_zh || asset.title).join(" · ")}</span>
      </div>
      <NavLink className="button primary compact" to="/compare">
        打开对比
      </NavLink>
      <button className="icon-button" type="button" onClick={compare.clear}>
        <X size={17} aria-hidden="true" />
        <span className="sr-only">清空对比列表</span>
      </button>
    </aside>
  );
}

export function AppShell({ children }: { children?: ReactNode }) {
  const location = useLocation();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [systemOpen, setSystemOpen] = useState(false);
  const history = usePageHistory();
  const pageLabel = location.pathname.startsWith("/assets/") ? "资料详情"
    : location.pathname.startsWith("/domains/") ? "领域资料"
    : ({ "/": "资料库", "/search": "搜索资料", "/domains": "研究领域", "/annotations": "我的笔记", "/compare": "论文对比", "/jobs": "后台任务", "/conflicts": "同步冲突", "/settings": "诊断信息" }[location.pathname] ?? "资料库");

  const openSearch = () => {
    setSystemOpen(false);
    setPaletteOpen(true);
  };
  const openSystem = () => {
    setPaletteOpen(false);
    setSystemOpen(true);
  };

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((current) => !current);
        setSystemOpen(false);
      }
      if (event.key === "Escape") {
        setPaletteOpen(false);
        setSystemOpen(false);
        setMobileNavOpen(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  useEffect(() => {
    setMobileNavOpen(false);
  }, [location.pathname]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside
        className={`sidebar ${mobileNavOpen ? "is-open" : ""}`}
        aria-label="应用导航"
      >
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <BookOpen size={21} strokeWidth={1.8} />
          </span>
          <div>
            <strong>Research Asset Hub</strong>
            <span>本地科研资产工作台</span>
          </div>
          <button
            className="sidebar-close"
            type="button"
            onClick={() => setMobileNavOpen(false)}
          >
            <X size={20} aria-hidden="true" />
            <span className="sr-only">关闭导航</span>
          </button>
        </div>

        <nav aria-label="主导航">
          {primaryNavigation.map((item) => (
            <NavigationItem {...item} key={item.to} />
          ))}
        </nav>

        <div className="sidebar-footer">
          <button
            className="system-status-button"
            type="button"
            aria-expanded={systemOpen}
            onClick={openSystem}
          >
            <HealthBadge />
            <ChevronRight size={15} aria-hidden="true" />
          </button>
          <span>资料保存在本机</span>
        </div>
      </aside>

      <div className="workspace">
        <header className="topbar">
          <button
            className="mobile-menu-button"
            type="button"
            aria-expanded={mobileNavOpen}
            onClick={() => setMobileNavOpen(true)}
          >
            <Menu size={20} aria-hidden="true" />
            <span className="sr-only">打开导航</span>
          </button>
          <div className="page-navigation" aria-label="页面历史">
            <button className="icon-button" type="button" onClick={history.back} disabled={!history.canGoBack} aria-label="返回上一页" title="返回上一页">
              <ArrowLeft size={18} aria-hidden="true" />
            </button>
            <button className="icon-button" type="button" onClick={history.forward} disabled={!history.canGoForward} aria-label="前进到下一页" title="前进到下一页">
              <ArrowRight size={18} aria-hidden="true" />
            </button>
          </div>
          <strong className="current-page-label">{pageLabel}</strong>
          <button className="global-search-trigger" type="button" onClick={openSearch} aria-label="搜索全部资料">
            <Search size={17} aria-hidden="true" />
            <span>搜索全部资料</span>
            <kbd>Ctrl K</kbd>
          </button>
          <button
            className="topbar-system-button icon-button"
            type="button"
            aria-label="打开系统状态"
            onClick={openSystem}
          >
            <Activity size={19} aria-hidden="true" />
          </button>
        </header>

        <main id="main-content" tabIndex={-1}>
          <Suspense fallback={<div className="route-loading" role="status">正在加载页面…</div>}>
            {children ?? <Outlet />}
          </Suspense>
        </main>
      </div>

      {mobileNavOpen && (
        <button
          className="nav-backdrop"
          type="button"
          aria-label="关闭导航"
          onClick={() => setMobileNavOpen(false)}
        />
      )}
      <CommandPalette open={paletteOpen} close={() => setPaletteOpen(false)} />
      {systemOpen && <SystemStatusPanel close={() => setSystemOpen(false)} />}
      <CompareTray />
    </div>
  );
}
