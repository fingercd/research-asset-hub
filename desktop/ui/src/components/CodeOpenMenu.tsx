import {
  Code2,
  FolderOpen,
  LoaderCircle,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type {
  QuickAction,
  QuickActionAlternative,
  QuickActionStatus,
  RepresentationOpenAction,
} from "../api/types";
import { CodeBrowser } from "./CodeBrowser";

interface CodeChoice {
  representationId: string;
  label: string;
  role?: string | null;
  relationType?: string | null;
  status: QuickActionStatus;
  isDefault: boolean;
}

function alternativeLabel(alternative: QuickActionAlternative, index: number): string {
  return (
    alternative.label?.trim() ||
    alternative.role?.trim() ||
    `备用入口 ${index + 1}`
  );
}

function buildChoices(action: QuickAction): CodeChoice[] {
  const choices: CodeChoice[] = [];
  if (action.representation_id) {
    choices.push({
      representationId: action.representation_id,
      label: "默认代码入口",
      relationType: action.relation_type,
      status: action.status,
      isDefault: true,
    });
  }
  (action.alternatives ?? []).forEach((alternative, index) => {
    if (!alternative.representation_id) return;
    choices.push({
      representationId: alternative.representation_id,
      label: alternativeLabel(alternative, index),
      role: alternative.role,
      relationType: alternative.relation_type,
      status: alternative.status,
      isDefault: false,
    });
  });
  return choices;
}

export function CodeOpenMenu({
  repositoryLabel,
  action,
  onClose,
}: {
  repositoryLabel: string;
  action: QuickAction;
  onClose: () => void;
}) {
  const choices = useMemo(() => buildChoices(action), [action]);
  const firstChoice = choices.find((choice) => choice.status === "available") ?? choices[0];
  const [selectedId, setSelectedId] = useState(
    firstChoice?.representationId ?? action.representation_id ?? "",
  );
  const [openAction, setOpenAction] = useState<RepresentationOpenAction | null>(null);
  const [loading, setLoading] = useState(Boolean(selectedId));
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedId) {
      setOpenAction(null);
      setLoading(false);
      return;
    }
    const selectedChoice = choices.find(
      (choice) => choice.representationId === selectedId,
    );
    if (selectedChoice && selectedChoice.status !== "available") {
      setOpenAction(null);
      setLoading(false);
      setError(
        selectedChoice.status === "network_required"
          ? "源码尚未本地化，按本地代码策略暂不打开外部网页。"
          : "该入口没有可用的本地源码。",
      );
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    void api
      .openRepresentation(selectedId)
      .then((payload) => {
        if (cancelled) return;
        setOpenAction(payload);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : "无法获取打开方式");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [choices, selectedId]);

  useEffect(() => {
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div
      className="code-open-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section
        className="code-open-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="代码打开方式"
      >
        <header className="code-open-header">
          <div>
            <p className="eyebrow">代码项目</p>
            <h2>{repositoryLabel}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label="关闭"
            title="关闭"
            onClick={onClose}
          >
            <X size={16} aria-hidden="true" />
          </button>
        </header>

        {choices.length === 0 ? (
          <div className="code-open-empty">
            <FolderOpen size={28} aria-hidden="true" />
            <strong>该仓库未提供源码</strong>
            <p>论文可能未开源，或还没有登记可用的代码入口。</p>
          </div>
        ) : (
          <>
            {choices.length > 1 && (
              <div className="code-open-choices" role="group" aria-label="代码入口">
                {choices.map((choice) => (
                  <button
                    type="button"
                    className={choice.representationId === selectedId ? "is-selected" : ""}
                    aria-label={`选择代码入口：${choice.label}`}
                    key={choice.representationId}
                    onClick={() => setSelectedId(choice.representationId)}
                  >
                    <Code2 size={14} aria-hidden="true" />
                    <span>{choice.label}</span>
                    {choice.status === "network_required" && <small>需联网</small>}
                  </button>
                ))}
              </div>
            )}

            {loading ? (
              <div className="code-open-loading">
                <LoaderCircle size={18} className="spin" aria-hidden="true" />
                <span>正在检测本地代码项目…</span>
              </div>
            ) : error ? (
              <div className="code-open-unavailable">
                <FolderOpen size={28} aria-hidden="true" />
                <strong>本地代码不可用</strong>
                <p>{error}</p>
              </div>
            ) : openAction ? (
              <CodeBrowser
                representationId={selectedId}
                repositoryLabel={repositoryLabel}
                openAction={openAction}
                compact
              />
            ) : null}
          </>
        )}
      </section>
    </div>
  );
}
