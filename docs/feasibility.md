# Route feasibility milestone

September 12, 2026. This is a local implementation with synthetic operator inputs, not live routing or field validation.

Relay previously allocated kilograms independently to drivers and recipients. It now verifies a concrete single-pickup schedule before participants can accept it. A route includes driver departure, loading, ordered receiving stops, waiting for openings and unloading. The plan's expiry is no later than its first driver departure. New logistics facts supersede all prior decisions.

## Inputs and constraints

- Absolute timezone-aware rehearsal start, with windows expressed as integer minute offsets.
- Driver availability and receiving windows; loading must finish inside the donor's pickup window, unloading inside the recipient and driver windows.
- Explicit directed travel minutes. Missing reverse legs are not inferred from forward legs. Missing inputs request clarification rather than guessed travel.
- Operator-confirmed handling categories, matched against the rescue's requirement. Compatibility is not food-safety certification.
- Driver and recipient weight capacities. Splitting between drivers and recipients is supported; received quantities remain separately acknowledged.
- A five-minute minimum decision allowance before driver departure and the existing overall rescue deadline.

The `TravelTimes` protocol isolates travel lookup. `MatrixTravelTimes` is the implemented adapter. A future provider must supply verified directed durations; the language model never invents them.

## Search behavior

For each driver, enumerate recipient subsets and stop permutations, retaining the least-driving feasible schedule per subset. Across combinations, an integral max-flow calculation assigns kilograms without exceeding either side's capacity. Choose the feasible combination with the lowest aggregate driving minutes, breaking ties by fewer drivers. This optimizes the declared small model, not general vehicle routing or disruption to prior commitments.

The search is limited to three available, handling-compatible drivers and four recipients: at most 16 subset choices per driver and 4,096 combinations. Larger inputs explicitly escalate with `PLANNER_LIMIT`. A missing input has its own outcome. `NO_FEASIBLE_ROUTE` means no full solution within the configured model and decision allowance, not that rescue is impossible under every operating arrangement.

Directed input matrices may be non-metric. A scheduled stop can carry zero allocated kilograms; the route retains that stop and its time checks instead of silently replacing its two travel legs with a potentially slower direct leg. The interface distinguishes such a stop from a delivery. This conservative behavior can require extra visits; a later routing adapter can distinguish transit waypoints from recipient service.

## Demonstrate changed outcomes

1. Start a new rehearsal and build the initial proposal.
2. Edit Harbour's closing offset to 40 minutes. Its kilograms still exist, but the delivery cannot arrive and unload in time. A new proposal is rejected.
3. Increase Garden's receiving capacity to 320 kg. Replanning now uses Garden.
4. Try an acceptance for the old revision: it is rejected.
5. Confirm the new assignments, approve before the displayed departure deadline, then record pickup and recipient acknowledgment.

The form edits synthetic inputs. Pickup/receipt controls are presenter-operated rehearsal events and do not enforce actual wall-clock travel; they are not physical delivery verification.

For a reproducible no-model HTTP check, run the local frontend and API, then `uv run python scripts/rehearse_routes.py` from the repository root. It creates an isolated synthetic workspace and writes `docs/route-recovery-smoke.json`. It sends no messages to real participants.

## Verification and remaining work

The 31-test suite includes 15 feasibility/route-migration cases plus the existing 16 domain/Strands tests. Cases cover a greedy allocation dead end, alternative stop order, closed windows, loading/unloading boundaries, waiting, missing travel, handling exclusion, route expiry, stale decisions and incompatible legacy plans. A separate HTTP run reached a recorded 320 kg recipient acknowledgment after a route failure and recovery. These are development cases, not held-out product evaluation or a comparison with Surplus Router.

Remaining: authenticated participant access, shared reservations across rescues, durable follow-up/outbox work, cancellation and compensation, volume constraints, donor loading-bay contention, return travel, live traffic, held-out natural-language evaluation and AWS deployment. The local Strands adapter remains connected to Bedrock; its draft tool does not yet capture natural-language time-window changes, and its prompt directs those to clarification.
