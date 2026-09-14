"""Participant-scoped pickup and recipient receipt views for durable reminders."""


def context(state, revision, kind, role, resource):
    plan = state.get("plan")
    if (
        not plan
        or plan["revision"] != revision
        or state["status"]
        not in ("committed", "in_transit", "awaiting_receipt", "discrepancy", "complete")
    ):
        return None
    failure = state.get("replacement_failure")
    if kind == "pickups" and role == "driver":
        assignment = next((d for d in plan["drivers"] if d["resource_id"] == resource), None)
        if not assignment or (failure and failure["driver"] == resource):
            return None
        return {
            "action": "pickup",
            "kg": assignment["kg"],
            "resource_id": resource,
            "already_confirmed": resource in plan["picked_up"],
            "can_submit": True,
            "received_kg": None,
        }
    if kind == "receipts" and role == "recipient":
        assignment = next((d for d in plan["recipients"] if d["resource_id"] == resource), None)
        if not assignment or failure:
            return None
        ready = all(
            leg["driver"] in plan["picked_up"]
            for leg in plan["legs"]
            if leg["recipient"] == resource
        )
        return {
            "action": "receive",
            "kg": assignment["kg"],
            "resource_id": resource,
            "already_confirmed": resource in plan["receipts"],
            "can_submit": ready,
            "received_kg": plan["receipts"].get(resource),
        }
    return None


def page(result, details):
    import html
    import json

    d = details["movement"]
    receipt = d["action"] == "receive"
    title = "Record received quantity" if receipt else "Record pickup"
    label = "Record received quantity" if receipt else "Confirm I collected this load"
    explanation = (
        f"Expected: {d['kg']} kg. Enter the actual quantity received. A short receipt stays unresolved."
        if receipt
        else f"Confirm only after collecting your assigned {d['kg']} kg. This records pickup, not delivery."
    )
    form = ""
    if d["already_confirmed"]:
        status = (
            f"Recorded receipt: {d['received_kg']} kg. Corrections require reconciliation."
            if receipt
            else "Your pickup is already recorded."
        )
    elif not d["can_submit"]:
        status = "Associated driver pickups are not yet recorded. Reopen this link after pickup confirmation."
    else:
        status = "Review the quantity before submitting."
        if receipt:
            form += f'<label>Actual kilograms received <input id="quantity" type="number" min="0" max="{d["kg"]}" step="1" required></label>'
        form += '<button id="confirm">' + label + "</button>"
    auth = json.dumps({"token": result["token"], "message": result["reminder_id"]}).replace(
        "<", r"\u003c"
    )
    return f"""<!doctype html><meta charset="utf-8"><title>Relay movement evidence</title>
<h1>{title}</h1><p>Revision {details["revision"]} · {html.escape(details["participant"])}</p>
<p>{html.escape(explanation)}</p>{form}<p id="status">{html.escape(status)}</p>
<script>const auth={auth};const button=document.getElementById('confirm');if(button)button.onclick=async()=>{{
const quantity=document.getElementById('quantity');if(quantity&&(!quantity.value.trim()||!quantity.checkValidity())){{document.getElementById('status').textContent='Enter a whole quantity within the expected load.';return;}}
button.disabled=true;try{{const response=await fetch('/email-actions/'+encodeURIComponent(auth.message)+'/confirm',{{method:'POST',headers:{{Authorization:'Bearer '+auth.token,'Content-Type':'application/json'}},body:JSON.stringify(quantity?{{received_kg:Number(quantity.value)}}:{{}})}});
const value=await response.json();document.getElementById('status').textContent=response.ok?(value.message||'Evidence recorded.'):(value.message||value.detail||'Submission failed. Reopen the reminder.');
}}catch(e){{document.getElementById('status').textContent='Response unavailable. Reopen this link to check the recorded evidence.';}}}};</script>"""
