from __future__ import annotations

import tempfile
from pathlib import Path

from modules.automation_engine.commercial_queue.repository import utc_now
from modules.automation_engine.commercial_queue.service import CampaignLifecycleError
from test_commercial_campaign_lifecycle import OrchestratorStub, _service


class ContactStoreStub:
    """Keep the multi-campaign lifecycle test above the real contact boundary."""

    def __init__(self, repository) -> None:
        self.repository = repository

    @staticmethod
    def _display_name(phone: str) -> str:
        return f"Fixture-{phone[-4:]}"

    def get_platform_contact(self, _platform: str, phone: str) -> dict[str, str]:
        return {"display_name": self._display_name(phone)}

    def ensure_stable_mapping(self, phone: str, display_name: str, account_id: str) -> dict[str, str]:
        mapping = self.repository.get_or_create_stable_contact_mapping(phone, display_name)
        verified_at = utc_now()
        self.repository.upsert_account_contact_proof(
            {
                "account_id": account_id,
                "mapping_id": mapping["id"],
                "binding_id": f"fixture-binding:{account_id}:{mapping['id']}",
                "normalized_phone": phone,
                "recipient_display_name": display_name,
                "preparation_status": "prepared",
                "verification_status": "verified",
                "verification_method": "isolated_fixture",
                "prepared_at": verified_at,
                "verified_at": verified_at,
                "profile_identity": account_id,
                "last_successful_step": "fixture_contact_proof",
            }
        )
        return {"account_binding_id": f"fixture-binding:{account_id}:{mapping['id']}"}

    def get_or_create_bale_contact(self, _account_id: str, phone: str) -> tuple[dict[str, str], bool]:
        display_name = self._display_name(phone)
        return {"stable_name": display_name, "display_name": display_name}, False


def _create_ready_campaign(service, name: str, count: int, **policy_overrides: object) -> dict:
    source_uid = str(policy_overrides.pop("source_channel_uid", "5613544284"))
    campaign = service.create_campaign({
        "name": name,
        "platform": "bale",
        "status": "draft",
        "source_channel_uid": source_uid,
        "capacity_reservation": 0,
        "policy_overrides": {
            "selected_platforms": ["bale"],
            "platform_source_urls": {"bale": f"https://web.bale.ai/chat?uid={source_uid}"},
            "eligible_account_ids": ["bale_a"],
            "round_cooldown_seconds": 0,
            "delay_between_deliveries_seconds": 0,
            "daily_limit_per_account": 100,
            **policy_overrides,
        },
    })
    prefix = {"A": "09111", "B": "09222", "C": "09333"}[name]
    phones = [f"{prefix}{index:06d}" for index in range(count)]
    service.confirm_campaign_recipients(
        campaign["id"],
        phones,
        submitted_by="multi_campaign_test",
        source_type="test",
        confirmation_checked=True,
    )
    service.materialize_campaign_recipients(
        campaign["id"],
        ["bale"],
        authorize_for_live_execution=True,
        authorized_by="multi_campaign_test",
        authorization_note="mocked non-send lifecycle test",
    )
    for recipient in service.list_recipients(campaign["id"], limit=1000)["items"]:
        authorization = {
            "recipient_origin": "user_provided",
            "synthetic_test_data": False,
            "live_execution_authorized": True,
            "live_authorized_at": "2026-07-31T00:00:00+00:00",
            "live_authorized_by": "multi_campaign_test",
            "authorization_source": "test_fixture",
            "authorization_note": "mocked non-send lifecycle test",
            "authorization_status": "authorized",
            "should_not_retry": False,
        }
        service.repository.update_recipient_authorization(recipient["id"], authorization)
        service.repository.update_jobs_authorization_by_recipient(recipient["id"], authorization)
    return service.repository.get_campaign(campaign["id"])


def _review_and_payload(service, campaign_id: str, key: str) -> tuple[dict, dict]:
    before = service.validate_campaign_start(campaign_id)
    review = service.final_review(
        campaign_id,
        explicit_operator_confirmation=True,
        approved_by="multi_campaign_test",
    )
    assert before["ok"] is True, before
    assert review["ok"] is True
    assert review["approved"] is True
    return review, {
        "validation_hash": review["validation_hash"],
        "final_review_hash": review["final_review_hash"],
        "review_token": review["review_token"],
        "manifest_hash": review["confirmed_recipients_summary"]["manifest_hash"],
        "idempotency_key": key,
        "explicit_operator_confirmation": True,
        "expected_campaign_status": "draft",
    }


def _error_code(callback) -> str:
    try:
        callback()
    except CampaignLifecycleError as exc:
        return exc.error_code
    raise AssertionError("expected lifecycle error")


