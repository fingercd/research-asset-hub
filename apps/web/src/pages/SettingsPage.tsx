import { useQuery } from "@tanstack/react-query";
import {
  Database,
  HardDrive,
  LockKeyhole,
  Search,
  Settings2,
} from "lucide-react";
import { McpSettings } from "../components/McpSettings";
import { api } from "../api/client";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { safeJson } from "../lib/format";

const sensitivePattern = /(secret|token|password|api[_-]?key|credential)/i;

function redactSensitive(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactSensitive);
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, nested]) => [
        key,
        sensitivePattern.test(key) && nested
          ? "••••••••"
          : redactSensitive(nested),
      ]),
    );
  }
  return value;
}

function displayValue(key: string, value: unknown): string {
  if (sensitivePattern.test(key) && value) return "••••••••";
  return safeJson(redactSensitive(value));
}

function SettingsGroup({
  title,
  icon: Icon,
  values,
}: {
  title: string;
  icon: typeof Settings2;
  values?: Record<string, unknown>;
}) {
  if (!values || !Object.keys(values).length) return null;
  return (
    <section className="settings-card">
      <header>
        <span>
          <Icon size={19} aria-hidden="true" />
        </span>
        <h2>{title}</h2>
      </header>
      <dl>
        {Object.entries(values).map(([key, value]) => (
          <div key={key}>
            <dt>{key.replaceAll("_", " ")}</dt>
            <dd
              className={
                typeof value === "object" && value !== null ? "is-json" : ""
              }
            >
              {displayValue(key, value)}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export function SettingsPage() {
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: ({ signal }) => api.settings(signal),
  });

  const data = settings.data;
  const known = new Set(["app", "search", "sources", "integrations"]);
  const other = data
    ? Object.fromEntries(
        Object.entries(data).filter(([key]) => !known.has(key)),
      )
    : {};

  return (
    <div className="page">
      <PageHeader
        eyebrow="连接与配置"
        title="设置"
        description="配置研究助手接入，查看本地服务、搜索与来源状态。"
      />
      <div className="notice">
        <LockKeyhole size={17} aria-hidden="true" />
        <div>
          <strong>本地优先</strong>
          <span>来源目录按只读方式接入；本页不会显示密钥或认证值。</span>
        </div>
      </div>
      <McpSettings />
      {settings.isPending ? (
        <LoadingState label="正在加载设置…" />
      ) : settings.isError ? (
        <ErrorState
          error={settings.error}
          retry={() => void settings.refetch()}
        />
      ) : data ? (
        <div className="settings-grid">
          <SettingsGroup title="应用" icon={Settings2} values={data.app} />
          <SettingsGroup
            title="搜索与索引"
            icon={Search}
            values={data.search}
          />
          {!!data.sources?.length && (
            <section className="settings-card span-full">
              <header>
                <span>
                  <HardDrive size={19} aria-hidden="true" />
                </span>
                <h2>来源</h2>
              </header>
              <div className="source-table-wrap">
                <table className="source-table">
                  <thead>
                    <tr>
                      <th>来源</th>
                      <th>适配器</th>
                      <th>状态</th>
                      <th>模式</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.sources.map((source, index) => (
                      <tr key={String(source.id ?? source.alias ?? index)}>
                        <td>
                          {String(
                            source.label ?? source.alias ?? `来源 ${index + 1}`,
                          )}
                        </td>
                        <td>{String(source.adapter ?? "—")}</td>
                        <td>
                          {source.enabled === false
                            ? "已停用"
                            : String(source.status ?? "已启用")}
                        </td>
                        <td>{source.read_only === false ? "可写" : "只读"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
          {!!data.integrations?.length && (
            <section className="settings-card span-full">
              <header>
                <span>
                  <Database size={19} aria-hidden="true" />
                </span>
                <h2>集成</h2>
              </header>
              <div className="integration-list">
                {data.integrations.map((integration, index) => (
                  <div
                    key={String(integration.id ?? integration.name ?? index)}
                  >
                    <span>
                      <strong>
                        {String(
                          integration.label ??
                            integration.name ??
                            `集成 ${index + 1}`,
                        )}
                      </strong>
                      <small>
                        {String(integration.detail ?? integration.type ?? "")}
                      </small>
                    </span>
                    <span
                      className={`state-pill state-${String(integration.status ?? "unknown")}`}
                    >
                      {String(integration.status ?? "未知")}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}
          <SettingsGroup title="其他" icon={Settings2} values={other} />
          {!Object.keys(data).length && (
            <EmptyState
              title="暂无设置数据"
              detail="API 已连接，但尚未返回配置概览。"
            />
          )}
        </div>
      ) : null}
    </div>
  );
}
