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
    refresh();
    const timer = window.setInterval(refresh, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, refresh]);

  return { logs, error, refresh };
}

