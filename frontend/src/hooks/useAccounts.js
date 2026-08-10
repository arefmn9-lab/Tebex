import { useCallback, useEffect, useMemo, useState } from "react";
import { listAccounts } from "../api/accounts";
import { listTasks } from "../api/automation";

export function useAccounts(intervalMs = 5000) {
  const [accounts, setAccounts] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const apiAccounts = await listAccounts();
      if (apiAccounts.length > 0) {
        setAccounts(apiAccounts);
      } else {
        const tasks = await listTasks();
        const grouped = new Map();
        tasks.forEach((task) => {
          const accountId = task.account_id || "default";
          const record = grouped.get(accountId) || {
            account_id: accountId,
            platform: "automation",
            status: "idle",
            active_tasks: 0,
            total_tasks: 0
          };
          record.total_tasks += 1;
          if (task.status === "running") {
            record.status = "active";
            record.active_tasks += 1;
          }
          grouped.set(accountId, record);
        });
        setAccounts([...grouped.values()]);
      }
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

  const activeAccounts = useMemo(
    () => accounts.filter((account) => account.status === "active").length,
    [accounts]
  );

  return { accounts, activeAccounts, error, refresh };
}
