"""Fixed-clock feasibility and revision safety, including misleading capacity-only cases."""

from copy import deepcopy
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from relay_core.api import create_app
from relay_core.engine import apply, seed
from relay_core.planner import solve


def scenario():
    s = seed()
    return s, datetime.fromisoformat(s["logistics"]["starts_at"])


def test_schedule_conserves_load_and_respects_every_window():
    s, at = scenario()
    s["resources"][3]["capacity"] = 240
    result = solve(s, at)
    assert result["ok"]
    assert sum(leg["kg"] for leg in result["legs"]) == 320
    for route in result["routes"]:
        assert route["loaded_minute"] <= s["logistics"]["pickup"]["closes"]
        assert (
            route["finish_minute"]
            <= s["logistics"]["participants"][route["driver"]]["window"]["closes"]
        )
        for stop in route["stops"]:
            window = s["logistics"]["participants"][stop["recipient"]]["window"]
            assert stop["service_minute"] >= window["opens"]
            assert stop["finish_minute"] <= window["closes"]
    for resource in s["resources"]:
        field = "driver" if resource["role"] == "driver" else "recipient"
        assert (
            sum(leg["kg"] for leg in result["legs"] if leg[field] == resource["id"])
            <= resource["capacity"]
        )


def test_enough_capacity_does_not_override_closed_receiving_window():
    s, at = scenario()
    s["logistics"]["participants"]["harbour"]["window"]["closes"] = 40
    result = solve(s, at)
    assert result["code"] == "NO_FEASIBLE_ROUTE"


def test_uses_alternative_recipient_instead_of_greedy_dead_end():
    s, at = scenario()
    s["logistics"]["participants"]["harbour"]["window"]["closes"] = 40
    s["resources"][4]["capacity"] = 320
    result = solve(s, at)
    assert result["ok"]
    assert {leg["recipient"] for leg in result["legs"]} == {"garden"}


def test_tries_stop_order_that_satisfies_early_recipient():
    s, at = scenario()
    s["resources"][1]["capacity"] = 320
    s["resources"][2]["available"] = False
    s["resources"][3]["capacity"] = 240
    s["logistics"]["travel_minutes"]["pickup"]["garden"] = 5
    s["logistics"]["participants"]["garden"]["window"]["closes"] = 45
    result = solve(s, at)
    assert result["ok"]
    assert [x["recipient"] for x in result["routes"][0]["stops"]] == ["garden", "harbour"]


def test_pickup_loading_must_finish_before_donor_closes():
    s, at = scenario()
    s["logistics"]["pickup"]["closes"] = 25
    assert solve(s, at)["code"] == "NO_FEASIBLE_ROUTE"


def test_driver_window_includes_final_unloading():
    s, at = scenario()
    s["logistics"]["participants"]["tunde"]["window"]["closes"] = 54
    assert solve(s, at)["code"] == "NO_FEASIBLE_ROUTE"


def test_unknown_travel_and_missing_handling_do_not_become_guesses():
    s, at = scenario()
    del s["logistics"]["travel_minutes"]["harbour"]["garden"]
    assert solve(s, at)["code"] == "MISSING_TRAVEL"
    s, at = scenario()
    s["logistics"]["participants"]["tunde"]["handling"] = []
    result = solve(s, at)
    assert result["code"] == "INSUFFICIENT_CAPACITY"
    assert "handling" in result["reasons"][0]


def test_arrival_can_wait_until_recipient_opens():
    s, at = scenario()
    s["logistics"]["participants"]["harbour"]["window"]["opens"] = 100
    result = solve(s, at)
    assert result["ok"]
    assert all(r["stops"][0]["service_minute"] == 100 for r in result["routes"])


