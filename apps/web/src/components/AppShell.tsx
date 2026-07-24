import { useQuery } from "@tanstack/react-query";
import {
  BotMessageSquare,
  BriefcaseBusiness,
  Command,
  GitCompareArrows,
  Home,
  LibraryBig,
  Menu,
  Network,
  RefreshCcw,
  Search,
  Settings,
  StickyNote,
  X,
} from "lucide-react";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useCompare } from "../state/CompareContext";

const primaryNavigation = [
  { to: "/", label: "总览", icon: Home, end: true },
  { to: "/search", label: "跨领域搜索", icon: Search },
  { to: "/domains", label: "领域", icon: LibraryBig },
  { to: "/graph", label: "关系图谱", icon: Network },
  { to: "/compare", label: "论文对比", icon: GitCompareArrows },
  { to: "/annotations", label: "批注", icon: StickyNote },
  { to: "/chat", label: "RAG 问答", icon: BotMessageSquare },
];

const systemNavigation = [
  { to: "/jobs", label: "后台任务", icon: BriefcaseBusiness },
  { to: "/conflicts", label: "同步冲突", icon: RefreshCcw },
  { to: "/settings", label: "设置", icon: Settings },
];

function NavigationItem({
  to,
  label,
  icon: Icon,
  end,
}: {
  to: string;
  label: string;
  icon: typeof Home;
  end?: boolean;
}) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) => `nav-item ${isActive ? "is-active" : ""}`}
    >
      <Icon size={18} strokeWidth={1.8} aria-hidden="true" />
      <span>{label}</span>
    </NavLink>
  );
}

function HealthBadge() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: ({ signal }) => api.health(signal),
    refetchInterval: 30_000,
    retry: false,
  });
  const healthy = health.data?.status === "healthy";
  const label = health.isPending
    ? "正在检查"
    : health.isError
      ? "API 离线"
      : healthy
        ? "系统就绪"
        : "部分服务不可用";

  return (
    <span
      className={`health-badge ${healthy ? "is-healthy" : "is-degraded"}`}
      title={health.isError ? "无法连接本地 API" : undefined}
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
  const inputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState("");

  useEffect(() => {
    if (open) {
      setValue("");
      window.setTimeout(() => inputRef.current?.focus(), 0);
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
        className="command-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="快速搜索"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <form onSubmit={submit}>
          <Search size={19} aria-hidden="true" />
          <input
            ref={inputRef}
            type="search"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="搜索论文、代码、导读或批注…"
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
            <button type="button" onClick={() => (navigate("/chat"), close())}>
              RAG 问答
            </button>
            <button type="button" onClick={() => (navigate("/jobs"), close())}>
              任务状态
            </button>
          </div>
        </div>
      </section>
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

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((current) => !current);
      }
      if (event.key === "Escape") setPaletteOpen(false);
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
            RA
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
          <p className="nav-section-label">研究</p>
          {primaryNavigation.map((item) => (
            <NavigationItem {...item} key={item.to} />
          ))}
          <p className="nav-section-label">系统</p>
          {systemNavigation.map((item) => (
            <NavigationItem {...item} key={item.to} />
          ))}
        </nav>

        <div className="sidebar-footer">
          <HealthBadge />
          <span>数据只在本机处理</span>
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
          <button
            className="global-search-trigger"
            type="button"
            onClick={() => setPaletteOpen(true)}
          >
            <Search size={17} aria-hidden="true" />
            <span>搜索全部资产…</span>
            <kbd>
              <Command size={11} aria-hidden="true" />K
            </kbd>
          </button>
          <HealthBadge />
        </header>

        <main id="main-content" tabIndex={-1}>
          {children ?? <Outlet />}
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
      <CompareTray />
    </div>
  );
}
