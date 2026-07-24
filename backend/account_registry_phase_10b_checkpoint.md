# ClinicOS Phase 10B Account Registry Checkpoint

## Safety Baseline

- Task: `CLINICOS_PHASE_10B_CANONICAL_ACCOUNT_REGISTRY_BACKEND_FOUNDATION`
- Scope: additive backend account registry foundation.
- Protected scenario: `backend/modules/automation_engine/scenarios/bale/forward_channel_messages.json`
- Protected scenario SHA256 before changes: `c21e891dc62fde69b7aaa0c9c3bf585a09529b6598835af831995278a1440ba0`
- Required protected scenario SHA256: `c21e891dc62fde69b7aaa0c9c3bf585a09529b6598835af831995278a1440ba0`
- Real sends authorized: no
- Forwarding/authentication/browser identity changes authorized: no

## Pre-Existing Dirty Backend Files

These files were already dirty before Phase 10B implementation and must not be reverted or cleaned by this phase:

- `backend/app/routes/automation.py`
- `backend/modules/automation_engine/browser/browser_manager.py`
- `backend/modules/automation_engine/browser_identity/__init__.py`
- `backend/modules/automation_engine/browser_identity/resolver.py`
- `backend/modules/automation_engine/commercial_queue/account_health.py`
- `backend/modules/automation_engine/commercial_queue/bale_authentication.py`
- `backend/modules/automation_engine/commercial_queue/errors.py`
- `backend/modules/automation_engine/commercial_queue/repository.py`
- `backend/modules/automation_engine/commercial_queue/service.py`
- `backend/modules/automation_engine/platforms/bale_adapter.py`
- `backend/modules/automation_engine/plugins/bale/account_store.py`
- `backend/modules/automation_engine/plugins/bale/plugin.py`
- `backend/test_bale_plugin.py`
- `backend/test_commercial_recipient_provenance_and_dry_run.py`
- `backend/test_commercial_recipient_scenarios.py`
- `backend/test_commercial_worker.py`
- `backend/test_platform_scenario_runner.py`

Pre-existing untracked backend files/directories included:

- `backend/modules/automation_engine/bale_bulk_orchestrator.py`
- `backend/modules/automation_engine/browser_identity/bale_profile_contract.py`
- `backend/test_bale_bulk_orchestrator.py`
- `backend/test_bale_profile_identity.py`
- `backend/test_diagnostics_ui.py`

## Existing Working Bale Paths

- Account listing: `GET /automation/platforms/bale/accounts` through `bale_account_store.list_accounts()`
- Account creation: `POST /automation/platforms/bale/accounts` through `bale_account_store.create_account(...)`
- Account editing: `PUT /automation/platforms/bale/accounts/{account_id}` through `bale_account_store.update_account(...)`
- Account deletion: `DELETE /automation/platforms/bale/accounts/{account_id}` through `bale_account_store.delete_account(...)`
- Manual login/open browser: `POST /automation/platforms/bale/accounts/{account_id}/open-login`
- Authentication check: `POST /automation/platforms/bale/accounts/{account_id}/check-login`
- Authentication maintenance workflow: `/automation/platforms/bale/authentication/*`
- Source channel storage: `bale_account_store.get_source_channel(...)` and `save_source_channel(...)`
- Forwarding endpoints: `/automation/platforms/bale/forward-*`, `/automation/platforms/bale/open-message-forward`, `/automation/platforms/bale/test-forward`
- Bulk Bale campaigns: `/automation/platforms/bale/bulk-campaigns*`

## Legacy Account Sources Identified

- `bale_account_store`: persistent Bale account source; must remain operational source for Bale.
- `platform_store`: in-memory generic platform account demo source.
- `commercial_account_settings`: commercial worker/account settings and counters; not an identity registry.
- `AccountManager`: legacy in-memory account-control source used by old dashboard/login flows.
- `worker.account_manager`: automation worker runtime account contexts.

## Phase 10B Boundary

The registry foundation must delegate Bale to the existing Bale store and add persistence only for non-Bale platform accounts. It must not merge accounts by phone number, rename Bale records, change browser profiles, change authentication flow, change forwarding selectors, or change campaign execution semantics.
