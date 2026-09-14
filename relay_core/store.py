"""Local persistence adapter. Every command and its receipt commit atomically."""

import hashlib
import json
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from . import access, email_delivery, email_events, execution, identity


class Store:
    def __init__(self, path: str, clock=time.time):
        self.clock = clock
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS workspaces (
                    id TEXT PRIMARY KEY, state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commands (
                    workspace TEXT NOT NULL, id TEXT NOT NULL,
                    digest TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY(workspace, id)
                );
            """)

            execution.initialize(db)
            access.initialize(db)
            identity.initialize(db)
            email_delivery.initialize(db)
            email_events.initialize(db)

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def create_network(self, resources):
        network = str(uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO networks VALUES (?,?)",
                (network, json.dumps({r["id"]: r for r in resources})),
            )
        return network

    def create(self, state, network_id=None, guard=None):
        state = deepcopy(state)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if network_id:
                row = db.execute(
                    "SELECT resources FROM networks WHERE id=?", (network_id,)
                ).fetchone()
                if not row:
                    raise KeyError(network_id)
                state["network_id"] = network_id
                state["resources"] = list(json.loads(row[0]).values())
            else:
                state["network_id"] = state["id"]
                db.execute(
                    "INSERT INTO networks VALUES (?,?)",
                    (state["id"], json.dumps({r["id"]: r for r in state["resources"]})),
                )
            if guard:
                guard(db, state)
            db.execute("INSERT INTO workspaces VALUES (?, ?)", (state["id"], json.dumps(state)))
        return state

    def read(self, workspace):
        with self.connect() as db:
            row = db.execute("SELECT state FROM workspaces WHERE id=?", (workspace,)).fetchone()
        if not row:
            raise KeyError(workspace)
        return json.loads(row[0])

    def transact(self, workspace, command_id, payload, operation, guard=None):
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            authorized_actor = None
            if guard:
                row = db.execute("SELECT state FROM workspaces WHERE id=?", (workspace,)).fetchone()
                if not row:
                    raise KeyError(workspace)
                authorized_actor = guard(db, json.loads(row[0]))
            prior = db.execute(
                "SELECT digest,result FROM commands WHERE workspace=? AND id=?",
                (workspace, command_id),
            ).fetchone()
            if prior:
                if prior[0] != digest:
                    return {
                        "ok": False,
                        "code": "IDEMPOTENCY_CONFLICT",
                        "message": "This request identifier already belongs to a different action.",
                    }
                return json.loads(prior[1])
            row = db.execute("SELECT state FROM workspaces WHERE id=?", (workspace,)).fetchone()
            if not row:
                raise KeyError(workspace)
            state = json.loads(row[0])
            before = deepcopy(state)
            if payload.get("action") == "propose":
                network = state.get("network_id", state["id"])
                registry = db.execute(
                    "SELECT resources FROM networks WHERE id=?", (network,)
                ).fetchone()
                canonical = (
                    json.loads(registry[0])
                    if registry
                    else {r["id"]: r for r in state["resources"]}
                )
                snapshot = {}
                for rid, resource in canonical.items():
                    used, count = db.execute(
                        "SELECT COALESCE(SUM(kg),0),COUNT(*) FROM reservations WHERE network=? AND resource=? AND released=0",
                        (network, rid),
                    ).fetchone()
                    snapshot[rid] = (
                        0
                        if resource["role"] == "driver" and count
                        else max(0, resource["capacity"] - used)
                    )
                state["reservation_snapshot"] = snapshot
            if payload.get("action") == "propose_replacement":
                rid = payload.get("replacement_id")
                busy = db.execute(
                    "SELECT COUNT(*) FROM reservations WHERE network=? AND resource=? AND released=0",
                    (state["network_id"], rid),
                ).fetchone()[0]
                state["replacement_capacity"] = {rid: 0} if busy else {}
            result = operation(state)
            if result["ok"] and payload.get("action") == "commit_replacement":
                db.execute("SAVEPOINT replacement_swap")
                db.execute(
                    "UPDATE reservations SET released=1 WHERE workspace=? AND released=0",
                    (workspace,),
                )
                conflict = execution.reserve(db, state)
                if conflict:
                    db.execute("ROLLBACK TO replacement_swap")
                    state = before
                    result = {"ok": False, "code": "RESERVATION_CONFLICT", "message": conflict}
                db.execute("RELEASE replacement_swap")
            if (
                result["ok"]
                and payload.get("action") != "commit_replacement"
                and state["status"] == "committed"
                and before["status"] != "committed"
            ):
                conflict = execution.reserve(db, state)
                if conflict:
                    state = before
                    result = {"ok": False, "code": "RESERVATION_CONFLICT", "message": conflict}
            if result["ok"] or before.get("replacement_failure") != state.get(
                "replacement_failure"
            ):
                execution.synchronize(db, before, state, self.clock())
            if authorized_actor:
                state.setdefault("audit", []).append(
                    {
                        "actor_id": authorized_actor["id"],
                        "role": authorized_actor["role"],
                        "resource_id": authorized_actor["resource_id"],
                        "command_id": command_id,
                        "action": payload.get("action", "message_tool"),
                        "ok": result["ok"],
                        "at": self.clock(),
                    }
                )
            result["state"] = state
            db.execute(
                "UPDATE workspaces SET state=? WHERE id=?",
                (json.dumps(state), workspace),
            )
            db.execute(
                "INSERT INTO commands VALUES (?,?,?,?)",
                (workspace, command_id, digest, json.dumps(result)),
            )
        return result

    def claim_job(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return execution.claim(db, self.clock())

    def deliver_job(self, ticket):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return execution.deliver(db, ticket, self.clock())

    def run_due(self, limit=100):
        count = 0
        for _ in range(limit):
            ticket = self.claim_job()
            if not ticket:
                break
            try:
                self.deliver_job(ticket)
            except (
                Exception  # noqa: BLE001 - isolate each durable job and bound failure retries
            ):  # Processing rolls back; record a bounded retry without leaking payloads.
                with self.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    execution.fail(db, ticket, self.clock())
            count += 1
        return count

    def inbox(self, workspace):
        self.read(workspace)
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,recipient,kind,body,created FROM inbox WHERE workspace=? ORDER BY created,id",
                (workspace,),
            ).fetchall()
        return [
            dict(zip(("id", "recipient", "kind", "body", "created"), row, strict=True))
            for row in rows
        ]

    def reservations(self, network):
        with self.connect() as db:
            rows = db.execute(
                "SELECT workspace,revision,resource,role,kg,released FROM reservations WHERE network=?",
                (network,),
            ).fetchall()
        return [
            dict(
                zip(
                    ("workspace", "revision", "resource", "role", "kg", "released"),
                    row,
                    strict=True,
                )
            )
            for row in rows
        ]
