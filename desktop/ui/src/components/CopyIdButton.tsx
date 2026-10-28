import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";

async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

export function CopyIdButton({ assetId }: { assetId: string }) {
  const [copied, setCopied] = useState(false);
  const resetTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (resetTimer.current !== null) {
        window.clearTimeout(resetTimer.current);
      }
    },
    [],
  );

  return (
    <button
      className={`copy-id-toggle ${copied ? "is-copied" : ""}`}
      type="button"
      title={copied ? "已复制到剪贴板" : "复制资产 ID 到剪贴板"}
      aria-label={copied ? "已复制" : "复制ID"}
      onClick={() => {
        void copyText(assetId)
          .then(() => {
            setCopied(true);
            if (resetTimer.current !== null) {
              window.clearTimeout(resetTimer.current);
            }
            resetTimer.current = window.setTimeout(() => setCopied(false), 1600);
          })
          .catch(() => undefined);
      }}
    >
      {copied ? (
        <Check size={14} aria-hidden="true" />
      ) : (
        <Copy size={14} aria-hidden="true" />
      )}
      {copied ? "已复制" : "复制ID"}
    </button>
  );
}
