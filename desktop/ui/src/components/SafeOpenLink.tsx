import { ExternalLink } from "lucide-react";

export function SafeOpenLink({
  url,
  label = "安全打开",
}: {
  url: string;
  label?: string;
}) {
  return (
    <a className="button secondary" href={url} target="_blank" rel="noreferrer">
      <ExternalLink size={15} aria-hidden="true" />
      {label}
    </a>
  );
}
