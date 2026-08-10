from __future__ import annotations

import asyncio
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


logger = logging.getLogger(__name__)


# ============================================================
# BLOCK: BALE_ACCOUNT_SCOPED_RUNTIME_EXECUTION
# PURPOSE:
# Pins every persistent Bale authentication session to one account-owned thread.
# ACCOUNT_SCOPE:
# One executor and one stable execution thread per active Bale account.
# DEPENDENCIES:
# asyncio, ThreadPoolExecutor
# LAYER:
# MANAGER
# ============================================================


class BaleAccountExecutorError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class BaleAccountExecutorRegistry:
    def __init__(self) -> None:
        self._executors: dict[str, ThreadPoolExecutor] = {}
        self._account_by_session: dict[str, str] = {}
        self._lock = threading.RLock()

    # FUNCTION:
    # execute_open
    #
    # RESPONSIBILITY:
    # Creates an authentication session on the account's stable execution thread.
    #
    # INPUT:
    # account_id, synchronous authentication callable, callable arguments
    #
    # OUTPUT:
    # Authentication session payload
    #
    # SIDE EFFECTS:
    # Creates an account-scoped executor and registers session ownership.
    async def execute_open(
        self,
        account_id: str,
        function: Callable[..., dict[str, Any]],
        *args: Any,
    ) -> dict[str, Any]:
        try:
            result = await self._execute(account_id, "open", function, *args)
        except Exception:
            self._release_account(account_id, "")
            raise
        session_id = str(result.get("maintenance_session_id") or "")
        if session_id:
            with self._lock:
                self._account_by_session[session_id] = account_id
        return result

    # FUNCTION:
    # execute_session_operation
    #
    # RESPONSIBILITY:
    # Runs status, verify, or close on the thread that opened the session.
    #
    # INPUT:
    # maintenance_session_id, operation, synchronous callable, callable arguments
    #
    # OUTPUT:
    # Lifecycle operation payload
    #
    # SIDE EFFECTS:
    # Close removes the session mapping and shuts down only its account executor.
    async def execute_session_operation(
        self,
        maintenance_session_id: str,
        operation: str,
        function: Callable[..., dict[str, Any]],
        *args: Any,
    ) -> dict[str, Any]:
        with self._lock:
            account_id = self._account_by_session.get(maintenance_session_id)
        if not account_id:
            raise BaleAccountExecutorError(
                "maintenance_session_not_found",
                "Bale authentication maintenance session was not found",
            )

        result = await self._execute(
            account_id,
            operation,
            function,
            *args,
            session_id=maintenance_session_id,
        )
        if operation == "close":
            self._release_account(account_id, maintenance_session_id)
        return result

    # FUNCTION:
    # shutdown_all
    #
    # RESPONSIBILITY:
    # Releases executor resources during tests or application shutdown.
    #
    # INPUT:
    # None
    #
    # OUTPUT:
    # None
    #
    # SIDE EFFECTS:
    # Stops all account executor threads without altering browser profiles.
    def shutdown_all(self) -> None:
        with self._lock:
            executors = list(self._executors.values())
            self._executors.clear()
            self._account_by_session.clear()
        for executor in executors:
            executor.shutdown(wait=True)

    async def _execute(
        self,
        account_id: str,
        operation: str,
        function: Callable[..., dict[str, Any]],
        *args: Any,
        session_id: str = "",
    ) -> dict[str, Any]:
        executor = self._executor_for(account_id)
        future = executor.submit(
            self._run_with_diagnostics,
            account_id,
            operation,
            session_id,
            function,
            args,
        )
        return await asyncio.wrap_future(future)

    def _executor_for(self, account_id: str) -> ThreadPoolExecutor:
        with self._lock:
            executor = self._executors.get(account_id)
            if executor is None:
                executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix=f"bale-runtime-{account_id}",
                )
                self._executors[account_id] = executor
            return executor

    @staticmethod
    def _run_with_diagnostics(
        account_id: str,
        operation: str,
        session_id: str,
        function: Callable[..., dict[str, Any]],
        args: tuple[Any, ...],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        result_label = "FAILED"
        resolved_session_id = session_id
        try:
            result = function(*args)
            resolved_session_id = str(result.get("maintenance_session_id") or session_id)
            result_label = "SUCCESS"
            return result
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "[BALE_RUNTIME] account_id=%s thread_id=%s operation=%s "
                "session_id=%s duration_ms=%s result=%s",
                account_id,
                threading.get_ident(),
                operation,
                resolved_session_id,
                duration_ms,
                result_label,
            )

    def _release_account(self, account_id: str, maintenance_session_id: str) -> None:
        with self._lock:
            self._account_by_session.pop(maintenance_session_id, None)
            if account_id in self._account_by_session.values():
                return
            executor = self._executors.pop(account_id, None)
        if executor is not None:
            executor.shutdown(wait=True)


bale_account_executor_registry = BaleAccountExecutorRegistry()


# ============================================================
# END BLOCK: BALE_ACCOUNT_SCOPED_RUNTIME_EXECUTION
# ============================================================
