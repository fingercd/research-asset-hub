import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { api, getApiErrorMessage } from "../api/client";
import type { Domain } from "../api/types";
import { useDialogFocus } from "../lib/useDialogFocus";

export function DomainEditor({
  domain,
  onClose,
  onSaved,
}: {
  domain?: Domain;
  onClose: () => void;
  onSaved?: (domain: Domain) => void;
}) {
  const dialogRef = useDialogFocus();
  const client = useQueryClient();
  const [label, setLabel] = useState(domain?.label ?? domain?.name ?? "");
  const [description, setDescription] = useState(domain?.description ?? "");
  const [aliases, setAliases] = useState(
    Array.isArray(domain?.aliases) ? domain.aliases.join(", ") : "",
  );
  const save = useMutation({
    mutationFn: () => {
      const body = {
        label: label.trim(),
        description: description.trim(),
        aliases: aliases
          .split(/[,，]/)
          .map((v) => v.trim())
          .filter(Boolean),
      };
      return domain
        ? api.updateDomain(domain.slug, body)
        : api.createDomain(body);
    },
    onSuccess: (result) => {
      void client.invalidateQueries({ queryKey: ["domains"] });
      void client.invalidateQueries({ queryKey: ["domain", result.slug] });
      onSaved?.(result);
      onClose();
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (label.trim()) save.mutate();
  }
  return (
    <div className="command-backdrop" onMouseDown={onClose}>
      <section
        ref={dialogRef}
        className="product-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="domain-editor-title"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <form onSubmit={submit}>
          <h2 id="domain-editor-title">{domain ? "编辑领域" : "新建领域"}</h2>
          <label>
            <span>领域名称 *</span>
            <input
              autoFocus
              required
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="例如：多模态学习"
            />
          </label>
          <label>
            <span>简介（可选）</span>
            <textarea
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label>
            <span>别名（可选，用逗号分隔）</span>
            <input
              value={aliases}
              onChange={(e) => setAliases(e.target.value)}
            />
          </label>
          <p className="muted">创建后可直接添加论文，精选门户可在后续补充。</p>
          {save.isError && (
            <p className="inline-error" role="alert">
              {getApiErrorMessage(save.error)}
            </p>
          )}
          <div className="form-actions">
            <button
              className="button secondary"
              type="button"
              onClick={onClose}
            >
              取消
            </button>
            <button
              className="button primary"
              disabled={save.isPending || !label.trim()}
            >
              {save.isPending ? "保存中…" : "保存领域"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
