"""Authenticated email actions: message IDs are locators, never access grants."""

from . import access, movement_actions, replacement_actions
from .engine import apply


def resolve(db, store, token, message_id):
    actor = access.principal(db, token, store.clock())
    row = db.execute(
        """SELECT b.workspace,e.revision,i.resource,b.id,i.id FROM email_outbox e
        JOIN inbox b ON b.id=e.inbox_id JOIN invitations i ON i.id=e.membership WHERE e.id=?""",
        (message_id,),
    ).fetchone()
    if not row:
        raise PermissionError("Reminder unavailable")
    job = db.execute("SELECT kind,id FROM jobs WHERE id=?", (row[3].rsplit(":", 2)[0],)).fetchone()
    if not job:
        raise PermissionError("Reminder unavailable")
    row = (*row[:3], job[0], row[4], job[1].rsplit(":", 1)[-1] if job[0] == "replacement" else None)
    linked = db.execute(
        "SELECT membership_id FROM identity_sessions WHERE grant_id=?", (actor["id"],)
    ).fetchone()
    if not linked or linked[0] != row[4]:
        raise PermissionError("This reminder belongs to another membership")
    return actor, row


def confirm(store, token, message_id, evidence=None, received_kg=None):
    with store.connect() as db:
        actor, row = resolve(db, store, token, message_id)
    workspace, revision, resource, kind, _, request_id = row
    if kind not in ("confirmations", "replacement", "pickups", "receipts"):
        raise PermissionError("This reminder is not a confirmation request")
    payload = {
        "action": "accept",
        "resource_id": resource,
        "revision": revision,
        "actor_id": actor["id"],
    }

    if kind in ("pickups", "receipts"):
        details = movement_actions.context(
            store.read(workspace), revision, kind, actor["role"], resource
        )
        if not details or not details["can_submit"]:
            raise PermissionError("Movement request is unavailable or awaits pickup evidence")
        payload["action"] = details["action"]
        if kind == "receipts":
            if type(received_kg) is not int or not 0 <= received_kg <= details["kg"]:
                raise ValueError("Enter an actual whole quantity from zero to the assigned load")
            payload["received_kg"] = received_kg

    if kind == "replacement":
        details = replacement_actions.context(
            store.read(workspace), revision, request_id, actor["role"], resource, store.clock()
        )
        if not details:
            raise PermissionError("This replacement request is no longer current")
        payload.update(
            action=details["action"],
            resource_id=details["resource_id"],
            replacement_request_id=request_id,
        )
        if details["action"] == "confirm_return":
            if not isinstance(evidence, str) or not evidence.strip():
                raise PermissionError("Full-return evidence is required")
            payload["evidence"] = evidence

    def guard(db, state):
        _current, fresh = resolve(db, store, token, message_id)
        if fresh != row:
            raise PermissionError("Reminder changed")
        if kind == "replacement":
            details = replacement_actions.context(
                state, revision, request_id, actor["role"], resource, store.clock()
            )
            if (
                not details
                or details["action"] != payload["action"]
                or details["resource_id"] != payload["resource_id"]
            ):
                raise PermissionError("This replacement request is no longer current")
        if kind in ("pickups", "receipts"):
            details = movement_actions.context(state, revision, kind, actor["role"], resource)
            if not details or not details["can_submit"]:
                raise PermissionError("Movement request is unavailable or awaits pickup evidence")
        return access.authorize(
            db, token, store.clock(), state, payload["action"], payload["resource_id"]
        )

    result = store.transact(
        workspace,
        "email-confirm:" + message_id + ":" + actor["id"],
        payload,
        lambda s: apply(s, payload),
        guard=guard,
    )

    if result["ok"] and kind in ("pickups", "receipts"):
        result["movement_status"] = result["state"]["status"]
        if kind == "pickups":
            result["message"] = "Pickup recorded. Recipient receipt is still required."
        elif result["state"]["status"] == "discrepancy":
            result["message"] = (
                f"Receipt recorded: {received_kg} kg. A quantity discrepancy remains for coordinator review."
            )
        else:
            result["message"] = f"Receipt recorded: {received_kg} kg."
    return result


def review(store, token, message_id):
    with store.connect() as db:
        actor, row = resolve(db, store, token, message_id)
        state = store.read(row[0])
        access.authorize(db, token, store.clock(), state, "read")
    from datetime import datetime

    if row[3] in ("pickups", "receipts"):
        details = movement_actions.context(state, row[1], row[3], actor["role"], row[2])
        return {
            "revision": row[1],
            "current": details is not None,
            "already_confirmed": bool(details and details["already_confirmed"]),
            "participant": row[2],
            "movement": details,
            "plan": None,
        }
    if row[3] == "replacement":
        details = replacement_actions.context(
            state, row[1], row[5], actor["role"], row[2], store.clock()
        )
        return {
            "revision": row[1],
            "current": details is not None,
            "already_confirmed": bool(details and details["already_confirmed"]),
            "participant": row[2] or actor["role"],
            "replacement": details,
            "plan": None,
        }
    plan = state.get("plan")
    active = bool(
        plan
        and plan["revision"] == row[1]
        and state["status"] == "awaiting_confirmations"
        and row[3] == "confirmations"
        and store.clock() < datetime.fromisoformat(plan["expires_at"]).timestamp()
    )
    projection = access.project(state, actor)
    return {
        "revision": row[1],
        "current": active,
        "already_confirmed": bool(plan and row[2] in plan["accepted"]),
        "participant": row[2],
        "plan": projection["plan"] if active else None,
    }


