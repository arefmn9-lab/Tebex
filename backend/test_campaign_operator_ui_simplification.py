from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "frontend" / "src" / "pages" / "CommercialCampaigns.jsx").read_text(encoding="utf-8")


def test_campaign_workflow_has_one_operator_account_allocation_control() -> None:
    assert "تعداد اکانت مورد نیاز این کمپین" in SOURCE
    assert "round_observability" not in SOURCE
    assert "مرورگر همزمان" not in SOURCE
    assert "کارگر همزمان" not in SOURCE
    assert "حداکثر اکانت همزمان" not in SOURCE
    assert "تعداد اکانت در هر دور" not in SOURCE
    assert "مکث بین دورها" not in SOURCE
    assert "مهلت هر کار" not in SOURCE


def test_capacity_input_is_preserved_while_refresh_data_arrives() -> None:
    assert "capacityInputDirty.current = true" in SOURCE
    assert "!capacityInputDirty.current && capacityInitializedCampaign.current !== expandedCampaignId" in SOURCE
    assert "invalid_selection_replaced" in SOURCE
    assert "stored_selection_cleared" in SOURCE
    assert "localStorage.removeItem(\"clinicos:selected-bale-campaign\")" in SOURCE
    assert "newCampaignMode.current = true" in SOURCE
    assert "if (newCampaignMode.current) return;" in SOURCE
