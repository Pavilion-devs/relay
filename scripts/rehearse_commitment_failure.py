"""Repeatable authenticated HTTP cancellation/recovery evidence, entirely offline."""

import argparse
import hashlib
import json
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from relay_core import access, email_actions, identity
from relay_core.api import create_app
from relay_core.engine import seed
from relay_core.store import Store


class Rehearsal:
    def __init__(self, path):
        self.app = create_app(str(path))
        self.store = self.app.state.store
        self.clock = time.time()
        self.store.clock = lambda: self.clock
        self.client = TestClient(self.app)
        state = seed()
        state["logistics"]["starts_at"] = (datetime.now(UTC) + timedelta(minutes=35)).isoformat()
        self.state = self.store.create(state)
        self.wid = self.state["id"]
        self.network = self.state["network_id"]
        self.tokens = {}
        for role in ("coordinator", "donor"):
            self.tokens[role] = access.issue(self.store, self.network, role)["token"]
        for resource in state["resources"]:
            rid = resource["id"]
            invite = identity.invite(
                self.store,
                self.tokens["coordinator"],
                self.network,
                rid + "@example.org",
                resource["role"],
                rid,
            )
            # Explicit synthetic identity fixture. No JWT verification or AWS claim.
            session = identity.session(
                self.store,
                {
                    "iss": "offline-fixture",
                    "sub": rid,
                    "email": rid + "@example.org",
                    "exp": self.clock + 3600,
                },
                invitation_token=invite["invitation_token"],
            )
            self.tokens[rid] = session["token"]
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO email_subscriptions VALUES(?,1)", (invite["invitation_id"],)
                )
        self.trace = []
        self.checks = {}

    def check(self, name, value):
        self.checks[name] = bool(value)
        if not value:
            raise AssertionError(name)

    def read(self):
        return self.store.read(self.wid)

    def command(self, actor, action, expected=200, key=None, **fields):
        payload = {"action": action, "command_id": key or str(uuid4()), **fields}
        response = self.client.post(
            f"/workspaces/{self.wid}/commands",
            json=payload,
            headers={"Authorization": "Bearer " + self.tokens[actor]},
        )
        value = response.json()
        self.trace.append(
            {
                "actor": actor,
                "action": action,
                "revision": fields.get("revision"),
                "http_status": response.status_code,
                "code": value.get("code"),
                "resulting_status": self.read()["status"],
            }
        )
        assert response.status_code == expected, (action, response.status_code, value)
        return value

    def reminders(self):
        self.clock += 61
        self.store.run_due()
        revision = self.read()["plan"]["revision"]
        with self.store.connect() as db:
            return dict(
                db.execute(
                    """SELECT i.resource,e.id FROM email_outbox e
                JOIN invitations i ON i.id=e.membership JOIN inbox b ON b.id=e.inbox_id
                WHERE b.workspace=? AND e.revision=?""",
                    (self.wid, revision),
                )
            )

    def email_confirm(self, actor, eid, expected=200):
        response = self.client.post(
            "/email-actions/" + eid + "/confirm",
            headers={"Authorization": "Bearer " + self.tokens[actor]},
        )
        self.trace.append(
            {
                "actor": actor,
                "action": "email_confirm",
                "http_status": response.status_code,
                "code": response.json().get("code"),
                "resulting_status": self.read()["status"],
            }
        )
        assert response.status_code == expected, response.json()
        return response.json()

    def active_reservations(self):
        return [r for r in self.store.reservations(self.network) if not r["released"]]


