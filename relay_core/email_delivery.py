"""Opt-in email outbox. Provider acceptance is not recipient delivery."""

import json
from uuid import uuid4

from . import access, email_events, movement_actions, replacement_actions


def initialize(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS email_consent_audit (
      id TEXT PRIMARY KEY, membership TEXT NOT NULL, actor TEXT NOT NULL,
      enabled INTEGER NOT NULL, recorded REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS email_subscriptions (membership TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS email_outbox (
      id TEXT PRIMARY KEY, inbox_id TEXT NOT NULL, membership TEXT NOT NULL,
      revision INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
      due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, lease TEXT,
      lease_until REAL, provider_id TEXT, UNIQUE(inbox_id,membership));
    """)


def enqueue(db, inbox_id, workspace, recipient, revision, now):
    network = json.loads(
        db.execute("SELECT state FROM workspaces WHERE id=?", (workspace,)).fetchone()[0]
    )["network_id"]
    rows = db.execute(
        """SELECT i.id FROM invitations i JOIN email_subscriptions s ON s.membership=i.id
      WHERE i.network=? AND COALESCE(i.resource,i.role)=? AND i.subject IS NOT NULL AND i.revoked=0 AND s.enabled=1""",
        (network, recipient),
    ).fetchall()
    for (membership,) in rows:
        db.execute(
            "INSERT OR IGNORE INTO email_outbox(id,inbox_id,membership,revision,due) VALUES(?,?,?,?,?)",
            (str(uuid4()), inbox_id, membership, revision, now),
        )


class RetryableRejection(Exception):
    """Provider explicitly rejected without accepting the message, e.g. throttling."""


class PermanentRejection(Exception):
    pass


def claim(store):
    now = store.clock()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # A crashed send may have reached the provider. Never automatically resend.
        db.execute(
            "UPDATE email_outbox SET state='uncertain',lease=NULL WHERE state='sending' AND lease_until<=?",
            (now,),
        )
        rows = db.execute(
            "SELECT id,inbox_id,membership,revision,attempts FROM email_outbox WHERE state='pending' AND due<=? ORDER BY due,id",
            (now,),
        ).fetchall()
        for eid, inbox_id, membership, revision, attempts in rows:
            row = db.execute(
                """SELECT i.email,i.revoked,s.enabled,b.workspace,b.body,i.role,i.resource FROM invitations i
              JOIN email_subscriptions s ON s.membership=i.id JOIN inbox b ON b.id=? WHERE i.id=?""",
                (inbox_id, membership),
            ).fetchone()
            valid = False
            if row and not row[1] and row[2]:
                state = json.loads(
                    db.execute("SELECT state FROM workspaces WHERE id=?", (row[3],)).fetchone()[0]
                )
                plan = state.get("plan")
                valid = bool(
                    plan
                    and plan["revision"] == revision
                    and state["status"] not in ("complete", "cancelled")
                )
            if valid:
                job = db.execute(
                    "SELECT kind,id FROM jobs WHERE id=?", (inbox_id.rsplit(":", 2)[0],)
                ).fetchone()
                recipient = db.execute(
                    "SELECT recipient FROM inbox WHERE id=?", (inbox_id,)
                ).fetchone()[0]
                if not job:
                    valid = False
                elif job[0] == "replacement":
                    details = replacement_actions.context(
                        state, revision, job[1].rsplit(":", 1)[-1], row[5], row[6], now
                    )
                    valid = bool(details and not details["already_confirmed"])
                elif job[0] in ("pickups", "receipts") and recipient != "coordinator":
                    details = movement_actions.context(state, revision, job[0], row[5], row[6])
                    valid = bool(details and not details["already_confirmed"])
                elif recipient != "coordinator":
                    kind = job[0]
                    if kind == "confirmations":
                        valid = (
                            state["status"] == "awaiting_confirmations"
                            and recipient not in plan["accepted"]
                        )
                    elif kind == "cancellation":
                        valid = state["status"] == "cancelling" and recipient not in plan.get(
                            "cancellation_acks", []
                        )
                    elif kind == "receipts":
                        valid = (
                            state["status"]
                            in ("committed", "in_transit", "awaiting_receipt", "discrepancy")
                            and recipient not in plan["receipts"]
                        )
            if not valid:
                db.execute("UPDATE email_outbox SET state='obsolete' WHERE id=?", (eid,))
                continue
            unresolved = db.execute(
                "SELECT inbox_id FROM email_outbox WHERE membership=? AND revision=? AND state IN ('sending','uncertain')",
                (membership, revision),
            ).fetchall()
            job_id = inbox_id.rsplit(":", 2)[0]
            if any(old[0].rsplit(":", 2)[0] == job_id for old in unresolved):
                continue  # Keep pending; operator must resolve the earlier uncertain send.
            lease = str(uuid4())
            db.execute(
                "UPDATE email_outbox SET state='sending',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                (lease, now + 60, eid),
            )
            return {
                "id": eid,
                "lease": lease,
                "to": row[0],
                "body": row[4],
                "attempt": attempts + 1,
            }
    return None


def finish(store, ticket, state, provider_id=None):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute(
            """UPDATE email_outbox SET state=?,provider_id=?,due=?,lease=NULL
            WHERE id=? AND lease=? AND state='sending' AND lease_until>?""",
            (
                state,
                provider_id,
                store.clock() + 60 * 2 ** min(ticket["attempt"], 5),
                ticket["id"],
                ticket["lease"],
                store.clock(),
            ),
        ).rowcount
    return bool(changed)


def run_once(store, provider):
    ticket = claim(store)
    if ticket is None:
        return False
    try:
        if hasattr(provider, "send_queued"):
            mid = provider.send_queued(ticket)
        else:
            mid = provider.send(ticket["to"], ticket["body"])
        if not isinstance(mid, str) or not mid:
            raise ValueError("Provider acceptance ID missing")
    except RetryableRejection:
        finish(store, ticket, "pending" if ticket["attempt"] < 3 else "failed")
    except PermanentRejection:
        finish(store, ticket, "failed")
    except Exception:  # noqa: BLE001 - unknown send outcomes must remain uncertain
        # Includes timeouts and unclassified failures. No invented delivery success.
        finish(store, ticket, "uncertain")
    else:
        finish(store, ticket, "accepted", mid)
    return True


class SESProvider:
    def __init__(self, sender, region, configuration_set=None, action_base_url=None):
        from urllib.parse import urlsplit

        import boto3
        from botocore.config import Config

        if action_base_url:
            u = urlsplit(action_base_url)
            if (
                (
                    u.scheme != "https"
                    and not (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1"))
                )
                or not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
                or u.path not in ("", "/")
            ):
                raise ValueError("Action origin must be HTTPS or local loopback")
        self.action_base_url = action_base_url.rstrip("/") if action_base_url else None
        self.configuration_set = configuration_set
        self.sender = sender
        self.client = boto3.client(
            "sesv2",
            region_name=region,
            config=Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1}),
        )

    def send_queued(self, ticket):
        body = ticket["body"]
        if self.action_base_url:
            from urllib.parse import quote

            body += (
                "\n\nSign in to review this request: "
                + self.action_base_url
                + "/reminders/"
                + quote(ticket["id"], safe="")
            )
        return self.send(ticket["to"], body, delivery_id=ticket["id"])

    def send(self, destination, body, delivery_id=None):
        from botocore.exceptions import ClientError

        try:
            options = (
                {"ConfigurationSetName": self.configuration_set} if self.configuration_set else {}
            )
            response = self.client.send_email(
                **options,
                EmailTags=[{"Name": "relay_outbox_id", "Value": delivery_id}]
                if delivery_id
                else [],
                FromEmailAddress=self.sender,
                Destination={"ToAddresses": [destination]},
                Content={
                    "Simple": {
                        "Subject": {"Data": "Relay: commitment needs attention"},
                        "Body": {"Text": {"Data": body}},
                    }
                },
            )
            return response["MessageId"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code == "TooManyRequestsException":
                raise RetryableRejection() from exc
            if code in (
                "MessageRejected",
                "MailFromDomainNotVerifiedException",
                "BadRequestException",
                "AccountSuspendedException",
                "SendingPausedException",
            ):
                raise PermanentRejection() from exc
            raise


def consent(store, token, enabled=None):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        actor = access.principal(db, token, store.clock())
        row = db.execute(
            "SELECT i.id FROM identity_sessions s JOIN invitations i ON i.id=s.membership_id WHERE s.grant_id=? AND i.revoked=0",
            (actor["id"],),
        ).fetchone()
        if not row:
            raise PermissionError("A signed-in participant membership is required")
        membership = row[0]
        if (
            enabled is True
            and db.execute(
                "SELECT 1 FROM email_events v JOIN email_outbox e ON e.id=v.outbox_id WHERE e.membership=? AND v.kind IN ('bounce','complaint') LIMIT 1",
                (membership,),
            ).fetchone()
        ):
            raise PermissionError("Email is suppressed after provider feedback")
        if enabled is not None:
            if type(enabled) is not bool:
                raise ValueError("Consent must be a boolean")
            db.execute(
                "INSERT INTO email_subscriptions VALUES(?,?) ON CONFLICT(membership) DO UPDATE SET enabled=excluded.enabled",
                (membership, int(enabled)),
            )
            db.execute(
                "INSERT INTO email_consent_audit VALUES(?,?,?,?,?)",
                (str(uuid4()), membership, actor["id"], int(enabled), store.clock()),
            )
            if not enabled:
                db.execute(
                    "UPDATE email_outbox SET state='obsolete' WHERE membership=? AND state='pending'",
                    (membership,),
                )
        setting = db.execute(
            "SELECT enabled FROM email_subscriptions WHERE membership=?", (membership,)
        ).fetchone()
        return {"enabled": bool(setting and setting[0]), "membership_id": membership}


def status_for_network(store, token, network):
    with store.connect() as db:
        actor = access.principal(db, token, store.clock())
        if actor["role"] != "coordinator" or actor["network_id"] != network:
            raise PermissionError("Network coordinator required")
        rows = db.execute(
            """SELECT e.id,b.workspace,e.revision,i.resource,e.state,e.attempts,e.provider_id
            FROM email_outbox e JOIN invitations i ON i.id=e.membership JOIN inbox b ON b.id=e.inbox_id
            WHERE i.network=? ORDER BY e.due,e.id LIMIT 500""",
            (network,),
        ).fetchall()
        result = [
            dict(
                zip(
                    (
                        "id",
                        "workspace_id",
                        "revision",
                        "resource_id",
                        "state",
                        "attempts",
                        "provider_id",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in rows
        ]

        for item in result:
            item["provider_outcome"] = email_events.outcome(db, item["id"])
        return result
