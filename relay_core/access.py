"""Local opaque access grants, not federated identity or production login."""

import hashlib
import json
import secrets
from copy import deepcopy
from uuid import uuid4


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS access_grants (
        id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, network TEXT NOT NULL,
        role TEXT NOT NULL, resource TEXT, expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
    )""")


def issue(store, network, role, resource=None, hours=24):
    if role not in ("coordinator", "driver", "recipient", "donor") or not 0 < hours <= 24:
        raise ValueError("Invalid grant role or lifetime")
    token = secrets.token_urlsafe(32)
    gid = str(uuid4())
    with store.connect() as db:
        row = db.execute("SELECT resources FROM networks WHERE id=?", (network,)).fetchone()
        if not row:
            raise KeyError(network)
        resources = json.loads(row[0])
        if role in ("driver", "recipient") and (
            resource not in resources or resources[resource]["role"] != role
        ):
            raise ValueError("Participant must match the registered role")
        if role in ("coordinator", "donor"):
            resource = None
        db.execute(
            "INSERT INTO access_grants VALUES(?,?,?,?,?,?,0)",
            (
                gid,
                hashlib.sha256(token.encode()).hexdigest(),
                network,
                role,
                resource,
                store.clock() + hours * 3600,
            ),
        )
    return {"id": gid, "token": token, "role": role, "resource_id": resource, "network_id": network}


def principal(db, token, timestamp):
    row = db.execute(
        "SELECT id,network,role,resource FROM access_grants WHERE digest=? AND revoked=0 AND expires>?",
        (hashlib.sha256(token.encode()).hexdigest(), timestamp),
    ).fetchone()
    if not row:
        raise PermissionError("Access grant is absent, expired or revoked")
    return dict(zip(("id", "network_id", "role", "resource_id"), row, strict=True))


def authorize(db, token, timestamp, state, action, resource_id=None):
    actor = principal(db, token, timestamp)
    if actor["network_id"] != state.get("network_id", state["id"]):
        raise PermissionError("This operation belongs to another network")
    role = actor["role"]
    allowed = {
        "coordinator": {
            "read",
            "inbox",
            "propose",
            "approve",
            "change",
            "set_logistics",
            "apply_suggestion",
            "cancel",
            "recover",
            "propose_replacement",
            "commit_replacement",
        },
        "driver": {
            "read",
            "inbox",
            "accept",
            "pickup",
            "ack_cancel",
            "ack_stop",
            "accept_replacement",
            "messages",
        },
        "recipient": {
            "read",
            "inbox",
            "accept",
            "receive",
            "ack_cancel",
            "accept_replacement",
            "messages",
        },
        "donor": {"read", "inbox", "ack_return", "confirm_return"},
    }
    if action not in allowed[role]:
        raise PermissionError("This role cannot perform that action")
    if (
        role in ("driver", "recipient")
        and action not in ("read", "inbox")
        and resource_id != actor["resource_id"]
    ):
        raise PermissionError("A participant can act only on their own commitment")
    return actor


def project(state, actor):
    if actor["role"] == "coordinator":
        return state
    rid = actor["resource_id"]
    p = state.get("plan")
    result = {k: state[k] for k in ("id", "name", "status", "network_id")}
    result["participant"] = {"role": actor["role"], "resource_id": rid}
    result["plan"] = None
    if p:
        plan = {k: p[k] for k in ("revision", "status", "expires_at")}
        if actor["role"] == "donor":
            plan["pickups"] = deepcopy(p["picked_up"])
            plan["returns"] = deepcopy(p.get("returns", {}))
            plan["drivers"] = deepcopy(p["drivers"])
        else:
            plan["assignments"] = [
                deepcopy(a) for a in p["drivers"] + p["recipients"] if a["resource_id"] == rid
            ]
            plan["accepted"] = rid in p["accepted"]
            plan["cancel_acknowledged"] = rid in p.get("cancellation_acks", [])
            plan["received_kg"] = p["receipts"].get(rid)
            plan["picked_up"] = rid in p["picked_up"]
            plan["legs"] = [deepcopy(leg) for leg in p["legs"] if leg.get(actor["role"]) == rid]
            if actor["role"] == "driver":
                plan["routes"] = [
                    deepcopy(route) for route in p.get("routes", []) if route["driver"] == rid
                ]
                plan["route_starts_at"] = p.get("route_starts_at")
        result["plan"] = plan
    result["recovery_pending"] = bool(state.get("replacement_failure"))
    draft = state.get("replacement")
    if draft and (
        rid in draft["required"] or rid == draft["failed_driver"] or actor["role"] == "donor"
    ):
        result["replacement"] = deepcopy(draft)
    else:
        result["replacement"] = None
    result["messages"] = [
        deepcopy(m) for m in state["messages"] if rid and m.get("resource_id") == rid
    ]
    result["suggestions"] = [
        deepcopy(m) for m in state["suggestions"] if rid and m.get("resource_id") == rid
    ]
    return result


class ScopedStore:
    """Recheck grant inside each model tool transaction, including after revocation."""

    def __init__(self, store, token, resource):
        self.store, self.token, self.resource = store, token, resource

    def guard(self, db, state):
        return authorize(db, self.token, self.store.clock(), state, "messages", self.resource)

    def read(self, workspace):
        state = self.store.read(workspace)
        with self.store.connect() as db:
            self.guard(db, state)
        return state

    def model_context(self, state, message):
        with self.store.connect() as db:
            actor = self.guard(db, state)
        logistics = state.get("logistics") or {}
        return {
            "resources": [
                {k: r[k] for k in ("id", "name", "role")}
                for r in state["resources"]
                if r["id"] == self.resource
            ],
            "total_kg": state["total_kg"],
            "facts_version": state["facts_version"],
            "message": message,
            "plan": project(state, actor)["plan"],
            "logistics": {
                "pickup": logistics.get("pickup"),
                "starts_at": logistics.get("starts_at"),
                "participant": logistics.get("participants", {}).get(self.resource),
            },
        }

    def transact(self, workspace, command_id, payload, operation):
        return self.store.transact(workspace, command_id, payload, operation, guard=self.guard)
