export function searchDefinitionToUrl(definition: Record<string, unknown>) {
  const query = new URLSearchParams();
  Object.entries(normalizeSearchDefinition(definition)).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "")
      query.set(key, String(value));
  });
  return `/search?${query}`;
}
export function normalizeSearchDefinition(definition: Record<string, unknown>) {
  return Object.fromEntries(
    Object.entries(definition)
      .filter(([key, value]) => key !== "cursor" && key !== "has_guide" && value !== undefined && value !== null && value !== "")
      .map(([key, value]) => [
        key,
        key.startsWith("has_")
          ? value === true || value === "true"
          : key === "year" || key === "year_from" || key === "year_to"
            ? Number(value)
            : value,
      ]),
  );
}
