import { getRepresentationType, type Representation } from "../api/types";

export function preferGuide(
  representations: readonly Representation[],
): Representation | undefined {
  const guides = representations.filter(
    (item) =>
      item.availability === "available" &&
      (item.role === "guide" ||
        ["guide", "analysis", "markdown"].includes(getRepresentationType(item))),
  );
  return (
    guides.find(
      (item) =>
        getRepresentationType(item) === "html" &&
        item.role === "guide" &&
        item.attributes?.unified_guide === true,
    ) ?? guides[0]
  );
}
