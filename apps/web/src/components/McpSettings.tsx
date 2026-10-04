import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, getApiErrorMessage } from "../api/client";
export function McpSettings() {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  const config = useQuery({
    queryKey: ["mcp-config"],
    queryFn: ({ signal }) => api.mcpConfig(signal),
  });
  const check = useMutation({ mutationFn: () => api.mcpCheck() });
  const configuration = config.data?.config;
  const text = JSON.stringify(configuration, null, 2);
  return (
    <section className="section-block mcp-settings">
      <div className="section-heading">
        <div>
          <p className="eyebrow">让研究助手读取资料库</p>
          <h2>MCP 接入</h2>
        </div>
        <button
          className="button secondary compact"
          type="button"
          disabled={check.isPending || config.isPending || config.isError}
          onClick={() => check.mutate()}
        >
          {check.isPending ? "检查中…" : "检查连接"}
        </button>
      </div>
      <p>
        将以下配置添加到支持 MCP
        的客户端，重新加载客户端后即可搜索资料、读取论文证据与代码。当前接入为只读。
      </p>
      {config.isPending ? (
        <p role="status">正在生成本机配置…</p>
      ) : config.isError ? (
        <p className="inline-error" role="alert">
          {getApiErrorMessage(config.error)}
        </p>
      ) : (
        <>
          <label>
            <span>客户端配置 JSON</span>
            <textarea className="mono" rows={12} readOnly value={text} />
          </label>
          <button
            className="button primary compact"
            type="button"
            onClick={() => {
              void navigator.clipboard
                .writeText(text)
                .then(() => {
                  setCopied(true);
                  setCopyError("");
                })
                .catch((error) => setCopyError(getApiErrorMessage(error)));
            }}
          >
            {copied ? "已复制配置" : "复制配置"}
          </button>
          <p className="muted">
            此配置使用当前安装的运行环境与资料位置。客户端的配置文件格式可能不同，请按客户端的
            MCP 设置说明应用。
          </p>
        </>
      )}
      {(check.isError || copyError) && (
        <p className="inline-error" role="alert">
          {copyError || getApiErrorMessage(check.error)}
        </p>
      )}
      {check.data && (
        <div className="mcp-check-result" role="status">
          <strong>
            {check.data.status === "ready"
              ? "本机 MCP 服务与资料库检查通过"
              : "需要检查接入"}
          </strong>
          <p>
            {String(
              check.data.note ??
                "已检查服务构建与资料库读取。客户端连接需在客户端中确认。",
            )}
          </p>
          <p>
            可用工具：{String(check.data.tool_count ?? 0)} · 数据库：
            {check.data.database === "ready"
              ? "就绪"
              : String(check.data.database ?? "未知")}
          </p>
          {Array.isArray(check.data.tools) && (
            <div className="tag-row">
              {check.data.tools.map((tool) => (
                <span key={String(tool)}>{String(tool)}</span>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
