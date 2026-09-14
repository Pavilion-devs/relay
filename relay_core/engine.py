"""Deterministic commitments. The model never bypasses these transitions."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError

from .planner import Logistics, fixture, solve


def now():
    return datetime.now(UTC)


def event(s, title, detail, kind="info"):
    s["events"].append(
        {
            "id": str(uuid4()),
            "at": now().isoformat(),
            "title": title,
            "detail": detail,
            "kind": kind,
        }
    )


def seed():
    s = {
        "id": str(uuid4()),
        "name": "Morning market collection",
        "total_kg": 320,
        "crates": 40,
        "kg_per_crate": 8,
        "facts_version": 1,
        "status": "disrupted",
        "plan": None,
        "plans": [],
        "messages": [],
        "suggestions": [],
        "events": [],
        "deadline": (now() + timedelta(hours=3)).isoformat(),
        "resources": [
            {
                "id": "ada",
                "name": "Ada",
                "role": "driver",
                "capacity": 320,
                "available": False,
                "conditional": False,
                "source": "Cancellation reported in rehearsal",
            },
            {
                "id": "tunde",
                "name": "Tunde",
                "role": "driver",
                "capacity": 192,
                "available": True,
                "conditional": True,
                "source": "24 crates, if someone takes the remaining 16",
            },
            {
                "id": "amara",
                "name": "Amara",
                "role": "driver",
                "capacity": 128,
                "available": True,
                "conditional": False,
                "source": "Available for a revised assignment",
            },
            {
                "id": "harbour",
                "name": "Harbour community kitchen",
                "role": "recipient",
                "capacity": 320,
                "available": True,
                "conditional": False,
                "source": "Initial receiving capacity",
            },
            {
                "id": "garden",
                "name": "Garden food pantry",
                "role": "recipient",
                "capacity": 80,
                "available": True,
                "conditional": False,
                "source": "Alternative receiving capacity",
            },
        ],
    }
    s["logistics"] = fixture(now(), s["resources"])
    s["planning"] = None
    event(
        s,
        "Original driver cancelled",
        "Ada can no longer collect 40 crates. Revised assignments are needed.",
        "warning",
    )
    return s


def resource(s, rid):
    return next((r for r in s["resources"] if r["id"] == rid), None)


def reject(s, code, message):
    event(s, "Action held", message, "warning")
    return {"ok": False, "code": code, "message": message}


def invalidate(s):
    if s["plan"]:
        old = deepcopy(s["plan"])
        if old["status"] != "cancelled":
            old["status"] = "superseded"
        s["plans"].append(old)
        event(
            s,
            "Previous proposal superseded",
            f"Revision {old['revision']} can no longer be accepted.",
            "warning",
        )
    s["plan"] = None
    s["planning"] = None
    s["facts_version"] += 1
    s["status"] = "disrupted"


def apply(s, c):
    action = c["action"]
    from . import replacement

    if action in replacement.ACTIONS:
        return replacement.apply(s, c)
    if action == "recover":
        if s["status"] != "cancelled":
            return reject(
                s,
                "CANCELLATION_REQUIRED",
                "Resolve cancellation before starting a replacement plan.",
            )
        invalidate(s)
        return {
            "ok": True,
            "message": "Cancelled plan preserved. Update facts and request fresh commitments.",
        }
    if action in ("cancel", "ack_cancel", "ack_return"):
        p = s.get("plan")
        if not p or c.get("revision") != p["revision"]:
            return reject(
                s, "STALE_REVISION", "Cancellation must reference the current committed revision."
            )
        if action == "cancel":
            if s["status"] == "cancelling":
                return {
                    "ok": True,
                    "message": "Cancellation is already pending. Reservations remain held.",
                }
            if s["status"] not in ("committed", "in_transit", "awaiting_receipt", "discrepancy"):
                return reject(
                    s, "INVALID_STATUS", "Only an active commitment can enter cancellation."
                )
            if p["receipts"]:
                return reject(
                    s,
                    "RECONCILIATION_REQUIRED",
                    "Recipient evidence already exists. Reconcile that delivery separately; it cannot be erased by cancellation.",
                )
            reason = c.get("reason", "")
            if not isinstance(reason, str) or not reason.strip():
                return reject(s, "REASON_REQUIRED", "Record the reason for cancellation.")
            if s.get("replacement"):
                s.setdefault("replacement_history", []).append(
                    {**deepcopy(s["replacement"]), "status": "cancelled"}
                )
                s["replacement"] = None
            s["replacement_failure"] = None
            p["cancellation_reason"] = reason[:2000]
            p["cancellation_acks"] = []
            p["returns"] = {}
            p["status"] = s["status"] = "cancelling"
            event(
                s,
                "Cancellation requested",
                "Participants must acknowledge; collected loads require donor return evidence. Reservations remain held.",
                "warning",
            )
            return {"ok": True, "message": "Cancellation pending. No capacity has been released."}
        if s["status"] != "cancelling":
            return reject(s, "INVALID_STATUS", "No cancellation is awaiting acknowledgment.")
        rid = c.get("resource_id")
        if action == "ack_return":
            evidence = c.get("evidence", "")
            if rid not in p["picked_up"] or not isinstance(evidence, str) or not evidence.strip():
                return reject(
                    s,
                    "RETURN_EVIDENCE_REQUIRED",
                    "Record donor acknowledgment for an assigned collected load.",
                )
            if rid in p["returns"]:
                return reject(
                    s, "RETURN_EXISTS", "Return evidence already exists; it cannot be overwritten."
                )
            p["returns"][rid] = {
                "evidence": evidence[:2000],
                "at": now().isoformat(),
                "kg": next(d["kg"] for d in p["drivers"] if d["resource_id"] == rid),
            }
            event(s, "Donor return recorded", f"Returned load acknowledgment recorded for {rid}.")
        else:
            if rid not in p["required"]:
                return reject(
                    s,
                    "NOT_ASSIGNED",
                    "Only participants in this revision can acknowledge cancellation.",
                )
            if rid not in p["cancellation_acks"]:
                p["cancellation_acks"].append(rid)
                event(
                    s, "Cancellation acknowledged", f"{rid} acknowledged revision {p['revision']}."
                )
        if set(p["required"]) == set(p["cancellation_acks"]) and set(p["picked_up"]) <= set(
            p["returns"]
        ):
            p["status"] = s["status"] = "cancelled"
            event(
                s,
                "Cancellation resolved",
                "All acknowledgments and required return records are present; reservations can be released.",
            )
        return {
            "ok": True,
            "message": "Cancellation evidence recorded. "
            + (
                "Reservations released."
                if s["status"] == "cancelled"
                else "Outstanding evidence still holds reservations."
            ),
        }
    if action in ("change", "apply_suggestion", "set_logistics"):
        if s["status"] in (
            "cancelling",
            "cancelled",
            "committed",
            "in_transit",
            "awaiting_receipt",
            "complete",
            "discrepancy",
        ):
            return reject(
                s,
                "OPERATION_ACTIVE",
                "This assignment is already committed. Pause and coordinate a cancellation before changing it.",
            )
        if action == "set_logistics":
            try:
                logistics = Logistics.model_validate(c.get("logistics"))
            except ValidationError:
                return reject(
                    s,
                    "INVALID_LOGISTICS",
                    "Use valid time windows, whole travel minutes and explicit handling inputs.",
                )
            previous = s.get("logistics")
            if previous and logistics.starts_at != datetime.fromisoformat(previous["starts_at"]):
                return reject(
                    s,
                    "INVALID_LOGISTICS",
                    "The rehearsal start time cannot be moved. Start a new rehearsal instead.",
                )
            invalidate(s)
            s["logistics"] = logistics.model_dump(mode="json")
            event(
                s,
                "Travel and operating windows updated",
                "Previous route decisions are superseded; a new feasibility check is required.",
                "warning",
            )
            return {
                "ok": True,
                "message": "Logistics updated. Build a new proposal and collect fresh confirmations.",
            }
        values = c
        suggestion = None
        if action == "apply_suggestion":
            suggestion = next(
                (x for x in s["suggestions"] if x["id"] == c.get("suggestion_id")), None
            )
            if not suggestion or suggestion["applied"]:
                return reject(
                    s,
                    "INVALID_SUGGESTION",
                    "That suggestion is absent or has already been applied.",
                )
            if suggestion["facts_version"] != s["facts_version"]:
                return reject(
                    s,
                    "STALE_SUGGESTION",
                    "Facts changed after this interpretation. Review the message again.",
                )
            values = suggestion
        r = resource(s, values.get("resource_id"))
        if not r:
            return reject(s, "UNKNOWN_RESOURCE", "Choose a known participant.")
        capacity = values.get("capacity")
        if capacity is not None and (type(capacity) is not int or not 0 <= capacity <= 10000):
            return reject(
                s,
                "INVALID_CAPACITY",
                "Capacity must be a whole number from 0 to 10,000 kg.",
            )
        for field in ("available", "conditional"):
            if field in values and values[field] is not None and type(values[field]) is not bool:
                return reject(s, "INVALID_VALUE", f"{field} must be true or false.")
        window = None
        if suggestion:
            if (
                suggestion.get("conditional") is True
                and suggestion.get("condition_kind") != "remaining_load_covered"
            ):
                return reject(
                    s,
                    "UNSUPPORTED_CONDITION",
                    "This condition needs coordinator resolution; it cannot become an automated commitment.",
                )
            if any(suggestion.get(key) is not None for key in ("window_opens", "window_closes")):
                config = s.get("logistics", {}).get("participants", {}).get(r["id"])
                if config is None:
                    return reject(
                        s,
                        "MISSING_LOGISTICS",
                        "Record participant logistics before applying a time change.",
                    )
                window = dict(config["window"])
                for field, key in (("opens", "window_opens"), ("closes", "window_closes")):
                    if suggestion.get(key) is not None:
                        window[field] = suggestion[key]
                if (
                    any(type(v) is not int or not 0 <= v <= 1440 for v in window.values())
                    or window["closes"] <= window["opens"]
                ):
                    return reject(
                        s,
                        "INVALID_WINDOW",
                        "The proposed availability window is invalid. Clarification is required.",
                    )
        invalidate(s)
        if window is not None:
            s["logistics"]["participants"][r["id"]]["window"] = window
        if suggestion and suggestion.get("conditional") is not None:
            r["condition_text"] = suggestion.get("condition_text")
            r["condition_kind"] = suggestion.get("condition_kind")
        for field in ("capacity", "available", "conditional"):
            if field in values and values[field] is not None:
                r[field] = values[field]
        r["source"] = values.get("source", "Structured update in rehearsal")[:2000]
        if suggestion:
            suggestion["applied"] = True
        event(
            s,
            f"{r['name']} updated",
            f"Capacity {r['capacity']} kg · {'available' if r['available'] else 'unavailable'}",
            "warning",
        )
        return {
            "ok": True,
            "message": "Updated facts recorded. Previous decisions cannot authorize a new proposal.",
        }
    if action == "propose":
        if s["status"] not in (
            "disrupted",
            "needs_attention",
            "awaiting_confirmations",
        ):
            return reject(
                s,
                "OPERATION_ACTIVE",
                "The current operation has already been committed.",
            )
        if now() >= datetime.fromisoformat(s["deadline"]):
            s["status"] = "needs_attention"
            return reject(
                s,
                "WINDOW_CLOSED",
                "The collection window has closed. Coordinator intervention is required.",
            )
        # Repeated proposals against unchanged facts preserve existing decisions.
        if (
            s["plan"]
            and s["plan"].get("routes")
            and s.get("logistics")
            and s["plan"].get("reservation_snapshot") == s.get("reservation_snapshot")
            and s["plan"]["facts_version"] == s["facts_version"]
            and now() < datetime.fromisoformat(s["plan"]["expires_at"])
        ):
            return {
                "ok": True,
                "message": "The current proposal still matches the facts.",
            }
        if s["plan"]:
            expired = deepcopy(s["plan"])
            timed_out = now() >= datetime.fromisoformat(expired["expires_at"])
            expired["status"] = "expired" if timed_out else "superseded"
            s["plans"].append(expired)
            event(
                s,
                "Proposal expired" if timed_out else "Shared availability changed",
                "New confirmations are required.",
                "warning",
            )
        s["plan"] = None
        result = solve(s, now())
        s["planning"] = result
        if not result["ok"]:
            s["status"] = "needs_attention"
            return reject(s, result["code"], result["message"])
        legs = result["legs"]

        def allocations(role, field):
            return [
                {
                    "resource_id": r["id"],
                    "name": r["name"],
                    "kg": sum(leg["kg"] for leg in legs if leg[field] == r["id"]),
                }
                for r in s["resources"]
                if r["role"] == role and any(leg[field] == r["id"] for leg in legs)
            ]

        drivers = allocations("driver", "driver")
        recipients = allocations("recipient", "recipient")
        revision = len(s["plans"]) + 1
        required = [x["resource_id"] for x in drivers + recipients]
        s["plan"] = {
            "revision": revision,
            "facts_version": s["facts_version"],
            "status": "awaiting_confirmations",
            "drivers": drivers,
            "recipients": recipients,
            "legs": legs,
            "required": required,
            "accepted": [],
            "picked_up": [],
            "receipts": {},
            "expires_at": min(
                now() + timedelta(minutes=30),
                datetime.fromisoformat(s["deadline"]),
                datetime.fromisoformat(result["dispatch_by"]),
            ).isoformat(),
            "reservation_snapshot": deepcopy(s.get("reservation_snapshot")),
            "routes": result["routes"],
            "route_starts_at": result["starts_at"],
            "travel_minutes": result["travel_minutes"],
            "assumptions": [
                result["source"],
                "One pickup per driver; no return trip or shared capacity across rescues. Handling follows supplied rules, not a food-safety certification.",
            ],
        }
        s["status"] = "awaiting_confirmations"
        event(
            s,
            f"Revision {revision} proposed",
            f"All {s['total_kg']} kg are allocated. Participants must confirm their exact assignments.",
        )
        return {"ok": True, "message": "Proposal ready for participant confirmation."}
    if action in ("accept", "approve", "pickup", "receive"):
        p = s["plan"]
        preserved_pickup = bool(
            p
            and action == "pickup"
            and c.get("revision")
            in p.get("preserved_driver_revisions", {}).get(c.get("resource_id"), [])
        )
        if (
            not p
            or (c.get("revision") != p["revision"] and not preserved_pickup)
            or p["facts_version"] != s["facts_version"]
        ):
            return reject(
                s,
                "STALE_REVISION",
                "This decision refers to an obsolete proposal. Review the current revision.",
            )
        if action in ("accept", "approve") and (not p.get("routes") or not s.get("logistics")):
            return reject(
                s,
                "UNVERIFIED_ROUTE",
                "This older proposal has no verified route. Start a new rehearsal with travel inputs.",
            )
        if action in ("accept", "approve") and now() >= datetime.fromisoformat(p["expires_at"]):
            return reject(
                s,
                "EXPIRED_PROPOSAL",
                "This proposal expired. Refresh the facts and request a new proposal.",
            )
        rid = c.get("resource_id")
        failure = s.get("replacement_failure")
        if failure and (action == "receive" or (action == "pickup" and rid == failure["driver"])):
            return reject(
                s,
                "REPLACEMENT_PENDING",
                "Resolve the failed load before recording its movement or recipient receipt.",
            )
        if action == "accept":
            if s["status"] != "awaiting_confirmations":
                return reject(
                    s,
                    "INVALID_STATUS",
                    "This proposal is no longer awaiting acceptance.",
                )
            if rid not in p["required"]:
                return reject(
                    s,
                    "NOT_ASSIGNED",
                    "This participant has no assignment in this revision.",
                )
            if rid not in p["accepted"]:
                p["accepted"].append(rid)
                event(
                    s,
                    f"{resource(s, rid)['name']} confirmed",
                    f"Accepted revision {p['revision']}.",
                    "success",
                )
            return {
                "ok": True,
                "message": "Confirmation recorded for this exact revision.",
            }
        if action == "approve":
            if s["status"] != "awaiting_confirmations":
                return reject(s, "INVALID_STATUS", "This proposal cannot be committed again.")
            if set(p["accepted"]) != set(p["required"]):
                return reject(
                    s,
                    "MISSING_CONFIRMATIONS",
                    "Every assigned participant must confirm before dispatch.",
                )
            p["status"] = s["status"] = "committed"
            event(
                s,
                "Revised rescue committed",
                "All participant confirmations and coordinator approval agree.",
                "success",
            )
            return {
                "ok": True,
                "message": "Assignments committed. Pickup and receipt are still unconfirmed.",
            }
        if s["status"] not in (
            "committed",
            "in_transit",
            "awaiting_receipt",
            "discrepancy",
        ):
            return reject(
                s,
                "NOT_COMMITTED",
                "Dispatch must be approved before recording movement.",
            )
        if action == "pickup":
            if rid not in [x["resource_id"] for x in p["drivers"]]:
                return reject(s, "NOT_ASSIGNED", "Only an assigned driver can record pickup.")
            if rid not in p["picked_up"]:
                p["picked_up"].append(rid)
                event(
                    s,
                    f"{resource(s, rid)['name']} collected",
                    "Driver reports their assigned load is on board.",
                    "success",
                )
        else:
            recipient = next((x for x in p["recipients"] if x["resource_id"] == rid), None)
            amount = c.get("received_kg")
            if not recipient or type(amount) is not int or not 0 <= amount <= recipient["kg"]:
                return reject(
                    s,
                    "INVALID_RECEIPT",
                    "Receipt must be between zero and the assigned quantity.",
                )
            if any(
                leg["driver"] not in p["picked_up"] for leg in p["legs"] if leg["recipient"] == rid
            ):
                return reject(
                    s,
                    "PICKUP_UNCONFIRMED",
                    "Associated drivers must record pickup before recipient acknowledgment.",
                )
            if rid in p["receipts"]:
                return reject(
                    s,
                    "RECEIPT_EXISTS",
                    "A receipt already exists. Corrections require a separate reconciliation workflow.",
                )
            p["receipts"][rid] = amount
            event(
                s,
                f"{recipient['name']} acknowledged",
                f"Received {amount} of {recipient['kg']} kg.",
                "success" if amount == recipient["kg"] else "warning",
            )
        if any(p["receipts"].get(x["resource_id"], x["kg"]) != x["kg"] for x in p["recipients"]):
            s["status"] = "discrepancy"
        elif len(p["receipts"]) == len(p["recipients"]):
            s["status"] = "complete"
        elif len(p["picked_up"]) == len(p["drivers"]):
            s["status"] = "awaiting_receipt"
        else:
            s["status"] = "in_transit"
        p["status"] = s["status"]
        return {"ok": True, "message": "Evidence recorded."}
    return reject(s, "UNKNOWN_ACTION", "This action is not supported.")
