import os
from pathlib import Path
import tempfile

import pytest


# ============================================================
# BLOCK: TEST_EXECUTION_MODE_ENVIRONMENT
# PURPOSE:
# Explicitly enables non-sending execution modes only inside pytest.
# ACCOUNT_SCOPE:
# Test processes only.
# DEPENDENCIES:
# CommercialQueueService
# LAYER:
# TEST
# ============================================================

os.environ["CLINICOS_ENABLE_TEST_EXECUTION_MODES"] = "1"
os.environ["CLINICOS_DISABLE_SCHEDULER_RUNTIME"] = "1"
os.environ["CLINICOS_DISABLE_AUTH_MAINTENANCE_RUNTIME"] = "1"
os.environ["CLINICOS_TEST_MODE"] = "1"
_collection_isolation_root = Path(tempfile.mkdtemp(prefix="clinicos-pytest-collection-"))
os.environ["CLINICOS_DB_PATH"] = str((_collection_isolation_root / "isolated.db").resolve())
os.environ["CLINICOS_PROFILE_ROOT"] = str((_collection_isolation_root / "profiles").resolve())
os.environ["CLINICOS_AUTOMATION_DATABASE_PATH"] = os.environ["CLINICOS_DB_PATH"]
os.environ["CLINICOS_BALE_PROFILE_ROOT"] = os.environ["CLINICOS_PROFILE_ROOT"]
os.environ["CLINICOS_BALE_RUNTIME_DIR"] = str((_collection_isolation_root / "registry").resolve())


@pytest.fixture(autouse=True)
def _forbid_production_database_in_tests(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Every test gets an isolated DB unless it explicitly supplies another safe path."""
    production = (Path(__file__).resolve().parent / "clinicos.db").resolve()
    configured = Path(os.environ.get("CLINICOS_DB_PATH") or (tmp_path / "clinicos-test.db")).resolve()
    if configured == production or configured.name == "clinicos.db":
        raise AssertionError(f"pytest refused production database path: {configured}")
    monkeypatch.setenv("CLINICOS_DB_PATH", str(configured))
    monkeypatch.setenv("CLINICOS_PROFILE_ROOT", str((tmp_path / "profiles").resolve()))

# ============================================================
# END BLOCK: TEST_EXECUTION_MODE_ENVIRONMENT
# ============================================================
