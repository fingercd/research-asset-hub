import { useEffect, useState } from "react";
import { useLocation, useNavigate, useNavigationType } from "react-router-dom";

/** Only traverse pages visited inside this app, never the desktop loading document. */
export function usePageHistory() {
  const location = useLocation();
  const action = useNavigationType();
  const navigate = useNavigate();
  const [history, setHistory] = useState({ keys: [location.key], index: 0 });

  useEffect(() => {
    setHistory((current) => {
      if (current.keys[current.index] === location.key) return current;
      if (action === "POP") {
        const index = current.keys.indexOf(location.key);
        return index >= 0 ? { ...current, index } : { keys: [location.key], index: 0 };
      }
      if (action === "REPLACE") {
        const keys = [...current.keys];
        keys[current.index] = location.key;
        return { ...current, keys };
      }
      const index = current.index + 1;
      return { keys: [...current.keys.slice(0, index), location.key], index };
    });
  }, [location.key, action]);

  return {
    canGoBack: history.index > 0,
    canGoForward: history.index < history.keys.length - 1,
    back: () => navigate(-1),
    forward: () => navigate(1),
  };
}
