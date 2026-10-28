import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import type { PackageImport } from "../api/types";
import { formatBytes } from "../lib/format";
import { useDialogFocus } from "../lib/useDialogFocus";

function ImportPreview({
  record,
  close,
}: {
  record: PackageImport;
  close: () => void;
}) {
  const ref = useDialogFocus();
  const client = useQueryClient();
  const [mapping, setMapping] = useState<Record<string, string | null>>({});
  const [userState, setUserState] = useState(false);
  const review = record.preview.assets.filter(
    (a) => a.action === "needs_review" || a.action === "conflict",
  );
  const commit = useMutation({
    mutationFn: () =>
      api.commitPackage(
        record.id,
        { identity_mapping: mapping, include_user_state: userState },
        record.row_version,
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["domains"] });
      void client.invalidateQueries({ queryKey: ["search"] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  return (
    <div className="command-backdrop" onMouseDown={close}>
      <section
        ref={ref}
        className="product-dialog wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="package-preview-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h2 id="package-preview-title">领域包导入预览</h2>
        {record.status === "committed" || commit.isSuccess ? (
          <>
            <p role="status">此领域包已完成导入。</p>
            <button className="button primary" onClick={close}>
              完成
            </button>
          </>
        ) : (
          <>
            <p>
              共 {record.preview.assets.length}{" "}
              项资产。逐项核对重复候选后确认导入。
            </p>
            {record.preview.attachment_count !== undefined && (
              <p className="muted">
                包内 {record.preview.attachment_count} 份附件 ·{" "}
                {formatBytes(record.preview.attachment_bytes)}
              </p>
            )}
            {!!record.preview.omitted_representations && (
              <p className="inline-warning">
                {record.preview.omitted_representations}{" "}
                项文件未随领域包携带。导入后需重新接入这些 PDF 或代码来源。
              </p>
            )}
            <div className="package-preview-list">
              {record.preview.assets.map((asset) => (
                <article key={asset.id}>
                  <strong>{asset.title}</strong>
                  <span>
                    {asset.action === "create"
                      ? "将创建"
                      : asset.action === "reuse"
                        ? "复用已有资产"
                        : asset.action === "conflict"
                          ? "身份冲突"
                          : "需核对重复"}
                  </span>
                  {(asset.action === "needs_review" ||
                    asset.action === "conflict") && (
                    <select
                      aria-label={`${asset.title} 的导入方式`}
                      value={
                        mapping[asset.id] === null
                          ? "__new__"
                          : (mapping[asset.id] ?? "")
                      }
                      onChange={(e) =>
                        setMapping((current) => ({
                          ...current,
                          [asset.id]:
                            e.target.value === "__new__"
                              ? null
                              : e.target.value,
                        }))
                      }
                    >
                      <option value="">请选择如何处理</option>
                      <option value="__new__">创建独立资产</option>
                      {asset.candidates?.map((c) => (
                        <option
                          key={c.asset_id ?? c.id}
                          value={c.asset_id ?? c.id}
                        >
                          {c.title ?? c.asset_id ?? c.id}
                        </option>
                      ))}
                    </select>
                  )}
                </article>
              ))}
            </div>
            <label className="check-label">
              <input
                type="checkbox"
                disabled={record.preview.has_user_state === false}
                checked={userState}
                onChange={(e) => setUserState(e.target.checked)}
              />
              <span>导入包内阅读状态与批注</span>
            </label>
            <div className="button-row">
              <button className="button secondary" onClick={close}>
                稍后处理
              </button>
              <button
                className="button primary"
                disabled={
                  commit.isPending || review.some((a) => !(a.id in mapping))
                }
                onClick={() => commit.mutate()}
              >
                {commit.isPending ? "导入中…" : "确认导入"}
              </button>
            </div>
            {commit.isError && (
              <p className="inline-error" role="alert">
                {getApiErrorMessage(commit.error)}
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
export function PackageActions({
  domain,
  assetIds,
}: {
  domain?: string;
  assetIds?: string[];
}) {
  const client = useQueryClient();
  const [state, setState] = useState(false);
  const [preview, setPreview] = useState<PackageImport | null>(null);
  const exportJob = useMutation({
    mutationFn: () =>
      api.exportPackage({
        domain,
        asset_ids: assetIds,
        include_user_state: state,
      }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const importJob = useMutation({
    mutationFn: (file: File) => api.importPackage(file),
    onSuccess: (record) => setPreview(record),
  });
  return (
    <div className="package-actions">
      {(domain || assetIds?.length) && (
        <>
          <button
            className="button secondary compact"
            type="button"
            disabled={exportJob.isPending}
            onClick={() => exportJob.mutate()}
          >
            {exportJob.isPending ? "正在创建…" : "导出领域包"}
          </button>
          <label className="check-label">
            <input
              type="checkbox"
              checked={state}
              onChange={(e) => setState(e.target.checked)}
            />
            <span>包含阅读状态与批注</span>
          </label>
        </>
      )}
      {!assetIds && (
        <label className="button secondary compact file-button">
          {importJob.isPending ? "导入中…" : "导入领域包"}
          <input
            type="file"
            accept=".zip,application/zip"
            disabled={importJob.isPending}
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) importJob.mutate(file);
              e.target.value = "";
            }}
          />
        </label>
      )}
      {(exportJob.isError || importJob.isError) && (
        <span className="inline-error" role="alert">
          {getApiErrorMessage(exportJob.error ?? importJob.error)}
        </span>
      )}
      {exportJob.data && (
        <Link className="text-link" to="/jobs">
          导出已提交，查看进度与下载
        </Link>
      )}
      {importJob.data && !preview && (
        <button
          className="button ghost compact"
          onClick={() => setPreview(importJob.data)}
        >
          继续导入预览
        </button>
      )}
      {preview && (
        <ImportPreview record={preview} close={() => setPreview(null)} />
      )}
    </div>
  );
}