def run_scenario(path, ending):
    r = Rehearsal(path)
    started = time.perf_counter()
    r.command("coordinator", "propose")
    original = r.read()["plan"]
    old = r.reminders()
    for rid in original["required"]:
        r.command(rid, "accept", resource_id=rid, revision=1)
    r.command("coordinator", "approve", revision=1)
    r.check("initial_commitment_reserved", bool(r.active_reservations()))
    # A load already collected makes cancellation harder than a pre-dispatch switch.
    r.command("tunde", "pickup", resource_id="tunde", revision=1)
    failure_time = time.perf_counter()
    r.command(
        "coordinator",
        "cancel",
        revision=1,
        reason="Synthetic driver breakdown after collection; Tunde cannot finish.",
    )
    r.check(
        "affected_load_identified",
        next(d["kg"] for d in original["drivers"] if d["resource_id"] == "tunde") == 192,
    )
    held = r.active_reservations()
    r.command("coordinator", "recover", expected=409)
    r.command("coordinator", "change", expected=409, resource_id="tunde", available=False)
    for rid in original["required"]:
        r.command(rid, "ack_cancel", resource_id=rid, revision=1)
    r.check(
        "acks_without_return_do_not_release_capacity",
        r.read()["status"] == "cancelling" and r.active_reservations() == held,
    )
    reopened = Store(str(path))
    r.check("pending_cancellation_survives_restart", reopened.read(r.wid)["status"] == "cancelling")
    r.command(
        "tunde",
        "ack_return",
        expected=403,
        resource_id="tunde",
        revision=1,
        evidence="Driver cannot attest for donor",
    )
    r.command(
        "donor",
        "ack_return",
        resource_id="tunde",
        revision=1,
        evidence="SYNTHETIC: donor acknowledges all 192 kg returned before replacement",
    )
    r.check(
        "donor_evidence_resolves_cancellation",
        r.read()["status"] == "cancelled" and not r.active_reservations(),
    )
    r.command("coordinator", "recover")
    r.command("coordinator", "change", resource_id="tunde", available=False)
    impossible = r.command("coordinator", "propose", expected=409)
    r.check(
        "no_replacement_escalates",
        impossible["code"] == "INSUFFICIENT_CAPACITY"
        and r.read()["status"] == "needs_attention"
        and r.read()["plan"] is None,
    )
    r.command("coordinator", "change", resource_id="ada", available=True)
    r.command("coordinator", "propose")
    replacement = r.read()["plan"]
    revision = replacement["revision"]
    r.check("new_revision_needs_fresh_acceptances", revision > 1 and not replacement["accepted"])
    r.check(
        "cancelled_history_preserved",
        r.read()["plans"][0]["status"] == "cancelled"
        and r.read()["plans"][0]["returns"]["tunde"]["kg"] == 192,
    )
    r.check(
        "replacement_excludes_failed_driver",
        all(d["resource_id"] != "tunde" for d in replacement["drivers"]),
    )
    r.check(
        "replacement_covers_320kg_with_routes",
        sum(d["kg"] for d in replacement["drivers"]) == 320 and bool(replacement["routes"]),
    )
    stale = r.email_confirm("tunde", old["tunde"], expected=409)
    r.check(
        "old_email_cannot_accept_new_revision",
        stale["code"] == "STALE_REVISION" and not r.read()["plan"]["accepted"],
    )
    r.check(
        "old_review_has_no_action",
        not email_actions.review(r.store, r.tokens["tunde"], old["tunde"])["current"],
    )
    r.command("coordinator", "approve", expected=409, revision=revision)
    fresh = r.reminders()
    for rid in replacement["required"]:
        before = r.read()["plan"]["accepted"]
        review = email_actions.review(r.store, r.tokens[rid], fresh[rid])
        r.check(
            "review_does_not_confirm_" + rid,
            review["current"] and r.read()["plan"]["accepted"] == before,
        )
        r.email_confirm(rid, fresh[rid])
        events = len(r.read()["events"])
        r.email_confirm(rid, fresh[rid])
        r.check("duplicate_confirmation_is_inert_" + rid, len(r.read()["events"]) == events)
    r.command("coordinator", "approve", revision=revision, key="replacement-dispatch")
    held = r.active_reservations()
    r.command("coordinator", "approve", revision=revision, key="replacement-dispatch")
    r.check(
        "duplicate_dispatch_has_one_reservation_set",
        r.active_reservations() == held
        and len(held) == len(replacement["drivers"]) + len(replacement["recipients"]),
    )
    recovery_seconds = time.perf_counter() - failure_time
    recipient = replacement["recipients"][0]
    r.command(
        recipient["resource_id"],
        "receive",
        expected=409,
        revision=revision,
        resource_id=recipient["resource_id"],
        received_kg=recipient["kg"],
    )
    r.check("receipt_without_pickup_cannot_complete", r.read()["status"] == "committed")
    for driver in replacement["drivers"]:
        r.command(
            driver["resource_id"], "pickup", revision=revision, resource_id=driver["resource_id"]
        )
    r.check("pickup_alone_cannot_complete", r.read()["status"] == "awaiting_receipt")
    if ending != "missing_receipt":
        for recipient in replacement["recipients"]:
            r.command(
                recipient["resource_id"],
                "receive",
                revision=revision,
                resource_id=recipient["resource_id"],
                received_kg=recipient["kg"] - (20 if ending == "short_receipt" else 0),
            )
    if ending == "complete":
        r.check(
            "matching_receipts_complete_and_release",
            r.read()["status"] == "complete" and not r.active_reservations(),
        )
    else:
        # Accelerate only the durable follow-up clock; no signed session is used afterward.
        r.clock += 1801
        for _ in range(3):
            r.store.run_due()
            r.clock += 301
        r.check(
            "unresolved_evidence_never_completes",
            r.read()["status"] != "complete" and bool(r.active_reservations()),
        )
        r.check(
            "unresolved_receipt_escalates_to_coordinator",
            any(
                item["recipient"] == "coordinator" and item["kind"] == "escalation"
                for item in r.store.inbox(r.wid)
            ),
        )
        if ending == "short_receipt":
            r.check(
                "short_receipt_preserved_as_discrepancy",
                r.read()["status"] == "discrepancy"
                and sum(r.read()["plan"]["receipts"].values()) == 300,
            )
    final = r.read()
    r.check("persisted_outcome_matches", reopened.read(r.wid) == final)
    successful = [a for a in final["audit"] if a["ok"]]
    report = {
        "ending": ending,
        "checks": r.checks,
        "trace": r.trace,
        "metrics": {
            "affected_load_kg": 192,
            "replacement_planned_kg": 320,
            "recipient_reported_kg": sum(final["plan"]["receipts"].values()),
            "unresolved_kg": 320 - sum(final["plan"]["receipts"].values()),
            "recovery_to_recommit_compute_seconds": round(recovery_seconds, 4),
            "total_compute_seconds": round(time.perf_counter() - started, 4),
            "scripted_successful_actor_actions": len(successful),
            "scripted_coordinator_actions": sum(a["role"] == "coordinator" for a in successful),
            "actual_human_interventions_measured": None,
        },
        "final_status": final["status"],
        "events": final["events"],
        "active_reservations": r.active_reservations(),
    }
    r.client.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; preserve previous evidence")
    with tempfile.TemporaryDirectory(prefix="relay-commitment-failure-") as directory:
        records = [
            run_scenario(Path(directory) / (ending + ".sqlite3"), ending)
            for ending in ("complete", "short_receipt", "missing_receipt")
        ]
    root = Path(__file__).resolve().parents[1]
    sources = [Path(__file__), *sorted((root / "relay_core").glob("*.py")), root / "uv.lock"]
    report = {
        "at": datetime.now(UTC).isoformat(),
        "scope": "Offline authenticated HTTP, synthetic identities/travel/food evidence, temporary SQLite. "
        "No AWS, model calls, external emails, real human timing or competitor comparison. "
        "Cancellation is coordinator-mediated; all old participants acknowledge and collected load returns before replanning.",
        "source_sha256": {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources
        },
        "records": records,
        "passed_checks": sum(sum(r["checks"].values()) for r in records),
    }
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(
        json.dumps(
            {
                "passed_checks": report["passed_checks"],
                "report": str(args.output),
                "outcomes": [r["final_status"] for r in records],
            }
        )
    )


if __name__ == "__main__":
    main()
