# Relay public dashboard — presenter notes

**Public link:** https://instrument-generations-reduce-experiments.trycloudflare.com

No sign-in required. Every browser gets an isolated synthetic rescue. The temporary AWS-hosted link stops **October 9, 2026 at 02:00 Lagos / 01:00 UTC**. Cloudflare Quick Tunnel has no uptime SLA. The laptop does not need to remain connected.

## Start with the live agent — 30 seconds

Open **Messages & AI**. Select **Amara** and enter:

> I can carry 160 kg and I am available.

Click **Interpret message**. This makes one real Claude Sonnet 4.6 call through Amazon Bedrock and Strands. Show the source quote, proposed capacity and availability. Then click **Apply reviewed update**, and open **Participants** to see the new capacity. Interpretation alone does not change facts.

For a clarification example, select Tunde and type “I can take 24.” Relay should ask whether the units are kilograms or crates. Live outputs can vary; clarification is an honest result, not a broken demo. Four interpretation requests are allowed per rescue, with a persistent shared $6.50 model allocation, including the additional $5 authorized for judging. Structured controls work after that allowance is exhausted.

## Show actual operations — around 2 minutes

1. **Commitments → Generate plan.** The engine computes assignments from current facts. With the starting resources, Tunde covers 192 kg and Amara covers 128 kg; Amara's 160 kg capacity still permits that 128 kg allocation. Confirm separately as Tunde, Amara and Harbour, then **Approve dispatch** as coordinator.
2. **Record Tunde's pickup.** Open **Recovery**. Choose Tunde as failed driver and Ada as replacement. Enter a synthetic vehicle-breakdown reason, check that replacement availability was reported, and **Propose replacement**.
3. **Acknowledge stop**, confirm as Ada and Harbour, then **Commit replacement** before recording the return. The engine returns `CUSTODY_UNRESOLVED`. Acceptance does not account for food already collected.
4. In **Commitments**, record Amara's pickup. Return to **Recovery**. Enter synthetic donor return evidence, **Confirm return as donor**, then **Commit replacement**. Revision 2 preserves Amara's 128 kg assignment and pickup.
5. In **Commitments**, record Ada's pickup and the recipient's 320 kg receipt. **Overview** shows completion; **Activity log** shows the actual events and rejection responses.

All steps are independently clickable controls, not a scripted “next” sequence. You are explicitly acting as simulated participants. No email or real dispatch is sent. Travel estimates and food movement remain synthetic.

## Other working controls

- **Participants:** select a participant, edit capacity/availability/conditions, provide a source, and save. Fact changes invalidate earlier proposals; committed operations require recovery.
- **Commitments:** recalculate an uncommitted plan, approve, record pickups, record exact receipt weight, request cancellation, acknowledge cancellation and donor returns, or recover after cancellation. A short receipt creates a discrepancy.
- **Recovery:** propose a partial replacement, collect separate confirmations and custody evidence, and commit only when allowed.
- **Activity log:** inspect recorded events and successful/rejected command responses.
- **New rescue:** create a fresh isolated workspace. Three starts per IP per minute, 300 total starts, 100 active sessions, 45-minute sessions. Planning windows can expire before session expiry; use a fresh rescue when needed.

## Verified evidence

- **277 tests pass**; Ruff and JavaScript syntax checks pass.
- The complete public browser flow passed from live AI draft through human review, editable facts, participant confirmations, custody rejection, replacement and 320 kg receipt.
- Additional public live calls verified a minute-80 time draft and a missing-units clarification. Retrying the same request did not create another draft or model call. See `demo-public-ai-01.json`.
- Public role restrictions, session isolation, cross-origin rejection and private API isolation passed.
- Cloud API/worker/maintenance/backup readiness remained healthy. Persistent model reservations survive companion restarts.

These are synthetic demonstrations and bounded tests. They do not establish field impact, production readiness or superiority to Surplus Router. No cloud reboot or destructive restore was performed during this change.

## Fallback

The same dashboard is available locally at http://127.0.0.1:8780 while the local process runs. Local live model calls are disabled; use its structured controls if the public network is unavailable. The original API at 127.0.0.1:8765 remains running. Avoid tunnel restarts before judging: restarting cloudflared can change the public URL.
