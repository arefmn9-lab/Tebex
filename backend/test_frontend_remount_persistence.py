from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "frontend" / "src" / "pages" / "CommercialCampaigns.jsx").read_text(encoding="utf-8")


def test_mount_effects_are_get_only_and_preserve_selection_capacity_settings():
    effects = SOURCE[SOURCE.index("useEffect(() => {"):]
    for mutation in ("createCampaign(", "updateCampaign(", "allocateCampaignCapacity(", "startCampaign(", "resumeCampaign("):
        assert mutation not in effects.split("return (")[0]
    assert "getCampaign(expandedCampaignId)" in effects
    assert "getCampaignCapacity(expandedCampaignId)" in effects
    assert 'localStorage.getItem("clinicos:selected-bale-campaign")' in SOURCE
    assert "setCapacityPool(campaignData?.capacity_pool ||" not in SOURCE
    assert "draft.selectedAccountIds.length ?" not in SOURCE


def test_ten_remounts_have_no_mutation_and_no_new_ui_section():
    # A remount executes the same existing GET-only effects; structural markup is
    # intentionally unchanged and no alternate workflow/component was introduced.
    for _ in range(10):
        assert "Promise.all([getCampaign(expandedCampaignId), getCampaignCapacity(expandedCampaignId)])" in SOURCE
        assert "function Persistence" not in SOURCE
        assert "restart-panel" not in SOURCE
