"""Consume SES notifications from a dedicated, policy-restricted SNS-to-SQS queue.

This is not an Internet webhook. Transport trust requires the accompanying queue
policy; a TopicArn in an arbitrary JSON object is not authentication.
"""

import argparse
import json

from . import email_events
from .store import Store


def parse(body, topic, account, configuration_set):
    if not isinstance(body, str) or len(body) > 262144:
        raise ValueError("Invalid envelope size")
    envelope = json.loads(body)
    if envelope.get("Type") != "Notification" or envelope.get("TopicArn") != topic:
        raise ValueError("Unexpected publisher")
    if (
        envelope.get("Message")
        == "Successfully validated SNS topic for Amazon SES event publishing."
    ):
        return None  # AWS setup control message; not evidence of email delivery.
    event = json.loads(envelope["Message"])
    mail = event["mail"]
    if mail.get("sendingAccountId") != account:
        raise ValueError("Unexpected sending account")
    tags = mail.get("tags", {})
    if tags.get("ses:configuration-set") != [configuration_set]:
        raise ValueError("Unexpected configuration set")
    ids = tags.get("relay_outbox_id")
    if not isinstance(ids, list) or len(ids) != 1:
        raise ValueError("Missing unique outbox correlation")
    kind = event["eventType"].lower()
    if kind not in ("delivery", "bounce", "complaint", "reject") or not isinstance(
        event.get(kind), dict
    ):
        raise ValueError("Unexpected event type or detail")
    return envelope["MessageId"], ids[0], mail["messageId"], kind


def poll_once(store, client, queue_url, topic, account, configuration_set):
    response = client.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=10, VisibilityTimeout=60
    )
    stats = {"handled": 0, "retained": 0}
    for message in response.get("Messages", []):
        try:
            values = parse(message["Body"], topic, account, configuration_set)
            if values is not None:
                email_events.record(store, *values)
            # Duplicates are also safe to acknowledge; persistence happens first.
            client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
            stats["handled"] += 1
        except Exception:  # noqa: BLE001 - retain failures for retry/DLQ; never log recipient payloads
            stats["retained"] += 1
    return stats


def main():
    import boto3
    from botocore.config import Config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=".data/relay.sqlite3")
    parser.add_argument("--queue-url", required=True)
    parser.add_argument("--topic-arn", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--configuration-set", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    client = boto3.client(
        "sqs", region_name=args.region, config=Config(connect_timeout=5, read_timeout=20)
    )
    store = Store(args.db)
    while True:
        print(
            json.dumps(
                poll_once(
                    store,
                    client,
                    args.queue_url,
                    args.topic_arn,
                    args.account_id,
                    args.configuration_set,
                )
            ),
            flush=True,
        )
        if args.once:
            return


if __name__ == "__main__":
    main()
