"""Local durable follow-up worker: uv run python -m relay_core.worker --db .data/relay.sqlite3.

Defaults to the local inbox. --email-sender explicitly enables SES for opted-in memberships.
"""

import argparse
import time

from .email_delivery import SESProvider, run_once
from .store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".data/relay.sqlite3")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--email-sender", help="Explicitly enable SES delivery with this verified sender"
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--configuration-set")
    parser.add_argument("--action-base-url", help="Trusted application origin for reminder links")
    args = parser.parse_args()
    provider = (
        SESProvider(args.email_sender, args.region, args.configuration_set, args.action_base_url)
        if args.email_sender
        else None
    )
    store = Store(args.db)
    while True:
        store.run_due()
        if provider:
            run_once(store, provider)
        if args.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
