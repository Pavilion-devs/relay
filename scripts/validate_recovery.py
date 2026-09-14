"""Offline multiprocess recovery evidence; never a competitor or model benchmark."""

import argparse
import hashlib
import json
import multiprocessing as mp
import os
import platform
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from relay_core import execution
from relay_core.engine import apply, seed
from relay_core.store import Store

BARRIER = None


def initialize(barrier):
    global BARRIER
    BARRIER = barrier


def send(store, wid, action, key=None, **fields):
    payload = {"action": action, **fields}
    return store.transact(wid, key or str(uuid4()), payload, lambda s: apply(s, payload))


def prepare(path):
    state = seed()
    now = datetime.fromisoformat(state["logistics"]["starts_at"]).timestamp()
    store = Store(str(path), clock=lambda: now)
    resources = state["resources"]
    resources[1].update(capacity=320, conditional=False)
    resources[2]["available"] = False
    resources[3]["capacity"] = 640
    network = store.create_network(resources)
    workspaces = [store.create(seed(), network)["id"] for _ in range(2)]
    for wid in workspaces:
        assert send(store, wid, "propose")["ok"]
        for rid in store.read(wid)["plan"]["required"]:
            assert send(store, wid, "accept", revision=1, resource_id=rid)["ok"]
    return store, network, workspaces, now


def approve(path, wid, now):
    store = Store(path, clock=lambda: now)
    BARRIER.wait(timeout=20)
    started = time.monotonic_ns()
    result = send(store, wid, "approve", key="dispatch", revision=1)
    return {
        "pid": os.getpid(),
        "started_ns": started,
        "finished_ns": time.monotonic_ns(),
        "workspace": wid,
        "result": result,
    }


def crash_dispatch(path, wid, now, phase):
    store = Store(path, clock=lambda: now)
    if phase == "before_commit":
        original = execution.synchronize

        def interrupt(*args):
            original(*args)
            # Reservations and outbox have been written, but the transaction
            # has not committed. Abrupt exit bypasses Python cleanup entirely.
            os._exit(23)

        execution.synchronize = interrupt
    send(store, wid, "approve", key="dispatch", revision=1)
    os._exit(24)  # commit succeeded, but caller never receives a response


def snapshot(store, network, wid):
    with store.connect() as db:
        jobs = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE workspace=? AND kind='receipts'", (wid,)
        ).fetchone()[0]
        receipts = db.execute(
            "SELECT COUNT(*) FROM commands WHERE workspace=? AND id='dispatch'", (wid,)
        ).fetchone()[0]
    return {
        "status": store.read(wid)["status"],
        "reservations": store.reservations(network),
        "receipt_jobs": jobs,
        "dispatch_receipts": receipts,
    }


def run(rounds):
    context = mp.get_context("spawn")
    records = []
    with tempfile.TemporaryDirectory(prefix="relay-validation-") as directory:
        barrier = context.Barrier(2)
        with ProcessPoolExecutor(
            2, mp_context=context, initializer=initialize, initargs=(barrier,)
        ) as pool:
            for index in range(rounds):
                path = str(Path(directory) / f"race-{index}.sqlite3")
                store, network, ids, now = prepare(path)
                futures = [pool.submit(approve, path, wid, now) for wid in ids]
                results = [f.result(timeout=30) for f in futures]
                winners = [r for r in results if r["result"]["ok"]]
                losers = [r for r in results if not r["result"]["ok"]]
                checks = {
                    "separate_processes": len({r["pid"] for r in results}) == 2,
                    "one_winner": len(winners) == 1,
                    "conflict_visible": len(losers) == 1
                    and losers[0]["result"]["code"] == "RESERVATION_CONFLICT",
                    "one_active_driver": sum(
                        r["role"] == "driver" and not r["released"]
                        for r in store.reservations(network)
                    )
                    == 1,
                    "one_committed_workspace": sum(
                        store.read(w)["status"] == "committed" for w in ids
                    )
                    == 1,
                }
                records.append(
                    {
                        "scenario": "shared_driver_race",
                        "iteration": index,
                        "checks": checks,
                        "attempts": results,
                        "snapshots": [snapshot(store, network, w) for w in ids],
                    }
                )
        for phase in ("before_commit", "after_commit"):
            path = str(Path(directory) / f"crash-{phase}.sqlite3")
            store, network, ids, now = prepare(path)
            wid = ids[0]
            process = context.Process(target=crash_dispatch, args=(path, wid, now, phase))
            process.start()
            process.join(timeout=20)
            if process.is_alive():
                process.kill()
                process.join()
                raise RuntimeError("Fault injection child timed out")
            reopened = Store(path, clock=lambda now=now: now)
            before = snapshot(reopened, network, wid)
            result = send(reopened, wid, "approve", key="dispatch", revision=1)
            after = snapshot(reopened, network, wid)
            replay = send(reopened, wid, "approve", key="dispatch", revision=1)
            checks = {
                "expected_exit": process.exitcode == (23 if phase == "before_commit" else 24),
                "retry_success": result["ok"],
                "one_reservation_pair": len(after["reservations"]) == 2,
                "one_outbox_job": after["receipt_jobs"] == 1,
                "one_receipt": after["dispatch_receipts"] == 1,
                "identical_replay": replay == result and snapshot(reopened, network, wid) == after,
            }
            if phase == "before_commit":
                checks["no_partial_commit"] = (
                    before["status"] == "awaiting_confirmations"
                    and not before["reservations"]
                    and before["receipt_jobs"] == before["dispatch_receipts"] == 0
                )
            else:
                checks["commit_survives_lost_response"] = before == after
            records.append(
                {
                    "scenario": f"dispatch_crash_{phase}",
                    "checks": checks,
                    "exit_code": process.exitcode,
                    "before_retry": before,
                    "after_retry": after,
                }
            )
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 1000:
        parser.error("rounds must be between 1 and 1000")
    if args.output.exists():
        parser.error("output already exists; preserve prior evidence")
    root = Path(__file__).resolve().parents[1]
    paths = [Path(__file__), root / "uv.lock", *sorted((root / "relay_core").glob("*.py"))]
    hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    records = run(args.rounds)
    passed = sum(all(r["checks"].values()) for r in records)
    report = {
        "schema_version": 1,
        "at": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "scope": "Relay only; local SQLite, synthetic inputs, direct domain commands, no model or external transport; no competitor result",
        "source_sha256": hashes,
        "scenarios": len(records),
        "passed": passed,
        "records": records,
    }
    with args.output.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps({"scenarios": len(records), "passed": passed, "report": str(args.output)}))
    if passed != len(records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
