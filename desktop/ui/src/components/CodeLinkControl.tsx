import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
export function CodeLinkControl({
  paperId,
  repositoryId,
}: {
  paperId: string;
  repositoryId?: string;
}) {
  const client = useQueryClient();
  const [source, setSource] = useState("url");
  const [value, setValue] = useState("");
  const [kind, setKind] = useState("official");
  const [revision, setRevision] = useState("");
  const [download, setDownload] = useState(false);
  const link = useMutation({
    mutationFn: () =>
      api.linkCode({
        paper_id: paperId,
        kind,
        download: source === "url" && download,
        revision: revision.trim() || undefined,
        ...(source === "url"
          ? { url: value.trim() }
          : source === "existing"
            ? { existing_asset_id: value.trim() }
            : { local_directory: value.trim() }),
      }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["asset", paperId] });
      void client.invalidateQueries({ queryKey: ["representations", paperId] });
      void client.invalidateQueries({ queryKey: ["search"] });
    },
  });
  const snapshot = useMutation({
    mutationFn: () =>
      api.repositorySnapshot({
        repository_asset_id: (repositoryId ?? link.data?.repository_asset_id)!,
        revision: revision.trim() || undefined,
      }),
  });
  const snapshotRepositoryId = repositoryId ?? link.data?.repository_asset_id;
  return (
    <details className="section-block code-link-control">
      <summary>关联代码与下载快照</summary>
      <div className="advanced-filter-grid">
        <label>
          <span>代码来源</span>
          <select value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="url">仓库 URL</option>
            <option value="existing">已有仓库资产 ID</option>
            <option value="local">本地代码目录</option>
          </select>
        </label>
        <label>
          <span>
            {source === "url"
              ? "仓库 URL"
              : source === "existing"
                ? "仓库资产 ID"
                : "本机稳定目录"}
          </span>
          <input
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={source === "url" ? "https://github.com/…" : ""}
          />
        </label>
        <label>
          <span>关系</span>
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="official">官方实现</option>
            <option value="reproduction">复现</option>
            <option value="project">项目</option>
          </select>
        </label>
        <label>
          <span>分支、tag 或 commit（可选）</span>
          <input
            value={revision}
            onChange={(e) => setRevision(e.target.value)}
          />
        </label>
      </div>
      <label className="check-label">
        <input
          type="checkbox"
          checked={download}
          onChange={(e) => setDownload(e.target.checked)}
          disabled={source !== "url"}
        />
        <span>关联后下载代码快照</span>
      </label>
      <div className="button-row">
        <button
          className="button primary compact"
          type="button"
          disabled={!value.trim() || link.isPending}
          onClick={() => link.mutate()}
        >
          {link.isPending ? "关联中…" : "保存代码关联"}
        </button>
        {snapshotRepositoryId && (
          <button
            className="button secondary compact"
            type="button"
            disabled={snapshot.isPending}
            onClick={() => snapshot.mutate()}
          >
            下载此仓库快照
          </button>
        )}
      </div>
      {(link.isError || snapshot.isError) && (
        <p className="inline-error" role="alert">
          {getApiErrorMessage(link.error ?? snapshot.error)}
        </p>
      )}
      {(link.isSuccess || snapshot.isSuccess) && (
        <p role="status">
          操作已提交。<Link to="/jobs">查看下载任务</Link>
        </p>
      )}
    </details>
  );
}
