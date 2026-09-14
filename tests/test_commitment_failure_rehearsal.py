"""Cross-component regression: custody failure through authenticated replacement receipts."""

import pytest

from scripts.rehearse_commitment_failure import run_scenario


@pytest.mark.parametrize(
    ('ending', 'status', 'reported', 'unresolved'),
    [
        ('complete', 'complete', 320, 0),
        ('short_receipt', 'discrepancy', 300, 20),
        ('missing_receipt', 'awaiting_receipt', 0, 320),
    ],
)
def test_committed_driver_failure_endings(tmp_path, ending, status, reported, unresolved):
    report = run_scenario(tmp_path / 'rehearsal.sqlite3', ending)
    assert all(report['checks'].values())
    assert report['final_status'] == status
    assert report['metrics']['recipient_reported_kg'] == reported
    assert report['metrics']['unresolved_kg'] == unresolved
    assert bool(report['active_reservations']) is (ending != 'complete')
