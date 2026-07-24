import { useMutation } from "@tanstack/react-query";
import { BotMessageSquare, ExternalLink, Send, Sparkles, User } from "lucide-react";
import { useState, type FormEvent, type KeyboardEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import type { ChatCitation } from "../api/types";
import { PageHeader } from "../components/PageHeader";

interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  citations?: ChatCitation[];
}

const prompts = [
  "这些资料中有哪些共同的研究假设？",
  "总结主要方法，并指出证据来源。",
  "有哪些值得继续阅读的相关论文？",
];

export function ChatPage() {
  const [searchParams] = useSearchParams();
  const scopedAsset = searchParams.get("asset_id");
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const chat = useMutation({
    mutationFn: (message: string) => api.chat(message, scopedAsset ? [scopedAsset] : []),
    onSuccess: (response) => {
      setMessages((current) => [
        ...current,
        {
          id: Date.now(),
          role: "assistant",
          content: response.answer,
          citations: response.citations,
        },
      ]);
    },
  });

  const send = () => {
    const message = input.trim();
    if (!message || chat.isPending) return;
    setMessages((current) => [
      ...current,
      { id: Date.now(), role: "user", content: message },
    ]);
    setInput("");
    chat.mutate(message);
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    send();
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      send();
    }
  };

  return (
    <div className="page chat-page">
      <PageHeader
        eyebrow="检索增强问答"
        title="向资料库提问"
        description={
          scopedAsset
            ? "当前问题限定在所选资产；回答会尽量附上可追溯引用。"
            : "基于本地索引生成回答，并保留引用资产，便于回到原文核验。"
        }
      />
      {scopedAsset && (
        <div className="notice">
          <Sparkles size={17} aria-hidden="true" />
          <div><strong>已限定一个资产</strong><span className="mono">{scopedAsset}</span></div>
          <Link to="/chat">清除限定</Link>
        </div>
      )}

      <section className="chat-panel" aria-label="问答对话">
        <div className="chat-messages" aria-live="polite">
          {!messages.length ? (
            <div className="chat-welcome">
              <span><BotMessageSquare size={28} aria-hidden="true" /></span>
              <h2>从一个具体问题开始</h2>
              <p>回答可能不完整，请通过引用回到原始论文、代码或导读核验。</p>
              <div>
                {prompts.map((prompt) => (
                  <button type="button" onClick={() => setInput(prompt)} key={prompt}>
                    {prompt}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((message) => (
              <article className={`chat-message is-${message.role}`} key={message.id}>
                <span className="message-avatar">
                  {message.role === "user" ? (
                    <User size={17} aria-hidden="true" />
                  ) : (
                    <BotMessageSquare size={17} aria-hidden="true" />
                  )}
                </span>
                <div>
                  <strong>{message.role === "user" ? "你" : "Research Hub"}</strong>
                  <p>{message.content}</p>
                  {!!message.citations?.length && (
                    <ol className="citation-list">
                      {message.citations.map((citation, index) => (
                        <li key={`${citation.asset_id ?? citation.url}-${index}`}>
                          {citation.asset_id ? (
                            <Link to={`/assets/${encodeURIComponent(citation.asset_id)}`}>
                              [{index + 1}] {citation.title ?? citation.asset_id}
                            </Link>
                          ) : citation.url ? (
                            <a href={citation.url} target="_blank" rel="noreferrer">
                              [{index + 1}] {citation.title ?? "外部来源"}{" "}
                              <ExternalLink size={12} aria-hidden="true" />
                            </a>
                          ) : (
                            <span>[{index + 1}] {citation.title ?? "引用"}</span>
                          )}
                          {citation.excerpt && <small>{citation.excerpt}</small>}
                        </li>
                      ))}
                    </ol>
                  )}
                </div>
              </article>
            ))
          )}
          {chat.isPending && (
            <article className="chat-message is-assistant is-pending">
              <span className="message-avatar"><BotMessageSquare size={17} aria-hidden="true" /></span>
              <div><strong>Research Hub</strong><p>正在检索资料并组织回答…</p></div>
            </article>
          )}
          {chat.isError && (
            <div className="chat-error" role="alert">
              <strong>回答失败</strong>
              <span>{getApiErrorMessage(chat.error)}</span>
            </div>
          )}
        </div>
        <form className="chat-composer" onSubmit={submit}>
          <label className="sr-only" htmlFor="chat-input">问题</label>
          <textarea
            id="chat-input"
            rows={3}
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="描述你要理解、比较或核验的问题…"
          />
          <div>
            <span>Ctrl + Enter 发送</span>
            <button className="button primary" type="submit" disabled={!input.trim() || chat.isPending}>
              <Send size={15} aria-hidden="true" /> 发送
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
