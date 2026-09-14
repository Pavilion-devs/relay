# Shared commitments and durable execution

Implemented September 13, 2026. Engine-only milestone; the existing UI remains a disposable harness and was not changed.

## Reproduce the complete scenario

Run `uv run python scripts/rehearse_execution.py` from the repository root. The script creates a temporary SQLite database, lets two rescues race for one driver, requests cancellation, launches a worker process that claims work and abruptly exits, reopens the database, recovers work, finishes cancellation, completes the competing rescue, and generates a fresh revision for the original rescue. It writes `docs/execution-smoke.json`. No AWS inference, external messages or real deliveries are involved.

`uv run pytest -q` runs 47 tests, including 16 new execution cases. These check dispatch races, receiver capacity, transaction rollback, obsolete jobs, repeated commands, worker contention, lease fencing, failure retry limits, cancellation custody, API integration, and restart recovery. This is a development invariant suite, not a production or held-out agent benchmark.

## Shared reservations

A network registers stable participant IDs, roles and capacity ceilings. `Store.create_network(resources)` creates the registry. `Store.create(state, network_id)` joins a rescue to it; omitted network IDs create isolated rehearsal networks for backward compatibility. The planner reads remaining network capacity without changing the participant's recorded offer.

Dispatch approval checks and inserts reservations inside the same SQLite `BEGIN IMMEDIATE` transaction that stores the committed plan, command receipt and follow-up job. Driver reservations are exclusive. Recipient allocations cannot sum beyond the registered capacity. A conflict rolls back the proposed commitment; no partial reservations or committed audit events leak through. Replayed command IDs return their stored result without reallocating.

Reservations deliberately remain held across planned end times until successful recipient acknowledgment or resolved cancellation. This is conservative: it prevents overdue operations silently freeing a driver, but does not support scheduling separate future time slots or daily recipient replenishment. Registry capacities are fixed for this milestone. Rescue-specific offers can reduce their own allocation but do not redefine the network-wide capacity ceiling. Network administration and globally propagated availability changes remain future work.

Always invoke domain commands through `Store.transact`; `engine.apply` alone is a pure state transition and does not provide database reservations.

## Cancellation and custody

Commands reference the committed revision:

| Command | Required fields | Effect |
|---|---|---|
| `cancel` | `revision`, `reason` | Enter `cancelling`; hold reservations and schedule follow-up. |
| `ack_cancel` | `revision`, `resource_id` | Record acknowledgment from an assigned participant. |
| `ack_return` | `revision`, driver `resource_id`, `evidence` | Record donor acknowledgment of that driver's full collected load. |
| `recover` | none | After `cancelled`, archive the cancelled revision and return to planning. |

Cancellation completes only when every assigned participant acknowledged and all previously recorded pickups have return evidence. Evidence cannot be overwritten. Changing an active or cancelling operation is blocked. Old decisions cannot authorize a replacement revision. Pickup and receipt commands cannot continue an operation under cancellation.

An operation with existing recipient receipts requires a separate reconciliation workflow; cancellation cannot erase them. Quantity discrepancies retain reservations. Partial returns, post-receipt reconciliation, custody transfers and late physical movements during cancellation remain unsupported. These operations need explicit extensions, not manual deletion of reservations.

Acknowledgments are locally supplied rehearsal records. This milestone does not authenticate participants or verify physical donor returns.

## Durable follow-up worker

Run alongside the API:

```sh
uv run python -m relay_core.worker --db .data/relay.sqlite3
```

For one bounded pass, add `--once`. The worker writes only the SQLite participant inbox; no email, SMS or other external connector is called.

- Proposal and cancellation follow-up starts after one minute.
- Receipt follow-up starts fifteen minutes after the latest scheduled delivery finish.
- Jobs use a sixty-second lease with a fresh claim token. A crashed worker's job can be reclaimed; an expired or replaced token cannot write.
- Inbox insertion, job progress and escalation state commit together. A crash before commit leaves no partial inbox delivery.
- Successful reminders repeat after five minutes; the third claim escalates to `coordinator`. Claims lost to crashes count toward this bound.
- Processing exceptions retry with bounded backoff, then record a coordinator-owned failure escalation. Database outages can prevent both delivery and retry recording; the lease remains durable for recovery when storage is available and the worker restarts.
- Expired proposals escalate to the coordinator instead of requesting obsolete participant approvals.
- A superseded revision or finished operation makes its old jobs obsolete on the next worker pass.

This proves atomic local inbox delivery, not exactly-once delivery to an external service. A future external adapter must implement delivery identifiers, deduplication and explicit uncertain-delivery handling.

## Local API

- `POST /networks` creates a synthetic network with the standard fixture registry.
- `POST /workspaces` with `{"network_id":"..."}` creates a rescue in that network; `{}` remains isolated.
- `GET /networks/{id}/reservations` shows active and released reservations.
- `GET /workspaces/{id}/inbox` reads local reminders and escalations.
- Existing `POST /workspaces/{id}/commands` accepts cancellation commands and normal revision-bound commands.

These endpoints remain loopback-only and unauthenticated, like the existing rehearsal API. Production identity, AWS persistence and managed worker deployment are separate milestones. Do not treat workspace IDs as access control.