def review_page(store, result):
    import html
    import json

    details = review(store, result["token"], result["reminder_id"])
    if not details["current"]:
        return "<!doctype html><title>Relay reminder</title><h1>This request is no longer current</h1><p>Ask your coordinator for the latest plan.</p>"
    if details.get("movement"):
        return movement_actions.page(result, details)
    if details.get("replacement"):
        return replacement_page(result, details)
    assignments = details["plan"].get("assignments", [])
    summary = "".join(
        "<li>" + html.escape(str(a.get("kg", "?"))) + " kg assigned</li>" for a in assignments
    )
    route_details = html.escape(
        json.dumps(details["plan"].get("routes", details["plan"].get("legs", [])), indent=2)
    )
    data = json.dumps({"token": result["token"], "message": result["reminder_id"]}).replace(
        "<", "\u003c"
    )
    button = (
        ""
        if details["already_confirmed"]
        else '<button id="confirm">Confirm my commitment</button>'
    )
    status = (
        "You have already confirmed this revision."
        if details["already_confirmed"]
        else "Review your assignment before confirming."
    )
    return f"""<!doctype html><meta charset="utf-8"><title>Relay commitment review</title>
<h1>Review revision {details["revision"]}</h1><p>{html.escape(details["participant"])}</p><ul>{summary}</ul>
<p>Request expires: {html.escape(str(details["plan"].get("expires_at", "")))}</p>
<h2>Planned route</h2><pre>{route_details}</pre>
{button}<p id="status">{status}</p>
<script>const auth={data};const button=document.getElementById('confirm');if(button)button.onclick=async()=>{{
button.disabled=true;try{{const r=await fetch('/email-actions/'+encodeURIComponent(auth.message)+'/confirm',{{method:'POST',headers:{{Authorization:'Bearer '+auth.token}}}});
const value=await r.json();document.getElementById('status').textContent=r.ok?'Your commitment is confirmed.':(value.message||value.detail||'Confirmation failed. Reopen the reminder.');
}}catch(e){{document.getElementById('status').textContent='Response unavailable. Reopen the reminder to check your confirmation.';}}}};</script>"""


def replacement_page(result, details):
    import html
    import json

    d = details["replacement"]
    action = d["action"]
    if action == "ack_stop":
        explanation = f"A replacement is proposed for your {d['kg']} kg load. Confirm that you have stopped this assignment. This does not confirm return of any collected food."
        label = "Confirm I have stopped"
    elif action == "confirm_return":
        explanation = f"Record donor evidence that the entire {d['kg']} kg load from {d['failed_driver']} has returned. A partial return cannot be recorded here."
        label = "Record full return evidence"
    else:
        verb = "collect and deliver" if d["role"] == "driver" else "receive"
        explanation = f"Review the proposal to {verb} {d['kg']} kg with replacement driver {d['replacement_driver']}. Unchanged driver commitments stay in place."
        label = "Confirm this replacement"
    reason = html.escape(d["reason"])
    route = html.escape(json.dumps(d["routes"], indent=2))
    form = ""
    if not d["already_confirmed"]:
        if action == "confirm_return":
            form += '<label>Return evidence <textarea id="evidence" required maxlength="2000"></textarea></label>'
        form += '<button id="confirm">' + label + "</button>"
    auth = json.dumps({"token": result["token"], "message": result["reminder_id"]}).replace(
        "<", r"\u003c"
    )
    status = (
        "Your response is already recorded."
        if d["already_confirmed"]
        else "Review before responding."
    )
    return f"""<!doctype html><meta charset="utf-8"><title>Relay replacement review</title>
<h1>Review partial replacement</h1><p>Current plan: revision {details["revision"]}</p>
<p>{html.escape(explanation)}</p><p>Reason: {reason}</p>
<p>Expires: {html.escape(d["expires_at"])}</p><pre>{route}</pre>
<p>Coordinator approval is still required before the replacement is committed.</p>
{form}<p id="status">{status}</p>
<script>const auth={auth};const button=document.getElementById('confirm');if(button)button.onclick=async()=>{{
const evidence=document.getElementById('evidence');if(evidence&&!evidence.value.trim()){{document.getElementById('status').textContent='Enter full-return evidence first.';return;}}
button.disabled=true;try{{const response=await fetch('/email-actions/'+encodeURIComponent(auth.message)+'/confirm',{{method:'POST',headers:{{Authorization:'Bearer '+auth.token,'Content-Type':'application/json'}},body:JSON.stringify(evidence?{{evidence:evidence.value}}:{{}})}});
const value=await response.json();document.getElementById('status').textContent=response.ok?'Your response is recorded. Coordinator approval is still required.':(value.message||value.detail||'Response failed. Reopen the reminder.');
}}catch(e){{document.getElementById('status').textContent='Response unavailable. Reopen the reminder to check the recorded state.';}}}};</script>"""
