import { useEffect, useState } from "react";

type ComponentState = "healthy" | "unavailable" | "stale";

interface ComponentHealth {
  status: ComponentState;
  detail: string;
  latency_ms: number | null;
}

interface HealthResponse {
  service: string;
  status: "healthy" | "degraded";
  checked_at: string;
  components: Record<string, ComponentHealth>;
}

const componentNames: Record<string, string> = {
  database: "PostgreSQL",
  full_text_index: "Meilisearch",
  vector_index: "Qdrant",
  worker: "后台任务",
};

function readableTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

export default function App() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    async function loadHealth() {
      try {
        const response = await fetch("/api/health", {
          signal: controller.signal,
          headers: { Accept: "application/json" },
        });
        if (!response.ok) {
          throw new Error(`API 返回 HTTP ${response.status}`);
        }
        setHealth((await response.json()) as HealthResponse);
        setHealthError(null);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        setHealthError(error instanceof Error ? error.message : "无法连接 API");
      }
    }

    void loadHealth();
    const interval = window.setInterval(() => void loadHealth(), 15_000);

    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, []);

  return (
    <div className="app-shell">
      <aside className="sidebar" aria-label="主导航">
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            RH
          </span>
          <div>
            <strong>Research Hub</strong>
            <span>个人科研资产</span>
          </div>
        </div>

        <nav>
          <a className="nav-item active" href="#overview">
            总览
          </a>
          <a className="nav-item" href="#search">
            跨领域搜索
          </a>
          <a className="nav-item" href="#graph">
            关系图谱
          </a>
          <a className="nav-item" href="#reading">
            阅读状态
          </a>
          <a className="nav-item" href="#collections">
            领域与脉络
          </a>
        </nav>

        <div className="privacy-note">
          <span className="status-dot" />
          仅在本机访问
        </div>
      </aside>

      <main>
        <header className="topbar">
          <div>
            <p className="eyebrow">本地知识工作台</p>
            <h1>你的科研数字资产</h1>
          </div>
          <div
            className={`system-state ${
              health?.status === "healthy" ? "is-healthy" : "is-degraded"
            }`}
          >
            <span className="status-dot" />
            {health?.status === "healthy" ? "系统就绪" : "系统尚未完全就绪"}
          </div>
        </header>

        <section className="search-panel" id="search">
          <label htmlFor="global-search">跨领域搜索</label>
          <div className="search-box">
            <span aria-hidden="true">⌕</span>
            <input
              id="global-search"
              type="search"
              placeholder="搜索论文、代码、导读、批注或方法……"
              disabled
            />
            <kbd>Ctrl K</kbd>
          </div>
          <p>统一资产导入完成后，可在这里检索全部领域。</p>
        </section>

        <section className="metric-grid" aria-label="资产概况">
          <article>
            <span>论文研究包</span>
            <strong>0</strong>
            <small>等待首次只读导入</small>
          </article>
          <article>
            <span>待读论文</span>
            <strong>0</strong>
            <small>阅读状态将在此汇总</small>
          </article>
          <article>
            <span>已连接领域</span>
            <strong>0</strong>
            <small>计划接入 6 个现有领域</small>
          </article>
          <article>
            <span>关系</span>
            <strong>0</strong>
            <small>图谱尚未建立</small>
          </article>
        </section>

        <section className="content-grid" id="overview">
          <article className="panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">运行状态</p>
                <h2>基础服务</h2>
              </div>
              {health && <time>{readableTime(health.checked_at)} 更新</time>}
            </div>

            {healthError ? (
              <div className="empty-state">
                <strong>API 尚未启动</strong>
                <p>{healthError}</p>
              </div>
            ) : health ? (
              <div className="service-list">
                {Object.entries(health.components).map(([key, component]) => (
                  <div className="service-row" key={key}>
                    <span
                      className={`service-icon service-${component.status}`}
                      aria-hidden="true"
                    />
                    <div>
                      <strong>{componentNames[key] ?? key}</strong>
                      <span title={component.detail}>{component.detail}</span>
                    </div>
                    <small>
                      {component.latency_ms === null
                        ? "—"
                        : `${component.latency_ms.toFixed(0)} ms`}
                    </small>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-state">
                <strong>正在检查服务……</strong>
              </div>
            )}
          </article>

          <article className="panel next-step">
            <p className="eyebrow">下一步</p>
            <h2>建立统一资产目录</h2>
            <p>
              平台骨架已经就位。完成统一 schema 和六个只读来源适配器后，这里会显示论文、
              代码、导读和个人批注。
            </p>
            <ul>
              <li>来源目录保持原样</li>
              <li>新增领域通过配置和适配器接入</li>
              <li>搜索与图谱索引可随时重建</li>
            </ul>
          </article>
        </section>
      </main>
    </div>
  );
}

