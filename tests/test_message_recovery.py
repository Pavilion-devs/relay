from scripts.rehearse_message_recovery import intake
from scripts.rehearse_partial_replacement import run


def test_message_review_to_partial_replacement_and_receipt(tmp_path):
    result = run(tmp_path / "message-recovery.sqlite3", intake=intake)
    assert all(result["checks"].values())
    assert result["final_status"] == "complete"
    assert result["metrics"]["replacement_kg"] == 192
    assert result["metrics"]["preserved_kg"] == 128
