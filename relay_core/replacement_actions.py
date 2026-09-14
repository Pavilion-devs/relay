"""Resolve the action permitted by one exact amendment and participant identity."""

from copy import deepcopy
from datetime import datetime


def context(state, revision, request_id, role, resource, timestamp):
    plan, draft = state.get("plan"), state.get("replacement")
    if (
        not plan
        or not draft
        or plan["revision"] != revision
        or draft["base_revision"] != revision
        or draft["id"] != request_id
        or state["status"] not in ("committed", "in_transit", "awaiting_receipt")
        or plan["receipts"]
        or timestamp >= datetime.fromisoformat(draft["expires_at"]).timestamp()
    ):
        return None
    action, done, kg = None, False, draft["kg"]
    target = resource
    routes = []
    if role == "driver" and resource == draft["failed_driver"]:
        action, done = "ack_stop", draft["stopped"]
    elif role == "donor" and draft["failed_driver"] in plan["picked_up"]:
        action, done, target = "confirm_return", bool(draft["return"]), draft["failed_driver"]
    elif resource in draft["required"] and role in ("driver", "recipient"):
        action, done = "accept_replacement", resource in draft["accepted"]
        if role == "recipient":
            kg = sum(leg["kg"] for leg in draft["legs"] if leg["recipient"] == resource)
            routes = [
                {
                    "driver": route["driver"],
                    "stops": [
                        deepcopy(stop) for stop in route["stops"] if stop["recipient"] == resource
                    ],
                }
                for route in draft["routes"]
            ]
        else:
            routes = deepcopy(draft["routes"])
    if action is None:
        return None
    return {
        "action": action,
        "resource_id": target,
        "already_confirmed": done,
        "kg": kg,
        "routes": routes,
        "expires_at": draft["expires_at"],
        "request_id": request_id,
        "failed_driver": draft["failed_driver"],
        "replacement_driver": draft["replacement_driver"],
        "reason": draft["reason"],
        "role": role,
    }
