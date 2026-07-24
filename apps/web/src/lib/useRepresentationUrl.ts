import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import type { Representation } from "../api/types";
import { getOpenUrl } from "./format";

function directUrl(representation: Representation): string | null {
  return (
    representation.open_url ??
    representation.external_url ??
    representation.url ??
    null
  );
}

export function useRepresentationUrl(representation?: Representation) {
  return useQuery({
    queryKey: ["representation-url", representation?.id],
    queryFn: async () => {
      if (!representation) return null;
      const direct = directUrl(representation);
      if (direct) return direct;
      return getOpenUrl(await api.openRepresentation(representation.id));
    },
    enabled: Boolean(representation),
    staleTime: 4 * 60_000,
  });
}
