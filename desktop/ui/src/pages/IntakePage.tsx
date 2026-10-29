import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Upload, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import type { IntakeDraft, IntakePreview } from "../api/types";
import { DomainEditor } from "../components/DomainEditor";
import { PageHeader } from "../components/PageHeader";

type OptionalMetadata = {
  summary: string;
  authors: string;
  year: string;
  doi: string;
  arxiv: string;
  htmlUrl: string;
  tags: string;
};
function draftMetadata(draft: IntakeDraft): OptionalMetadata {
  return {
    summary: draft.summary ?? "",
    authors: (draft.authors ?? []).join(", "),
    year: draft.year == null ? "" : String(draft.year),
    doi: draft.doi ?? "",
    arxiv: draft.arxiv_id ?? "",
    htmlUrl: draft.html_url ?? "",
    tags: (draft.tags ?? []).join(", "),
  };
}
type Row = {
  key: string;
  file?: File;
  title: string;
  metadata: OptionalMetadata;
  draft?: IntakeDraft;
  preview?: IntakePreview;
  action: "create" | "attach";
  attachId: string;
  busy: boolean;
  reviewed?: boolean;
  referencePath?: string;
  error?: string;
  result?: string;
};
export function IntakePage() {
  const [search] = useSearchParams();
  const client = useQueryClient();
  const [domain, setDomain] = useState(search.get("domain") ?? "");
  const [createDomain, setCreateDomain] = useState(false);
  const [rows, setRows] = useState<Row[]>([]);
  const [optionalMetadata, setOptionalMetadata] = useState<OptionalMetadata>({
    summary: "", authors: "", year: "", doi: "", arxiv: "", htmlUrl: "", tags: "",
  });
  const { summary, authors, year, doi, arxiv, htmlUrl, tags } = optionalMetadata;
  const [storageMode, setStorageMode] = useState<"managed" | "reference">(
    "managed",
  );
  const [referencePath, setReferencePath] = useState("");
  const [referenceTitle, setReferenceTitle] = useState("");
  const [codeUrl, setCodeUrl] = useState("");
  const [downloadCode, setDownloadCode] = useState(false);
  const [batchBusy, setBatchBusy] = useState(false);
  const domains = useQuery({
    queryKey: ["domains"],
    queryFn: ({ signal }) => api.domains(signal),
  });
  const drafts = useQuery({
    queryKey: ["intake-drafts"],
    queryFn: ({ signal }) => api.intakeDrafts(signal),
  });
  useEffect(
    () =>
      setRows((current) =>
        current.map((row) =>
          row.result || row.draft?.status === "committing"
            ? row
            : { ...row, preview: undefined, reviewed: false },
        ),
      ),
    [domain],
  );
  const editMetadata = (values: Partial<OptionalMetadata>) => {
    setOptionalMetadata((current) => ({ ...current, ...values }));
    setRows((current) =>
      current.map((row) =>
        row.result || row.draft?.status === "committing"
          ? row
          : {
              ...row,
              metadata: { ...row.metadata, ...values },
              preview: undefined,
              reviewed: false,
            },
      ),
    );
  };
  const patch = (key: string, values: Partial<Row>) =>
    setRows((current) =>
      current.map((row) => (row.key === key ? { ...row, ...values } : row)),
    );
  const files = (selected: FileList | null) => {
    if (!selected) return;
    const incoming = Array.from(selected).map((file) => ({
      key: crypto.randomUUID(),
      file,
      title: file.name.replace(/\.pdf$/i, ""),
      metadata: optionalMetadata,
      action: "create" as const,
      attachId: "",
      busy: false,
    }));
    setRows((current) => [...current, ...incoming]);
  };
  async function prepare(row: Row) {
    patch(row.key, { busy: true, error: undefined });
    try {
      if (
        !domain ||
        !row.title.trim() ||
        (!row.file && !row.draft && !row.referencePath)
      )
        throw new Error("请选择领域，并为每篇论文提供题名与 PDF。");
      const input = row.metadata;
      const metadata = {
        domain,
        title: row.title.trim(),
        summary: input.summary.trim() || null,
        authors: input.authors
          .split(/[,，;]/)
          .map((v) => v.trim())
          .filter(Boolean),
        year: input.year ? Number(input.year) : null,
        doi:
          rows.length === 1
            ? input.doi.trim() || null
            : (row.draft?.doi ?? undefined),
        arxiv_id:
          rows.length === 1
            ? input.arxiv.trim() || null
            : (row.draft?.arxiv_id ?? undefined),
        html_url:
          rows.length === 1
            ? input.htmlUrl.trim() || null
            : (row.draft?.html_url ?? undefined),
        tags: input.tags
          .split(/[,，]/)
          .map((v) => v.trim())
          .filter(Boolean),
      };
      const locked = row.draft?.status === "committing";
      const draft = row.draft
        ? locked
          ? row.draft
          : await api.updateIntakeDraft(row.draft.id, metadata)
        : await api.createIntakeDraft({
            ...metadata,
            storage_mode: row.referencePath ? "reference" : "managed",
          });
      patch(row.key, { draft });
      if (!locked) {
        if (row.file && !draft.pdf_uploaded) {
          const uploaded = await api.uploadIntakePdf(draft.id, row.file);
          patch(row.key, { draft: uploaded });
        }
        if (row.referencePath)
          await api.updateIntakeDraft(draft.id, {
            reference_path: row.referencePath,
          });
      }
      const preview = await api.previewIntakeDraft(draft.id);
      patch(row.key, {
        draft: preview.draft,
        preview,
        busy: false,
        reviewed: locked || !preview.duplicates.length,
      });
      void client.invalidateQueries({ queryKey: ["intake-drafts"] });
    } catch (error) {
      patch(row.key, { busy: false, error: getApiErrorMessage(error) });
    }
  }
  async function commit(row: Row) {
    if (!row.draft || !row.preview || !row.reviewed) return;
    patch(row.key, { busy: true, error: undefined });
    try {
      const result = await api.commitIntakeDraft(row.draft.id, {
        action: row.action,
        ...(row.action === "attach" ? { asset_id: row.attachId } : {}),
      });
      patch(row.key, { busy: false, result: result.asset_id });
      if (rows.length === 1 && codeUrl.trim()) {
        try {
          await api.linkCode({
            paper_id: result.asset_id,
            url: codeUrl.trim(),
            kind: "official",
            download: downloadCode,
          });
        } catch (error) {
          patch(row.key, {
            error: `论文已入库，代码关联失败：${getApiErrorMessage(error)}`,
          });
        }
      }
      void client.invalidateQueries({ queryKey: ["search"] });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
      void client.invalidateQueries({ queryKey: ["domains"] });
      void client.invalidateQueries({ queryKey: ["intake-drafts"] });
    } catch (error) {
      patch(row.key, { busy: false, error: getApiErrorMessage(error) });
      try {
        const preview = await api.previewIntakeDraft(row.draft.id);
        patch(row.key, { draft: preview.draft, preview });
      } catch {
        /* The original commit error remains visible; the draft can be retried. */
      }
    }
  }
  async function prepareAll() {
    setBatchBusy(true);
    for (const row of rows.filter((r) => !r.result)) await prepare(row);
    setBatchBusy(false);
  }
  async function commitAll() {
    setBatchBusy(true);
    for (const row of rows.filter(
      (r) =>
        r.preview &&
        r.reviewed &&
        !r.result &&
        (r.action === "create" || r.attachId),
    ))
      await commit(row);
    setBatchBusy(false);
  }
  async function remove(row: Row) {
    try {
      if (row.draft && !row.result) await api.cancelIntakeDraft(row.draft.id);
      setRows((current) => current.filter((r) => r.key !== row.key));
      void client.invalidateQueries({ queryKey: ["intake-drafts"] });
    } catch (error) {
      patch(row.key, { error: getApiErrorMessage(error) });
    }
  }
  return (
    <div className="page">
      <PageHeader
        title="添加论文"
        eyebrow="单篇与批量入库"
        description="选择领域，上传 PDF 并确认题名即可入库。简介、HTML 与代码可稍后补充。"
      />
      {domains.isError && (
        <p className="inline-error" role="alert">
          领域无法读取：{getApiErrorMessage(domains.error)}
        </p>
      )}
      {drafts.isError && (
        <p className="inline-warning" role="status">
          历史草稿无法读取：{getApiErrorMessage(drafts.error)}
        </p>
      )}
      <section className="section-block intake-form">
        <fieldset
          className="intake-fields"
          disabled={batchBusy || rows.some((row) => row.busy)}
        >
          <div className="form-row">
            <label className="grow">
              <span>所属领域 *</span>
              <select
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
              >
                <option value="">选择领域</option>
                {domains.data?.items.map((d) => (
                  <option key={d.slug} value={d.slug}>
                    {d.label ?? d.name ?? d.slug}
                  </option>
                ))}
              </select>
            </label>
            <button
              className="button secondary"
              type="button"
              onClick={() => setCreateDomain(true)}
            >
              <Plus size={16} />
              新建领域
            </button>
          </div>
          <label>
            <span>PDF 保存方式</span>
            <select
              value={storageMode}
              disabled={rows.length > 0}
              onChange={(e) =>
                setStorageMode(e.target.value as "managed" | "reference")
              }
            >
              <option value="managed">复制到资料库（推荐）</option>
              <option value="reference">原位引用永久目录</option>
            </select>
          </label>
          {storageMode === "managed" ? (
            <label className="upload-zone">
              <Upload size={25} />
              <strong>选择一篇或多篇 PDF</strong>
              <span>文件会复制到应用管理的资料库；原文件保留。</span>
              <input
                type="file"
                accept="application/pdf,.pdf"
                multiple
                onChange={(e) => {
                  files(e.target.files);
                  e.target.value = "";
                }}
              />
            </label>
          ) : (
            <div className="reference-intake">
              <p className="muted">
                路径需位于已批准的永久资料根。文件保持原位，目录不可用时 PDF
                也会不可用。
              </p>
              <div className="form-row">
                <label className="grow">
                  <span>PDF 完整路径 *</span>
                  <input
                    value={referencePath}
                    onChange={(e) => setReferencePath(e.target.value)}
                    placeholder="D:/Research/paper.pdf"
                  />
                </label>
                <label className="grow">
                  <span>题名 *</span>
                  <input
                    value={referenceTitle}
                    onChange={(e) => setReferenceTitle(e.target.value)}
                  />
                </label>
              </div>
              <button
                type="button"
                className="button secondary compact"
                disabled={!referencePath.trim() || !referenceTitle.trim()}
                onClick={() => {
                  setRows((current) => [
                    ...current,
                    {
                      key: crypto.randomUUID(),
                      referencePath: referencePath.trim(),
                      title: referenceTitle.trim(),
                      metadata: optionalMetadata,
                      action: "create",
                      attachId: "",
                      busy: false,
                    },
                  ]);
                  setReferencePath("");
                  setReferenceTitle("");
                }}
              >
                加入待入库列表
              </button>
            </div>
          )}
          <details>
            <summary>补充信息（全部选填）</summary>
            <div className="advanced-filter-grid">
              <label>
                <span>作者（逗号分隔）</span>
                <input
                  value={authors}
                  onChange={(e) => editMetadata({ authors: e.target.value })}
                />
              </label>
              <label>
                <span>年份</span>
                <input
                  type="number"
                  min="1450"
                  max="2200"
                  value={year}
                  onChange={(e) => editMetadata({ year: e.target.value })}
                />
              </label>
              <label>
                <span>DOI（单篇）</span>
                <input
                  disabled={rows.length > 1}
                  value={doi}
                  onChange={(e) => editMetadata({ doi: e.target.value })}
                />
              </label>
              <label>
                <span>arXiv ID（单篇）</span>
                <input
                  disabled={rows.length > 1}
                  value={arxiv}
                  onChange={(e) => editMetadata({ arxiv: e.target.value })}
                />
              </label>
              <label>
                <span>标签（逗号分隔）</span>
                <input value={tags} onChange={(e) => editMetadata({ tags: e.target.value })} />
              </label>
            </div>
            <label>
              <span>简介</span>
              <textarea
                rows={3}
                value={summary}
                onChange={(e) => editMetadata({ summary: e.target.value })}
              />
            </label>
            <label>
              <span>HTML URL（单篇，选填）</span>
              <input
                type="url"
                disabled={rows.length > 1}
                value={htmlUrl}
                onChange={(e) => editMetadata({ htmlUrl: e.target.value })}
                placeholder="https://…"
              />
            </label>
            <label>
              <span>代码仓库 URL（单篇）</span>
              <input
                type="url"
                disabled={rows.length > 1}
                value={codeUrl}
                onChange={(e) => setCodeUrl(e.target.value)}
                placeholder="https://github.com/…"
              />
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={downloadCode}
                onChange={(e) => setDownloadCode(e.target.checked)}
              />
              <span>入库后下载代码快照</span>
            </label>
            {rows.length > 1 && (
              <p className="muted">
                批量导入共享领域、作者、年份、标签与简介；题名逐篇确认。
              </p>
            )}
          </details>
        </fieldset>
      </section>
      {!!rows.length && (
        <section className="section-block">
          <div className="section-heading">
            <h2>待入库 · {rows.length} 篇</h2>
            <div className="button-row">
              <button
                type="button"
                className="button secondary"
                disabled={batchBusy || rows.some((r) => r.busy) || !domain}
                onClick={() => void prepareAll()}
              >
                全部预览
              </button>
              <button
                type="button"
                className="button primary"
                disabled={
                  batchBusy ||
                  rows.some((r) => r.busy) ||
                  !rows.some((r) => r.preview && !r.result)
                }
                onClick={() => void commitAll()}
              >
                确认已预览项目
              </button>
            </div>
          </div>
          <div className="intake-rows">
            {rows.map((row) => (
              <article key={row.key} className="intake-row">
                <div className="form-row">
                  <label className="grow">
                    <span>题名 *</span>
                    <input
                      required
                      disabled={
                        batchBusy ||
                        row.busy ||
                        !!row.result ||
                        row.draft?.status === "committing"
                      }
                      value={row.title}
                      onChange={(e) =>
                        patch(row.key, {
                          title: e.target.value,
                          preview: undefined,
                        })
                      }
                    />
                  </label>
                  <button
                    type="button"
                    className="icon-button"
                    disabled={batchBusy || row.busy}
                    onClick={() => void remove(row)}
                    aria-label={`移除 ${row.title}`}
                  >
                    <X size={18} />
                  </button>
                </div>
                <p className="muted">
                  {row.file?.name ?? row.draft?.filename ?? "已保存草稿"} ·{" "}
                  {row.result ? "已入库" : (row.draft?.status ?? "尚未预览")}
                </p>
                {row.preview && (
                  <div className="intake-preview">
                    <p>
                      已检查 PDF 与元数据。
                      {row.preview.duplicates.length
                        ? `发现 ${row.preview.duplicates.length} 个重复候选，请选择如何保存。`
                        : "未发现重复候选。"}
                    </p>
                    {row.preview.warnings.map((warning, i) => (
                      <p className="inline-warning" key={i}>
                        {typeof warning === "string"
                          ? warning
                          : (warning.detail ?? warning.message)}
                      </p>
                    ))}
                    {!!row.preview.duplicates.length && (
                      <label>
                        <span>保存方式</span>
                        <select
                          disabled={
                            batchBusy ||
                            row.busy ||
                            row.draft?.status === "committing"
                          }
                          value={
                            !row.reviewed
                              ? ""
                              : row.action === "create"
                                ? "create"
                                : row.attachId
                          }
                          onChange={(e) =>
                            patch(
                              row.key,
                              e.target.value === "create"
                                ? {
                                    action: "create",
                                    attachId: "",
                                    reviewed: true,
                                  }
                                : {
                                    action: "attach",
                                    attachId: e.target.value,
                                    reviewed: !!e.target.value,
                                  },
                            )
                          }
                        >
                          <option value="">请选择保存方式</option>
                          <option value="create">创建独立论文</option>
                          {row.preview.duplicates.map((d) => (
                            <option
                              key={d.asset_id ?? d.id}
                              value={d.asset_id ?? d.id}
                            >
                              附加到：{d.title ?? d.asset_id ?? d.id}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}
                  </div>
                )}
                {row.error && (
                  <p className="inline-error" role="alert">
                    {row.error}
                  </p>
                )}
                {row.result ? (
                  <Link
                    className="button secondary compact"
                    to={`/assets/${row.result}`}
                  >
                    打开已入库论文
                  </Link>
                ) : (
                  <div className="button-row">
                    <button
                      type="button"
                      className="button secondary compact"
                      disabled={
                        batchBusy || row.busy || !domain || !row.title.trim()
                      }
                      onClick={() => void prepare(row)}
                    >
                      {row.busy ? "处理中…" : "预览与重复检查"}
                    </button>
                    {row.preview && (
                      <button
                        type="button"
                        className="button primary compact"
                        disabled={
                          row.busy ||
                          !row.reviewed ||
                          (row.action === "attach" && !row.attachId)
                        }
                        onClick={() => void commit(row)}
                      >
                        确认入库
                      </button>
                    )}
                  </div>
                )}
              </article>
            ))}
          </div>
        </section>
      )}
      {!!drafts.data?.items.filter(
        (d) => !["committed", "cancelled"].includes(d.status),
      ).length && (
        <section className="section-block">
          <h2>已保存草稿</h2>
          <div className="domain-mini-list">
            {drafts.data.items
              .filter((d) => !["committed", "cancelled"].includes(d.status))
              .map((d) => (
                <button
                  type="button"
                  key={d.id}
                  disabled={batchBusy || rows.some((row) => row.busy || row.draft?.id === d.id)}
                  onClick={() => {
                    setDomain(d.domain);
                    setStorageMode(d.storage_mode ?? "managed");
                    setOptionalMetadata(draftMetadata(d));
                    if (!rows.some((r) => r.draft?.id === d.id))
                      setRows((current) => [
                        ...current,
                        {
                          key: d.id,
                          title: d.title ?? "",
                          metadata: draftMetadata(d),
                          draft: d,
                          referencePath:
                            typeof d.reference_path === "string"
                              ? d.reference_path
                              : undefined,
                          action: d.commit_plan?.action ?? "create",
                          attachId: d.commit_plan?.asset_id ?? "",
                          busy: false,
                        },
                      ]);
                  }}
                >
                  <span>{d.title ?? d.filename ?? d.id}</span>
                  <span>{d.status} · 继续编辑</span>
                </button>
              ))}
          </div>
        </section>
      )}
      <p className="muted">
        抽取与关键词索引在入库后继续运行，可在 <Link to="/jobs">后台任务</Link>{" "}
        查看失败原因并重试。
      </p>
      {createDomain && (
        <DomainEditor
          onClose={() => setCreateDomain(false)}
          onSaved={(d) => setDomain(d.slug)}
        />
      )}
    </div>
  );
}
