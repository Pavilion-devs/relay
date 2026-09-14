"""Reproduce competing rescues and a real worker-process crash in an isolated temporary DB."""

import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from relay_core.engine import apply, seed
from relay_core.store import Store


def main():
    with tempfile.TemporaryDirectory(prefix="relay-execution-") as directory:
        clock = [datetime.now(UTC).timestamp()]
        store = Store(str(Path(directory) / "relay.sqlite3"), clock=lambda: clock[0])
        resources = seed()["resources"]
        resources[1].update(capacity=320, conditional=False)
        resources[2]["available"] = False
        resources[3]["capacity"] = 640
        network = store.create_network(resources)
        a, b = (store.create(seed(), network)["id"] for _ in range(2))

        def send(wid, action, **kw):
            payload = {"action": action, **kw}
            return store.transact(wid, str(uuid4()), payload, lambda s: apply(s, payload))

        for wid in (a, b):
            assert send(wid, "propose")["ok"]
            for rid in store.read(wid)["plan"]["required"]:
                assert send(wid, "accept", revision=1, resource_id=rid)["ok"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda wid: send(wid, "approve", revision=1), (a, b)))
        assert sum(r["ok"] for r in results) == 1
        winner, loser = (a, b) if results[0]["ok"] else (b, a)
        assert send(winner, "cancel", revision=1, reason="Synthetic donor postponement")["ok"]
        assert send(loser, "approve", revision=1)["code"] == "RESERVATION_CONFLICT"
        clock[0] += 61
        # The child commits a lease and exits abruptly before delivery. No long-running
        # process or personal AWS credentials are needed for this fault injection.
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json,os,sys; from relay_core.store import Store; "
                    "s=Store(sys.argv[1],clock=lambda:float(sys.argv[2])); "
                    "print(json.dumps(s.claim_job()),flush=True); os._exit(23)"
                ),
                store.path,
                str(clock[0]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert process.returncode == 23
        abandoned = json.loads(process.stdout)
        assert abandoned
        clock[0] += 61
        store = Store(store.path, clock=lambda: clock[0])
        assert not store.deliver_job(abandoned)
        store.run_due()
        assert store.read(winner)["status"] == "cancelling"
        assert any(
            r["workspace"] == winner and not r["released"] for r in store.reservations(network)
        )
        for rid in store.read(winner)["plan"]["required"]:
            assert send(winner, "ack_cancel", revision=1, resource_id=rid)["ok"]
        assert store.read(winner)["status"] == "cancelled"
        assert send(loser, "approve", revision=1)["ok"]
        assert send(loser, "pickup", revision=1, resource_id="tunde")["ok"]
        assert send(loser, "receive", revision=1, resource_id="harbour", received_kg=320)["ok"]
        assert all(r["released"] for r in store.reservations(network))
        assert send(winner, "recover")["ok"]
        assert send(winner, "propose")["ok"]
        assert store.read(winner)["plan"]["revision"] == 2
        assert store.read(winner)["plans"][0]["status"] == "cancelled"
        report = {
            "scenario": "Two synthetic rescues compete for one driver; cancellation and worker crash; restart and recovery",
            "checks": {
                "one_concurrent_dispatch_wins": True,
                "pending_cancellation_holds_capacity": True,
                "worker_process_exited_after_claim": process.returncode == 23,
                "expired_worker_lease_fenced": True,
                "restart_preserved_cancellation_and_reservations": True,
                "acknowledged_cancellation_released_capacity": True,
                "competing_rescue_completed": True,
                "replacement_revision_preserves_cancelled_history": True,
            },
            "model_calls": 0,
            "external_messages_sent": 0,
            "physical_deliveries_verified": 0,
            "storage": "Temporary SQLite database, removed after run",
        }
        Path("docs/execution-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
