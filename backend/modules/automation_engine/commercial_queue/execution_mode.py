from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

REAL_SEND = "real_send"
TEST_EXECUTION_MODES = {"mock_only", "controlled_live_no_send", "simulation"}


class ExecutionModeError(ValueError):
    def __init__(self, execution_mode: str, source: str) -> None:
        super().__init__("Non-production execution modes require the explicit test execution environment flag")
        self.execution_mode = execution_mode
        self.source = source


# ============================================================
# BLOCK: COMMERCIAL_EXECUTION_MODE_RESOLVER
# PURPOSE:
# Resolves one explicit execution mode before campaign work reaches a worker.
# ACCOUNT_SCOPE:
# The resolved mode applies only to the current worker operation.
# DEPENDENCIES:
# Explicit test-environment authorization
# LAYER:
# SERVICE
# ============================================================

# FUNCTION:
# resolve_execution_mode
# RESPONSIBILITY:
# Returns real_send for production and permits simulation modes only in tests.
# INPUT:
# requested_mode, test_modes_enabled, source
# OUTPUT:
# A canonical execution-mode string.
# SIDE EFFECTS:
# Emits a safe execution-mode diagnostic.
def resolve_execution_mode(
    requested_mode: str | None = None,
    *,
    test_modes_enabled: bool = False,
    source: str = "campaign_scheduler",
) -> str:
    mode = str(requested_mode or REAL_SEND).strip().lower()
    if mode == REAL_SEND:
        resolved = REAL_SEND
    elif test_modes_enabled and mode in TEST_EXECUTION_MODES:
        resolved = mode
    else:
        raise ExecutionModeError(mode, source)
    logger.info("[EXECUTION_MODE] execution_mode=%s source=%s", resolved, source)
    return resolved


# ============================================================
# END BLOCK: COMMERCIAL_EXECUTION_MODE_RESOLVER
# ============================================================
