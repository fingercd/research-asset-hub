import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, ApiError, getApiErrorMessage } from "../api/client";
import type { Asset } from "../api/types";
import { invalidateResearchData } from "../lib/invalidateResearchData";
import { useDialogFocus } from "../lib/useDialogFocus";

const fields = [
  { key: "title", label: "题名 *" },
  { key: "title_zh", label: "中文题名" },
  { key: "subtitle", label: "副标题" },
  { key: "authors", label: "作者（每行一位）", multiline: true },
  { key: "year", label: "年份" },
  { key: "venue", label: "会议 / 期刊" },
  { key: "language", label: "语言" },
  { key: "tags", label: "标签（逗号分隔）" },
  { key: "source_urls", label: "来源 URL（每行一个）", multiline: true },
  { key: "abstract", label: "原始摘要", multiline: true },
  { key: "summary", label: "简介 / 导读", multiline: true },
] as const;
type Field = (typeof fields)[number]["key"];
type Values = Record<Field, string>;

function valuesFrom(asset: Asset): Values {
  return {
    title: asset.title,
    title_zh: asset.title_zh ?? "",
    subtitle: asset.subtitle ?? "",
    authors: (asset.authors ?? []).join("\n"),
    year: asset.year == null ? "" : String(asset.year),
    venue: asset.venue ?? "",
    language: asset.language ?? "",
    tags: (asset.tags ?? []).join(", "),
    source_urls: (asset.source_urls ?? []).join("\n"),
    abstract: asset.abstract ?? "",
    summary: asset.summary ?? "",
  };
}

function patchFrom(values: Values, original: Values): Partial<Asset> {
  const body: Partial<Asset> = {};
  for (const { key } of fields) {
    if (values[key] === original[key]) continue;
    const value = values[key].trim();
    if (key === "authors") {
      body.authors = value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    } else if (key === "tags") {
      body[key] = value.split(/[,，;]/).map((item) => item.trim()).filter(Boolean);
    } else if (key === "source_urls") {
      body.source_urls = value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
    } else if (key === "year") {
      body.year = value ? Number(value) : null;
    } else if (key === "title") {
      body.title = value;
    } else {
      body[key] = value || null;
    }
  }
  return body;
}

export function AssetMetadataEditor({ asset, onClose }: {
  asset: Asset;
  onClose: () => void;
}) {
  const dialogRef = useDialogFocus();
  const client = useQueryClient();
  const [original] = useState(() => valuesFrom(asset));
  const [values, setValues] = useState(original);
  const [current, setCurrent] = useState(asset);
  const [needsRefresh, setNeedsRefresh] = useState(asset.row_version == null);
  const [refreshed, setRefreshed] = useState(false);
  const body = patchFrom(values, original);
  const save = useMutation({
    mutationFn: () => api.updateAsset(asset.id, body, current.row_version),
    onError: (error) => {
      if (error instanceof ApiError && [409, 412, 428].includes(error.status)) {
        setNeedsRefresh(true);
        setRefreshed(false);
      }
    },
    onSuccess: (result) => {
      client.setQueryData<Asset>(["asset", asset.id], (previous) => ({ ...previous, ...result }));
      invalidateResearchData(client, { assetId: asset.id });
      for (const key of ["comparisons", "comparison-context"]) {
        void client.invalidateQueries({ queryKey: [key] });
      }
      onClose();
    },
  });
  const refresh = useMutation({
    mutationFn: () => api.asset(asset.id),
    onSuccess: (result) => {
      setCurrent(result);
      setNeedsRefresh(result.row_version == null);
      setRefreshed(true);
      save.reset();
    },
  });
  const busy = save.isPending || refresh.isPending;
  function close() {
    if (!busy) onClose();
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (values.title.trim() && !needsRefresh && Object.keys(body).length) save.mutate();
  }
  const latest = valuesFrom(current);
  return (
    <div className="command-backdrop metadata-backdrop" onMouseDown={close}>
      <section ref={dialogRef} className="product-dialog metadata-dialog" role="dialog"
        aria-modal="true" aria-labelledby="asset-metadata-title"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            close();
          }
        }}
        onMouseDown={(event) => event.stopPropagation()}>
        <form onSubmit={submit}>
          <h2 id="asset-metadata-title">编辑论文元数据</h2>
          <p className="muted">修订保存到资料库并留下审计记录；来源清单、PDF 和其他原文件保持只读。</p>
          <fieldset className="intake-fields" disabled={busy}>
            {fields.map((field) => (
              <label key={field.key} className={"multiline" in field ? "metadata-wide" : undefined}>
                <span>{field.label}</span>
                {"multiline" in field ? (
                  <textarea rows={3} value={values[field.key]}
                    onChange={(event) => setValues({ ...values, [field.key]: event.target.value })} />
                ) : (
                  <input required={field.key === "title"}
                    type={field.key === "year" ? "number" : "text"}
                    min={field.key === "year" ? 1450 : undefined}
                    max={field.key === "year" ? 2200 : undefined}
                    value={values[field.key]}
                    onChange={(event) => setValues({ ...values, [field.key]: event.target.value })} />
                )}
              </label>
            ))}
          </fieldset>
          <p className="muted">DOI / arXiv 属于身份标识，目前不能在此编辑；更正需走身份审计流程。</p>
          {save.isError && <p className="inline-error" role="alert">保存失败：{getApiErrorMessage(save.error)}。输入已保留，可重试。</p>}
          {needsRefresh && <p className="inline-warning" role="status">请先读取最新版本并核对其他修改，再保存。你的输入会保留。</p>}
          {(needsRefresh || refreshed) && (
            <button className="button secondary compact" type="button" disabled={busy}
              onClick={() => refresh.mutate()}>{refresh.isPending ? "读取中…" : "读取最新版本"}</button>
          )}
          {refresh.isError && <p className="inline-error" role="alert">读取失败：{getApiErrorMessage(refresh.error)}。输入已保留。</p>}
          {refreshed && (
            <div className="inline-warning" role="status">
              <p>已读取最新版本。请核对以下字段的当前值；保存将应用你的输入。</p>
              <dl>{fields.filter(({ key }) => key in body).map(({ key, label }) => (
                <div key={key}><dt>{label}</dt><dd>{latest[key] || "（空）"}</dd></div>
              ))}</dl>
            </div>
          )}
          <div className="form-actions">
            <button className="button secondary" type="button" disabled={busy} onClick={close}>取消</button>
            <button className="button primary" disabled={busy || needsRefresh || !values.title.trim() || !Object.keys(body).length}>
              {save.isPending ? "保存中…" : "保存元数据"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
