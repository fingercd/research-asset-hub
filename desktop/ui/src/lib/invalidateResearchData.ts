import type { QueryClient } from "@tanstack/react-query";

export function invalidateResearchData(
  queryClient: QueryClient,
  options: {
    assetId?: string;
    annotations?: boolean;
    domains?: boolean;
  } = {},
) {
  if (options.assetId) {
    void queryClient.invalidateQueries({
      queryKey: ["asset", options.assetId],
    });
  }
  if (options.annotations) {
    void queryClient.invalidateQueries({ queryKey: ["annotations"] });
  }
  void queryClient.invalidateQueries({ queryKey: ["search"] });
  void queryClient.invalidateQueries({ queryKey: ["dashboard"] });
  void queryClient.invalidateQueries({ queryKey: ["assets"] });
  if (options.domains !== false) {
    void queryClient.invalidateQueries({ queryKey: ["domains"] });
    void queryClient.invalidateQueries({ queryKey: ["domain"] });
    void queryClient.invalidateQueries({ queryKey: ["domain-portal"] });
  }
}
