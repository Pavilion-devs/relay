"""Provision local test identities; never prints bearer tokens to the terminal."""

import argparse
import json
import os

from .access import issue
from .engine import seed
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".data/relay.sqlite3")
    sub = parser.add_subparsers(dest="action", required=True)
    provision = sub.add_parser("provision")
    provision.add_argument("--output", required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("grant_id")
    args = parser.parse_args()
    store = Store(args.db)
    if args.action == "revoke":
        with store.connect() as db:
            count = db.execute(
                "UPDATE access_grants SET revoked=1 WHERE id=?", (args.grant_id,)
            ).rowcount
        print(f"Revoked {count} grant(s).")
        return
    # Refuse an existing path before creating identities. No shell or token output.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as destination:
        resources = seed()["resources"]
        network = store.create_network(resources)
        grants = [issue(store, network, "coordinator"), issue(store, network, "donor")]
        grants.extend(issue(store, network, r["role"], r["id"]) for r in resources)
        json.dump({"network_id": network, "grants": grants}, destination, indent=2)
    print("Local test grants provisioned in the requested private file. They expire in 24 hours.")


if __name__ == "__main__":
    main()
