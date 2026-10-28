import { useEffect, useRef } from "react";

/** Keep keyboard navigation inside an open dialog and return focus to its trigger. */
export function useDialogFocus(open = true) {
  const ref = useRef<HTMLElement>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!open || !dialog) return;
    const trigger = document.activeElement;
    const focusable = () => Array.from(dialog.querySelectorAll<HTMLElement>("*"))
      .filter((element) => element.matches("button, a[href], input, select, textarea, [tabindex]")
        && element.tabIndex >= 0 && !element.matches(":disabled"));
    focusable()[0]?.focus();
    const trapFocus = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const elements = focusable();
      const first = elements[0];
      const last = elements.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    dialog.addEventListener("keydown", trapFocus);
    return () => {
      dialog.removeEventListener("keydown", trapFocus);
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
    };
  }, [open]);

  return ref;
}
