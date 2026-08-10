import { useCallback, useEffect, useState } from "react";
import { listLogs } from "../api/logs";

export function useLogs(intervalMs = 2500) {
  const [logs, setLogs] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const data = await listLogs();
      setLogs(Array.isArray(data) ? data : []);
      setError("");
    } catch (requestError) {
      setError(requestError.message);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer;
    const poll = async () => {
      if (!document.hidden) await refresh();
      if (!cancelled) timer = window.setTimeout(poll, intervalMs);
    };
    poll();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [intervalMs, refresh]);

  return { logs, error, refresh };
}
