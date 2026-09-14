"""Single-driver amendments; unchanged driver commitments retain their provenance."""

from copy import deepcopy
from datetime import datetime, timedelta
from uuid import uuid4

ACTIONS = {
    "propose_replacement",
    "ack_stop",
    "confirm_return",
    "accept_replacement",
    "commit_replacement",
}


def apply(state, command):
    from .engine import event, now, reject, resource
    from .planner import solve

    p = state.get("plan")
    action = command["action"]
    if (
        not p
        or command.get("revision") != p["revision"]
        or state["status"] not in ("committed", "in_transit", "awaiting_receipt")
    ):
        return reject(
            state, "INVALID_REPLACEMENT_STATE", "An active current commitment is required."
        )
    if p["receipts"]:
        return reject(
            state, "RECONCILIATION_REQUIRED", "Receipt evidence needs separate reconciliation."
        )
    if action == "propose_replacement":
        failed = command.get("resource_id")
        candidate = command.get("replacement_id")
        old = next((d for d in p["drivers"] if d["resource_id"] == failed), None)
        new = resource(state, candidate)
        if (
            not old
            or not new
            or new["role"] != "driver"
            or candidate in p["required"]
            or not command.get("reason", "").strip()
        ):
            return reject(
                state,
                "INVALID_REPLACEMENT",
                "Name the failed driver, a different registered driver, and the reason.",
            )
        if command.get("available") is not True:
            return reject(
                state, "AVAILABILITY_REQUIRED", "Explicitly record replacement availability."
            )
        failure = state.get("replacement_failure")
        if failure and failure["driver"] != failed:
            return reject(state, "REPLACEMENT_PENDING", "Resolve the already failed driver first.")
        if not failure:
            state["replacement_failure"] = {
                "id": str(uuid4()),
                "driver": failed,
                "reason": command["reason"],
            }
            event(
                state,
                "Driver failure held",
                "Failed-driver pickup and recipient receipts are held until recovery.",
                "warning",
            )
        prior = state.get("replacement")
        if prior and now() < datetime.fromisoformat(prior["expires_at"]):
            return reject(
                state, "REPLACEMENT_PENDING", "Resolve the existing proposal before replacing it."
            )
        if prior and prior["failed_driver"] != failed:
            return reject(
                state, "REPLACEMENT_PENDING", "The stopped driver must be resolved first."
            )
        quantities = {}
        for leg in p["legs"]:
            if leg["driver"] == failed:
                quantities[leg["recipient"]] = quantities.get(leg["recipient"], 0) + leg["kg"]
        partial = deepcopy(state)
        partial["total_kg"] = old["kg"]
        partial["resources"] = [deepcopy(new)] + [
            deepcopy(resource(state, rid)) for rid in quantities
        ]
        partial["resources"][0]["available"] = True
        # Existing recipient capacity remains reserved by this workspace, unchanged.
        for dest in partial["resources"][1:]:
            dest["capacity"] = quantities[dest["id"]]
        partial["reservation_snapshot"] = {
            candidate: state.get("replacement_capacity", {}).get(candidate, new["capacity"]),
            **quantities,
        }
        result = solve(partial, now())
        if not result["ok"]:
            return reject(state, result["code"], result["message"])
        state.setdefault("replacement_history", [])
        if prior:
            state["replacement_history"].append({**deepcopy(prior), "status": "expired"})
        state["replacement"] = {
            "id": str(uuid4()),
            "base_revision": p["revision"],
            "failed_driver": failed,
            "replacement_driver": candidate,
            "kg": old["kg"],
            "reason": command["reason"],
            "source": command.get("source", "Structured replacement availability"),
            "required": [candidate, *sorted(quantities)],
            "accepted": [],
            "stopped": bool(prior and prior["stopped"]),
            "return": deepcopy(prior.get("return")) if prior else None,
            "legs": result["legs"],
            "routes": result["routes"],
            "expires_at": min(
                now() + timedelta(minutes=30),
                datetime.fromisoformat(result["dispatch_by"]),
                datetime.fromisoformat(state["deadline"]),
            ).isoformat(),
        }
        event(
            state,
            "Partial replacement proposed",
            f"Replace {failed}'s {old['kg']} kg; other driver assignments remain unchanged.",
            "warning",
        )
        return {
            "ok": True,
            "message": "Stop acknowledgment, custody evidence and affected confirmations required.",
        }
    draft = state.get("replacement")
    if not draft or command.get("replacement_request_id") != draft["id"]:
        return reject(state, "STALE_REPLACEMENT", "Review the current replacement request.")
    rid = command.get("resource_id")
    if action in ("accept_replacement", "commit_replacement") and now() >= datetime.fromisoformat(
        draft["expires_at"]
    ):
        return reject(state, "EXPIRED_REPLACEMENT", "Request a fresh feasible replacement.")
    if action == "ack_stop":
        if rid != draft["failed_driver"]:
            return reject(state, "NOT_ASSIGNED", "Only the failed driver can acknowledge stopping.")
        draft["stopped"] = True
    elif action == "confirm_return":
        if (
            rid != draft["failed_driver"]
            or rid not in p["picked_up"]
            or not command.get("evidence", "").strip()
        ):
            return reject(
                state,
                "RETURN_EVIDENCE_REQUIRED",
                "Donor evidence must identify the collected failed load.",
            )
        if draft["return"]:
            return reject(state, "RETURN_EXISTS", "Return evidence cannot be overwritten.")
        draft["return"] = {
            "kg": draft["kg"],
            "evidence": command["evidence"],
            "at": now().isoformat(),
        }
    elif action == "accept_replacement":
        if rid not in draft["required"]:
            return reject(
                state, "NOT_ASSIGNED", "Only affected participants confirm this replacement."
            )
        if rid not in draft["accepted"]:
            draft["accepted"].append(rid)
    else:
        if not draft["stopped"] or (
            draft["failed_driver"] in p["picked_up"] and not draft["return"]
        ):
            return reject(
                state,
                "CUSTODY_UNRESOLVED",
                "The failed driver must stop and collected food must be returned.",
            )
        if set(draft["required"]) != set(draft["accepted"]):
            return reject(
                state,
                "MISSING_CONFIRMATIONS",
                "Replacement driver and affected recipients must confirm.",
            )
        old = deepcopy(p)
        old["status"] = "amended"
        state["plans"].append(old)
        p["revision"] = len(state["plans"]) + 1
        failed, candidate = draft["failed_driver"], draft["replacement_driver"]
        unchanged = [d["resource_id"] for d in p["drivers"] if d["resource_id"] != failed]
        origins = p.setdefault("preserved_driver_revisions", {})
        origins.pop(failed, None)
        for driver in unchanged:
            origins.setdefault(driver, []).append(old["revision"])
        p["drivers"] = [d for d in p["drivers"] if d["resource_id"] != failed] + [
            {
                "resource_id": candidate,
                "name": resource(state, candidate)["name"],
                "kg": draft["kg"],
            }
        ]
        p["legs"] = [leg for leg in p["legs"] if leg["driver"] != failed] + deepcopy(draft["legs"])
        p["routes"] = [route for route in p["routes"] if route["driver"] != failed] + deepcopy(
            draft["routes"]
        )
        p["travel_minutes"] = sum(route["travel_minutes"] for route in p["routes"])
        p["required"] = [candidate if rid == failed else rid for rid in p["required"]]
        p["accepted"] = list(p["required"])
        p["picked_up"] = [rid for rid in p["picked_up"] if rid != failed]
        p["status"] = state["status"] = "in_transit" if p["picked_up"] else "committed"
        p["expires_at"] = draft["expires_at"]
        state["replacement_history"].append(
            {**deepcopy(draft), "status": "committed", "result_revision": p["revision"]}
        )
        state["replacement"] = None
        state["replacement_failure"] = None
        event(
            state,
            "Partial replacement committed",
            f"{candidate} takes {draft['kg']} kg. Unchanged drivers retain their route, load and prior commitment.",
        )
        return {
            "ok": True,
            "message": "Replacement committed; unchanged driver commitments preserved.",
        }
    event(state, "Replacement evidence recorded", action)
    return {"ok": True, "message": "Evidence recorded for this exact replacement request."}
