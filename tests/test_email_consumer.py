import json

import pytest
from test_email_delivery import queued as email_queue_fixture

from relay_core import email_delivery, email_events
from relay_core.email_consumer import parse, poll_once


@pytest.fixture
def queued(tmp_path):
    return email_queue_fixture.__wrapped__(tmp_path)


TOPIC = "arn:aws:sns:us-east-1:123456789012:relay"


def body(eid):
    event = {
        "eventType": "Delivery",
        "delivery": {},
        "mail": {
            "sendingAccountId": "123456789012",
            "messageId": "ses-id",
            "tags": {"relay_outbox_id": [eid], "ses:configuration-set": ["relay"]},
        },
    }
    return json.dumps(
        {
            "Type": "Notification",
            "TopicArn": TOPIC,
            "MessageId": "sns-id",
            "Message": json.dumps(event),
        }
    )


class Queue:
    def __init__(self, body, fail_delete=False):
        self.body = body
        self.fail_delete = fail_delete
        self.deleted = 0

    def receive_message(self, **kw):
        return {"Messages": [{"Body": self.body, "ReceiptHandle": "handle"}]}

    def delete_message(self, **kw):
        if self.fail_delete:
            raise TimeoutError()
        self.deleted += 1


def poll(store, q):
    return poll_once(store, q, "queue", TOPIC, "123456789012", "relay")


def test_persist_before_ack_and_replay_after_delete_failure(queued):
    store, _, _, _ = queued
    ticket = email_delivery.claim(store)
    email_delivery.finish(store, ticket, "accepted", "ses-id")
    q = Queue(body(ticket["id"]), fail_delete=True)
    assert poll(store, q) == {"handled": 0, "retained": 1}
    with store.connect() as db:
        assert email_events.outcome(db, ticket["id"]) == "delivery"
    q.fail_delete = False
    assert poll(store, q) == {"handled": 1, "retained": 0}
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 1


@pytest.mark.parametrize(
    "field,value", [("TopicArn", "wrong"), ("Type", "SubscriptionConfirmation")]
)
def test_unexpected_envelopes_rejected(field, value):
    message = json.loads(body("outbox"))
    message[field] = value
    with pytest.raises(ValueError):
        parse(json.dumps(message), TOPIC, "123456789012", "relay")


@pytest.mark.parametrize("account,config", [("other", "relay"), ("123456789012", "other")])
def test_account_and_configuration_must_match(account, config):
    with pytest.raises(ValueError):
        parse(body("outbox"), TOPIC, account, config)


def test_unknown_correlation_not_acknowledged(queued):
    store, _, _, _ = queued
    q = Queue(body("missing"))
    assert poll(store, q) == {"handled": 0, "retained": 1}
    assert q.deleted == 0


def test_exact_setup_notice_is_not_a_delivery_event():
    envelope = {
        "Type": "Notification",
        "TopicArn": TOPIC,
        "Message": "Successfully validated SNS topic for Amazon SES event publishing.",
    }
    assert parse(json.dumps(envelope), TOPIC, "123456789012", "relay") is None
    envelope["TopicArn"] = "wrong"
    with pytest.raises(ValueError):
        parse(json.dumps(envelope), TOPIC, "123456789012", "relay")