def test_changed_logistics_invalidates_all_confirmations():
    s, _ = scenario()
    apply(s, {"action": "propose"})
    apply(s, {"action": "accept", "revision": 1, "resource_id": "tunde"})
    logistics = deepcopy(s["logistics"])
    logistics["travel_minutes"]["pickup"]["harbour"] = 25
    assert apply(s, {"action": "set_logistics", "logistics": logistics})["ok"]
    assert s["plan"] is None
    assert apply(s, {"action": "propose"})["ok"]
    assert s["plan"]["accepted"] == []
    assert (
        apply(s, {"action": "accept", "revision": 1, "resource_id": "tunde"})["code"]
        == "STALE_REVISION"
    )


def test_dispatch_cannot_use_route_after_departure(monkeypatch):
    s, at = scenario()
    monkeypatch.setattr("relay_core.engine.now", lambda: at)
    apply(s, {"action": "propose"})
    for rid in s["plan"]["required"]:
        apply(s, {"action": "accept", "revision": 1, "resource_id": rid})
    departure = datetime.fromisoformat(s["plan"]["expires_at"])
    monkeypatch.setattr("relay_core.engine.now", lambda: departure)
    assert apply(s, {"action": "approve", "revision": 1})["code"] == "EXPIRED_PROPOSAL"
    assert apply(s, {"action": "propose"})["ok"]
    assert s["plan"]["revision"] == 2
    assert s["plan"]["accepted"] == []


def test_old_saved_workspace_cannot_skip_feasibility():
    s, at = scenario()
    del s["logistics"]
    assert solve(s, at)["code"] == "MISSING_LOGISTICS"


def test_inputs_and_active_logistics_are_protected(tmp_path):
    client = TestClient(create_app(str(tmp_path / "api.sqlite3"), rehearsal=True))
    s = client.post("/workspaces").json()
    path = f"/workspaces/{s['id']}/commands"
    data = deepcopy(s["logistics"])
    data["pickup"]["closes"] = data["pickup"]["opens"]
    assert client.post(path, json={"action": "set_logistics", "logistics": data}).status_code == 422
    data = deepcopy(s["logistics"])
    data["travel_minutes"]["tunde"]["pickup"] = True
    assert client.post(path, json={"action": "set_logistics", "logistics": data}).status_code == 422
    data = deepcopy(s["logistics"])
    data["starts_at"] = (datetime.fromisoformat(data["starts_at"]) + timedelta(hours=1)).isoformat()
    assert client.post(path, json={"action": "set_logistics", "logistics": data}).status_code == 409
    p = client.post(path, json={"action": "propose"}).json()["state"]["plan"]
    for rid in p["required"]:
        client.post(path, json={"action": "accept", "revision": 1, "resource_id": rid})
    assert client.post(path, json={"action": "approve", "revision": 1}).status_code == 200
    assert (
        client.post(path, json={"action": "set_logistics", "logistics": s["logistics"]}).status_code
        == 409
    )


def test_legacy_plan_cannot_commit_without_route_verification():
    s, _ = scenario()
    apply(s, {"action": "propose"})
    del s["plan"]["routes"]
    assert (
        apply(s, {"action": "accept", "revision": 1, "resource_id": "tunde"})["code"]
        == "UNVERIFIED_ROUTE"
    )
    assert apply(s, {"action": "propose"})["ok"]
    assert s["plan"]["routes"]
    assert s["plan"]["revision"] == 2


def test_closed_pickup_on_replanning_retires_expired_plan(monkeypatch):
    s, at = scenario()
    monkeypatch.setattr("relay_core.engine.now", lambda: at)
    apply(s, {"action": "propose"})
    monkeypatch.setattr("relay_core.engine.now", lambda: at + timedelta(minutes=100))
    assert apply(s, {"action": "propose"})["code"] == "NO_FEASIBLE_ROUTE"
    assert s["plan"] is None
    assert len(s["plans"]) == 1


def test_network_limit_is_escalation_not_an_impossibility_claim():
    s, at = scenario()
    for index in range(2):
        d = deepcopy(s["resources"][1])
        d["id"] = f"extra-{index}"
        s["resources"].append(d)
        s["logistics"]["participants"][d["id"]] = deepcopy(s["logistics"]["participants"]["tunde"])
    assert solve(s, at)["code"] == "PLANNER_LIMIT"
