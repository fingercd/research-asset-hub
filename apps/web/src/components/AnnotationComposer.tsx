import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { useState, type FormEvent } from "react";
import { api, getApiErrorMessage } from "../api/client";

export function AnnotationComposer({
  assetId,
  onCreated,
}: {
  assetId: string;
  onCreated?: () => void;
}) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState("comment");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      api.createAnnotation({
        asset_id: assetId,
        kind,
        title: title.trim() || null,
        body_markdown: body.trim(),
      }),
    onSuccess: () => {
      setTitle("");
      setBody("");
      void queryClient.invalidateQueries({ queryKey: ["annotations"] });
      void queryClient.invalidateQueries({ queryKey: ["asset", assetId] });
      onCreated?.();
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (body.trim()) mutation.mutate();
  };

  return (
    <form className="annotation-composer" onSubmit={submit}>
      <div className="form-row">
        <label>
          <span>类型</span>
          <select value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="comment">评论</option>
            <option value="summary">总结</option>
            <option value="question">问题</option>
            <option value="highlight">摘录</option>
            <option value="general">一般笔记</option>
          </select>
        </label>
        <label className="grow">
          <span>标题（可选）</span>
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="这条批注讲什么"
          />
        </label>
      </div>
      <label>
        <span>内容</span>
        <textarea
          value={body}
          onChange={(event) => setBody(event.target.value)}
          rows={4}
          placeholder="记录关键结论、疑问或后续行动…"
          required
        />
      </label>
      <div className="form-actions">
        {mutation.isError && (
          <span className="inline-error" role="alert">
            {getApiErrorMessage(mutation.error)}
          </span>
        )}
        <button
          className="button primary"
          type="submit"
          disabled={!body.trim() || mutation.isPending}
        >
          <Send size={15} aria-hidden="true" />
          {mutation.isPending ? "保存中…" : "保存批注"}
        </button>
      </div>
    </form>
  );
}
