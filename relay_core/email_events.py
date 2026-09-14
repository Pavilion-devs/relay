"""Provider-event reducer for a trusted consumer, not a public webhook."""

import hashlib
import json


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS email_events (
        event_id TEXT PRIMARY KEY, digest TEXT NOT NULL, outbox_id TEXT NOT NULL,
        provider_id TEXT NOT NULL, kind TEXT NOT NULL, observed REAL NOT NULL)""")


def record(store, event_id, outbox_id, provider_id, kind):
    """Call only after authenticating provider transport and validating SES correlation."""
    if kind not in ("delivery", "bounce", "complaint", "reject"):
        raise ValueError("Unsupported event")
    if not all(
        isinstance(x, str) and 0 < len(x) <= 256 for x in (event_id, outbox_id, provider_id)
    ):
        raise ValueError("Invalid event identifiers")
    digest = hashlib.sha256(json.dumps([outbox_id, provider_id, kind]).encode()).hexdigest()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT digest FROM email_events WHERE event_id=?", (event_id,)).fetchone()
        if old:
            if old[0] != digest:
                raise ValueError("Event identifier reused with different content")
            return False
        row = db.execute(
            "SELECT membership,state,provider_id FROM email_outbox WHERE id=?", (outbox_id,)
        ).fetchone()
        if not row or row[1] not in ("accepted", "uncertain", "sending"):
            raise ValueError("No correlated send attempt")
        if row[2] is not None and row[2] != provider_id:
            raise ValueError("Provider identifier mismatch")
        db.execute(
            "INSERT INTO email_events VALUES(?,?,?,?,?,?)",
            (event_id, digest, outbox_id, provider_id, kind, store.clock()),
        )
        db.execute(
            "UPDATE email_outbox SET state='accepted',provider_id=?,lease=NULL WHERE id=?",
            (provider_id, outbox_id),
        )
        if kind in ("bounce", "complaint"):
            db.execute(
                "INSERT INTO email_subscriptions VALUES(?,0) ON CONFLICT(membership) DO UPDATE SET enabled=0",
                (row[0],),
            )
            db.execute(
                "UPDATE email_outbox SET state='obsolete' WHERE membership=? AND state='pending'",
                (row[0],),
            )
    return True


def outcome(db, outbox_id):
    kinds = {
        r[0] for r in db.execute("SELECT kind FROM email_events WHERE outbox_id=?", (outbox_id,))
    }
    return next((k for k in ("complaint", "bounce", "reject", "delivery") if k in kinds), None)