def test_three_campaigns_are_isolated_restart_safe_and_multi_round() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        database = Path(tmp_dir) / "campaign-independent.db"
        worker = OrchestratorStub()
        service = _service(database, worker)
        campaigns = {
            "A": _create_ready_campaign(service, "A", 3, deliveries_per_round=1, priority=5),
            "B": _create_ready_campaign(service, "B", 15, deliveries_per_round=10, priority=20),
            "C": _create_ready_campaign(service, "C", 1, deliveries_per_round=1, priority=1, scheduled_start_at=None),
        }

        reviews: dict[str, dict] = {}
        payloads: dict[str, dict] = {}
        for name, campaign in campaigns.items():
            reviews[name], payloads[name] = _review_and_payload(service, campaign["id"], f"queue-{name}")

        assert len({review["configuration_revision"]["revision_id"] for review in reviews.values()}) == 3
        assert len({review["execution_snapshot"]["snapshot_id"] for review in reviews.values()}) == 3
        assert len({review["review_token"] for review in reviews.values()}) == 3
        assert reviews["A"]["effective_policy"]["delivery"]["deliveries_per_round"] == 1
        assert reviews["B"]["effective_policy"]["delivery"]["deliveries_per_round"] == 10
        assert reviews["A"]["effective_policy"]["schedule"]["priority"] == 5
        assert reviews["B"]["effective_policy"]["schedule"]["priority"] == 20
        assert _error_code(lambda: service.queue_campaign(campaigns["B"]["id"], {
            **payloads["B"],
            "review_token": reviews["A"]["review_token"],
        })) == "final_review_proof_missing"

        old_a_token = reviews["A"]["review_token"]
        service.update_campaign(campaigns["A"]["id"], {
            "policy_overrides": {
                **reviews["A"]["effective_policy"].get("policy_overrides", {}),
                "selected_platforms": ["bale"],
                "platform_source_urls": {"bale": "https://web.bale.ai/chat?uid=5613544284"},
                "eligible_account_ids": ["bale_a"],
                "deliveries_per_round": 2,
                "daily_limit_per_account": 100,
                "delay_between_deliveries_seconds": 0,
                "round_cooldown_seconds": 0,
            },
        })
        reviews["A"], payloads["A"] = _review_and_payload(service, campaigns["A"]["id"], "queue-A-v2")
        assert reviews["A"]["review_token"] != old_a_token
        assert service.repository.get_campaign_final_review(campaigns["A"]["id"], old_a_token)["approved"] == 0
        assert service.repository.get_campaign_final_review(campaigns["B"]["id"], reviews["B"]["review_token"])["approved"] == 1

        for name, campaign in campaigns.items():
            queued = service.queue_campaign(campaign["id"], payloads[name])
            assert queued["queued"] is True
            assert len(service.repository.list_campaign_jobs_all(campaign["id"])) == {"A": 3, "B": 15, "C": 1}[name]
            assert service.queue_campaign(campaign["id"], payloads[name])["idempotency"]["status"] == "reused"

        reloaded_worker = OrchestratorStub()
        reloaded = _service(database, reloaded_worker)
        reloaded.contact_store = ContactStoreStub(reloaded.repository)
        for name, campaign in campaigns.items():
            assert reloaded.repository.get_campaign(campaign["id"])["status"] == "queued"
            assert reloaded.repository.get_campaign_final_review(campaign["id"], reviews[name]["review_token"]) is not None

        first_start = reloaded.start_campaign(campaigns["B"]["id"])
        second_start = reloaded.start_campaign(campaigns["B"]["id"])
        assert first_start["campaign"]["status"] == "running"
        assert second_start["idempotency"]["status"] == "reused"
        round_sizes = []
        while reloaded.repository.campaign_job_counts(campaigns["B"]["id"]).get("queued", 0):
            result = reloaded.run_account_round("bale_a", campaign_id=campaigns["B"]["id"])
            round_sizes.append(int(result.get("processed_count") or 0))
        # The scheduler/worker contract claims at most one active job for an
        # account at a time. A 15-recipient campaign therefore advances in 15
        # durable one-job rounds rather than pre-assigning a 10/5 batch to the
        # same account; the important regression is that it continues to
        # drain instead of hitting a lifetime diagnostic token and stalling.
        assert round_sizes == [1] * 15
        assert len(reloaded.repository.list_campaign_jobs_all(campaigns["B"]["id"])) == 15

        deleted = reloaded.delete_campaign(campaigns["C"]["id"])
        assert deleted["deleted"] is True
        assert deleted["soft_deleted"] is True
        assert reloaded.repository.get_campaign(campaigns["C"]["id"])["deleted_at"]
        assert reloaded.repository.get_campaign(campaigns["A"]["id"]) is not None
        assert reloaded.repository.get_campaign(campaigns["B"]["id"]) is not None
        assert reloaded_worker.calls
