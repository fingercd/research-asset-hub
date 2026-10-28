import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Representation } from "../api/types";

export function useRepresentationUrl(representation?: Representation) {
  return useQuery({
    queryKey: ["representation-open-action", representation?.id],
    queryFn: async () => {
      if (!representation) return null;
      // The API decides between a signed local session and an external-only
      // source. Metadata URLs are provenance, not permission to bypass it.
      const action = await api.openRepresentation(representation.id);
      if (!action.url) throw new Error("安全打开服务未返回文件地址。");
      const protocol = new URL(action.url, window.location.href).protocol;
      if (protocol !== "https:" && protocol !== "http:")
        throw new Error("安全打开服务返回了不支持的地址类型。");
      return action;
    },
    enabled: Boolean(representation),
    staleTime: (query) => Math.min(4 * 60_000, (query.state.data?.expires_in ?? 300) * 800),
    retry: false,
  });
}
