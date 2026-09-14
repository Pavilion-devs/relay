"""Atomic shared reservations and a durable local-inbox outbox.

All functions run inside Store's write transaction. No external messages are sent.
Reservations remain held until evidence closes the operation, even after planned times.
"""

import json
from datetime import datetime
from uuid import uuid4

from . import email_delivery


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS networks (id TEXT PRIMARY KEY, resources TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reservations (
            workspace TEXT NOT NULL, revision INTEGER NOT NULL, network TEXT NOT NULL,
            resource TEXT NOT NULL, role TEXT NOT NULL, kg INTEGER NOT NULL,
            released INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(workspace, revision, resource)
        );
        CREATE INDEX IF NOT EXISTS reservations_active ON reservations(network, resource, released);
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, workspace TEXT NOT NULL, revision INTEGER NOT NULL,
            kind TEXT NOT NULL, due REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            lease TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS jobs_due ON jobs(status, due, lease_until);
        CREATE TABLE IF NOT EXISTS inbox (
            id TEXT PRIMARY KEY, workspace TEXT NOT NULL, recipient TEXT NOT NULL,
            kind TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL
        );
    """)


def reserve(db, state):
    network = state.get("network_id", state["id"])
    row = db.execute("SELECT resources FROM networks WHERE id=?", (network,)).fetchone()
    resources = json.loads(row[0]) if row else {r["id"]: r for r in state["resources"]}
    p = state["plan"]
    allocations = [(r, "driver") for r in p["drivers"]] + [
        (r, "recipient") for r in p["recipients"]
    ]
    for allocation, role in allocations:
        rid = allocation["resource_id"]
        canonical = resources.get(rid)
        if not canonical or canonical["role"] != role:
            return f"{rid} is not a registered {role} in this network."
        used = db.execute(
            "SELECT COALESCE(SUM(kg),0), COUNT(*) FROM reservations WHERE network=? AND resource=? AND released=0",
            (network, rid),
        ).fetchone()
        if (role == "driver" and used[1]) or used[0] + allocation["kg"] > canonical["capacity"]:
            return f"{canonical['name']} is already reserved or exceeds the network capacity. Replan after the competing commitment is resolved."
    for allocation, role in allocations:
        db.execute(
            "INSERT INTO reservations(workspace,revision,network,resource,role,kg) VALUES(?,?,?,?,?,?)",
            (
                state["id"],
                p["revision"],
                network,
                allocation["resource_id"],
                role,
                allocation["kg"],
            ),
        )
    return None


def synchronize(db, before, state, timestamp):
    p = state.get("plan")
    if not p:
        return
    revision = p["revision"]
    previous = before.get("plan")
    failure = state.get("replacement_failure")
    if failure:
        draft = state.get("replacement")
        target = draft["id"] if draft else failure["id"]
        db.execute(
            "INSERT OR IGNORE INTO jobs(id,workspace,revision,kind,due) VALUES(?,?,?,?,?)",
            (
                f"{state['id']}:{revision}:replacement:{target}",
                state["id"],
                revision,
                "replacement",
                timestamp + 60,
            ),
        )
    for status, kind, delay in [
        ("awaiting_confirmations", "confirmations", 60),
        ("committed", "pickups", 60),
        ("in_transit", "pickups", 60),
        ("committed", "receipts", 1800),
        ("in_transit", "receipts", 1800),
        ("cancelling", "cancellation", 60),
    ]:
        if state["status"] == status and (
            before["status"] != status or not previous or previous["revision"] != revision
        ):
            jid = f"{state['id']}:{revision}:{kind}"
            db.execute(
                "INSERT OR IGNORE INTO jobs(id,workspace,revision,kind,due) VALUES(?,?,?,?,?)",
                (jid, state["id"], revision, kind, timestamp + delay),
            )
    if state["status"] in ("complete", "cancelled"):
        db.execute(
            "UPDATE reservations SET released=1 WHERE workspace=? AND revision=?",
            (state["id"], revision),
        )


def claim(db, timestamp, lease_seconds=60):
    row = db.execute(
        "SELECT id FROM jobs WHERE (status='pending' AND due<=?) OR (status='leased' AND lease_until<=?) ORDER BY due,id LIMIT 1",
        (timestamp, timestamp),
    ).fetchone()
    if not row:
        return None
    token = str(uuid4())
    db.execute(
        "UPDATE jobs SET status='leased', lease=?, lease_until=?, attempts=attempts+1 WHERE id=?",
        (token, timestamp + lease_seconds, row[0]),
    )
    return {"id": row[0], "lease": token}


def deliver(db, ticket, timestamp):
    row = db.execute(
        "SELECT workspace,revision,kind,attempts FROM jobs WHERE id=? AND lease=? AND status='leased' AND lease_until>?",
        (ticket["id"], ticket["lease"], timestamp),
    ).fetchone()
    if not row:
        return False
    workspace, revision, kind, attempts = row
    state = json.loads(
        db.execute("SELECT state FROM workspaces WHERE id=?", (workspace,)).fetchone()[0]
    )
    p = state.get("plan")
    waiting = {
        "confirmations": ("awaiting_confirmations",),
        "pickups": ("committed", "in_transit", "awaiting_receipt", "discrepancy"),
        "receipts": ("committed", "in_transit", "awaiting_receipt", "discrepancy"),
        "cancellation": ("cancelling",),
        "replacement": ("committed", "in_transit", "awaiting_receipt"),
    }
    if not p or p["revision"] != revision or state["status"] not in waiting[kind]:
        db.execute("UPDATE jobs SET status='obsolete',lease=NULL WHERE id=?", (ticket["id"],))
        return True
    draft = state.get("replacement")
    failure = state.get("replacement_failure")
    if kind == "replacement":
        target = draft["id"] if draft else failure["id"] if failure else None
        if not target or not ticket["id"].endswith(":" + target):
            db.execute("UPDATE jobs SET status='obsolete',lease=NULL WHERE id=?", (ticket["id"],))
            return True
    if kind == "replacement":
        recipients = sorted(set(draft["required"]) - set(draft["accepted"])) if draft else []
        if draft and not draft["stopped"]:
            recipients.append(draft["failed_driver"])
        if draft and draft["failed_driver"] in p["picked_up"] and not draft["return"]:
            recipients.append("donor")
        recipients = recipients or ["coordinator"]
    elif kind == "confirmations":
        missing = sorted(set(p["required"]) - set(p["accepted"]))
        # All participants may have replied while coordinator approval remains pending.
        recipients = missing or ["coordinator"]
    elif kind == "pickups":
        recipients = [
            d["resource_id"]
            for d in p["drivers"]
            if d["resource_id"] not in p["picked_up"]
            and not (failure and failure["driver"] == d["resource_id"])
        ]
        if not recipients:
            db.execute("UPDATE jobs SET status='obsolete',lease=NULL WHERE id=?", (ticket["id"],))
            return True
    elif kind == "receipts":
        recipients = [
            r["resource_id"] for r in p["recipients"] if r["resource_id"] not in p["receipts"]
        ]
        if not recipients:
            recipients = ["coordinator"]  # unresolved quantity discrepancy
    else:
        recipients = sorted(set(p["required"]) - set(p.get("cancellation_acks", []))) or [
            "coordinator"
        ]
    escalated = attempts >= 3 or (
        kind == "confirmations" and timestamp >= datetime.fromisoformat(p["expires_at"]).timestamp()
    )
    if kind == "replacement" and (
        not draft or timestamp >= datetime.fromisoformat(draft["expires_at"]).timestamp()
    ):
        escalated = True
    if escalated:
        recipients = ["coordinator"]
        state.setdefault("escalations", []).append(
            {
                "job_id": ticket["id"],
                "revision": revision,
                "reason": kind,
                "owner": "coordinator",
                "at": timestamp,
            }
        )
    for recipient in recipients:
        message_id = f"{ticket['id']}:{attempts}:{recipient}"
        body = f"Revision {revision}: {kind} still needs attention."
        if kind == "replacement" and draft and not escalated:
            body = (
                f"A partial replacement for revision {revision} needs your response. "
                "Sign in to review the exact request. Acknowledging a stop, recording a full return, "
                "and accepting a replacement are separate actions. Coordinator approval remains required."
            )
        db.execute(
            "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?,?)",
            (
                message_id,
                workspace,
                recipient,
                "escalation" if escalated else "reminder",
                body,
                timestamp,
            ),
        )
        if kind != "replacement" or (draft and not escalated and recipient != "coordinator"):
            email_delivery.enqueue(db, message_id, workspace, recipient, revision, timestamp)
    db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), workspace))
    db.execute(
        "UPDATE jobs SET status=?, due=?, lease=NULL, lease_until=NULL WHERE id=?",
        ("escalated" if escalated else "pending", timestamp + 300, ticket["id"]),
    )
    return True


def fail(db, ticket, timestamp):
    row = db.execute(
        "SELECT workspace,revision,attempts FROM jobs WHERE id=? AND lease=? AND status='leased' AND lease_until>?",
        (ticket["id"], ticket["lease"], timestamp),
    ).fetchone()
    if not row:
        return False
    workspace, revision, attempts = row
    terminal = attempts >= 3
    if terminal:
        state = json.loads(
            db.execute("SELECT state FROM workspaces WHERE id=?", (workspace,)).fetchone()[0]
        )
        state.setdefault("escalations", []).append(
            {
                "job_id": ticket["id"],
                "revision": revision,
                "reason": "worker_failure",
                "owner": "coordinator",
                "at": timestamp,
            }
        )
        db.execute("UPDATE workspaces SET state=? WHERE id=?", (json.dumps(state), workspace))
        db.execute(
            "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?,?)",
            (
                ticket["id"] + ":failure",
                workspace,
                "coordinator",
                "escalation",
                "Follow-up processing failed repeatedly. Coordinator intervention is required.",
                timestamp,
            ),
        )
    db.execute(
        "UPDATE jobs SET status=?,due=?,lease=NULL,lease_until=NULL WHERE id=?",
        (
            "failed" if terminal else "pending",
            timestamp + min(300, 30 * 2 ** min(attempts, 3)),
            ticket["id"],
        ),
    )
    return True
