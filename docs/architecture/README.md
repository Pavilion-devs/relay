# Relay public demo architecture

## Artifacts

- [Editable SVG master](relay-architecture.svg)
- [2240 × 1460 PNG](relay-architecture.png)
- [Self-contained local HTML view](relay-architecture.html)
- [Browser verification receipt](verification.json)

The diagram describes the implementation and deployment recorded in the September 14–15, 2026 evidence. It does not assert that the temporary environment is currently running. No live service, model, deployment or AWS resource was invoked or changed to produce these artifacts.

## Reading the diagram

Green arrows show command/state traffic. Blue arrows show interpretation. Dashed arrows show durable work and backup. Double-ended arrows represent request/response or read/write relationships, not independent public access. AWS contains EC2, Bedrock and S3; Cloudflare is outside AWS. EBS is shown as the encrypted volume mounted by EC2 and holding SQLite. Cost limits, normalization details and operational controls are documented below rather than filling the overview with implementation notes.

The AI extraction box is a module group inside the public companion process, not another container or autonomous agent. Docker Compose runs the companion, private engine API, worker and maintenance containers. Cloudflared runs separately on the EC2 host and is shown as the entry annotation above the companion. HTTPS carries both message/review responses and user actions through the same tunnel and companion. The browser has no direct engine, Bedrock or database access.

Human review is between an interpreted draft and the command that applies its proposed facts. Applying a draft is not dispatch approval. Participant confirmations, coordinator approval, pickup, donor return, replacement commitment and recipient receipt are separate commands. The engine checks applicable role, fact version, revision, capacity, route and custody conditions inside transactional processing. Replacement changes only the failed assignment and preserves unaffected commitments and pickups.

## Actual path and separate rehearsals

- The public dashboard is plain JavaScript/HTML/CSS in `demo/`. The React/Vinext harness in `web/` is not the public judging UI.
- The companion creates synthetic networks and opaque role grants for each session. Roles are simulated; public participant identity is not verified. Grants stay server-side. The session cookie is HttpOnly, Secure and SameSite=Lax.
- `/demo/interpret` records a sender-scoped message and calls the existing Strands `Agent` using `BedrockModel` with `tools=[]`. Claude Sonnet 4.6 extracts source spans; typed validation and deterministic Python normalize units and times. The result is a persisted draft or clarification. No planner/dispatch tool is called by the agent.
- CountTokens runs before an atomic persistent cost reservation, which runs before Converse. There is one attempt, a 1,000-token output limit, four requests per session and a shared $1.50 reservation cap. Unknown outcomes retain their reservation. The region used to contact Bedrock is us-east-1; the US inference profile can route across permitted US regions.
- Both scoped AI writes and engine command writes use the same SQLite/WAL database. The private API's own `/workspaces/{workspace}/messages` model path is disabled in the recorded public deployment. Public interpretation does not pass through that HTTP endpoint.
- The worker claims durable jobs using leases/fencing and writes a local inbox. Maintenance produces SQLite snapshots and uploads encrypted backups to versioned S3. Enabling an email sender is not part of this public deployment.
- Cognito identity and SES delivery were exercised in separate authenticated integration rehearsals, outside this diagram's public demo. SNS, SQS, DynamoDB, Lambda, AgentCore, Amazon Location, MCP and multiple autonomous agents are absent from the active path.
- People, quantities, travel matrices and food movement are synthetic. Recorded model calls and engine transactions are real. This task did not rerun them.

## Source map

Paths below are relative to the repository root. Current implementation was inspected alongside the deployment evidence; no `.env`, credentials, cookies or `.data/` content was read.

| Component / relationship | Evidence |
|---|---|
| Browser UI, message entry, review/apply and independent commands | `demo/app.js`: `messages()`, `command()`, submit handler; `demo/server.py`: static routes, `/demo/action` and `/demo/interpret` |
| Browser → HTTPS → Cloudflare → host daemon → companion | `docs/demo-launch-status.md`: public tunnel origin and deployed daemon; `deploy/compose.demo.yaml`: loopback 8780 binding; `infra/private-rehearsal-public-ai.json`: empty security-group ingress |
| Session network, grants and secure cookie | `demo/server.py`: `Sandbox`, `start()`, `get_session()`; `relay_core/access.py`: `issue()`, `authorize()`, `ScopedStore` |
| Companion → scoped message → Strands / Bedrock | `demo/server.py`: `message()` and `run_interpretation()`; `demo/demo_ai.py`: `run()`; `relay_core/agent.py`: `interpret()`, `tools=[]` |
| CountTokens → reservation → Converse | `demo/demo_ai.py`: `CountedClient.converse()`, `reserve()`; `infra/private-rehearsal-public-ai.json`: selected inference-profile/model IAM resources and CountTokens permission |
| Bedrock spans → typed normalization → draft / clarification → browser review | `relay_core/agent.py`: `Extraction.model_validate_json`, `normalize`, `record`; `relay_core/extraction.py`: `Extraction`, `capacity_kg`, `minute_offset`, source coverage; `demo/app.js`: `messages()` |
| Companion → scoped private engine command | `demo/server.py`: `Sandbox.command()`, role allowlist, `/demo/action`; `relay_core/api.py`: `/workspaces/{workspace}/commands`, authorization guard |
| Engine → validated transaction → shared SQLite | `relay_core/store.py`: `transact()` / `BEGIN IMMEDIATE`, authorization, command digest/idempotency, execution synchronization; `relay_core/access.py`: `authorize()`; `relay_core/engine.py`: `apply()` |
| Route feasibility and partial replacement | `relay_core/planner.py`: typed logistics, `MatrixTravelTimes`, `solve()`; `relay_core/replacement.py`: `apply()`; `relay_core/engine.py`: review, custody, pickup and receipt commands |
| Scoped AI read/write → same SQLite, persistent AI budget | `relay_core/access.py`: `ScopedStore.read/transact`; `relay_core/agent.py`: persisted interpretation transaction; `demo/demo_ai.py`: `public_ai_budget`; shared mounts in Compose files |
| SQLite ↔ durable worker / local inbox | `relay_core/store.py`: `claim_job`, `deliver_job`, `run_due`; `relay_core/execution.py`: `claim`, `deliver`, `fail`, reservations/jobs/inbox tables; `relay_core/operations.py`: deployed worker loop; `relay_core/worker.py`: separate CLI with optional email sender |
| SQLite → maintenance → S3 backup | `relay_core/operations.py`: `backup()`, SQLite backup API, S3 upload with AES256; `deploy/compose.yaml`, `deploy/compose.host.yaml`; infrastructure template: encrypted EBS and encrypted/versioned bucket |
| AWS / EC2 container and volume boundaries | All three `deploy/compose*.yaml` files; `infra/private-rehearsal-public-ai.json`; `docs/demo-launch-status.md` |
| Operations rail | `infra/private-rehearsal-public-ai.json`: CloudFormation resources, IAM instance role, SSM permissions, stop schedule; `docs/demo-launch-status.md`: host expiry timer evidence |
| Recorded end-to-end behavior | `docs/demo-dashboard-verification.json`: complete recovery, unaffected pickup preserved, final receipt; `docs/demo-public-ai-01.json`: draft, clarification, review and isolation checks |
| Actual dependencies | `pyproject.toml`, `uv.lock`: Strands Agents 1.55.1, FastAPI 0.141.1, boto3 1.43.92, Pydantic 2.13.5 |

## 20-second spoken explanation

“Relay turns participant messages into reviewable drafts using Claude on Bedrock. A coordinator reviews the draft before the private engine applies a change, checking roles, capacity, routes and custody. Both paths share durable storage. Recovery replaces only the failed assignment, preserves unaffected pickups, and closes with a recipient receipt.”

## Verification and limitations

The local HTML was loaded in headless Chrome at 1440×900, 1600×1000, 1920×1080 and 2048×1320. Each view fit without document overflow. Checks found zero remote requests, page errors, text overlaps, labels outside the canvas, component text outside its box, or sampled route crossings through text. The exported PNG was rendered from the SVG at 2240×1460 and visually inspected in full; routes, arrowheads, labels, review ordering and boundary placement were reviewed. `verification.json` contains the machine measurements; visual judgment is separate from those checks.

This is custom local SVG authoring, permitted by the task prompt, informed by Archify's small-node-count, boundary and self-contained export guidance. It was not compiled or validated by Archify; no Archify showcase acceptance is claimed. `build_diagram.py` reproduces SVG/HTML, and `verify_diagram.cjs` exports the PNG and runs the browser checks using the local bundled Playwright and Chrome.

No material topology discrepancy was found between the supplied prompt and inspected implementation. Deployment-specific settings such as the disabled core model path and running tunnel are supported by recorded deployment evidence, not a fresh remote inspection. Two local files differ from the source hashes in the recorded dashboard verification: `demo/demo_ai.py` and `deploy/compose.demo.yaml`. Their inspected contents still implement the topology shown, but the recorded `source_matches_running_release` flag cannot establish that these current local bytes are deployed. No remote state was changed or queried to resolve this. The comparison is recorded in `source-verification.json`.

## Attribution and license

Visual/workflow reference: [Archify by tt-a1i](https://github.com/tt-a1i/archify), README, `archify/SKILL.md`, and HTML template inspected September 15, 2026. The deliverable uses original diagram geometry and a minimal original HTML wrapper; it does not bundle Archify's renderer, scripts, fonts or icons. The upstream MIT notice is retained in [ARCHIFY-LICENSE.txt](ARCHIFY-LICENSE.txt) for provenance. No remote fonts, scripts, images or styles are required. Original artifact code is provided under the repository's MIT license. Third-party trademarks remain the property of their owners.


### Visual polish revision

The overview follows the spacious cards, small icon tiles and restrained hierarchy of the user-supplied [Argus architecture reference](https://www.tryargus.xyz/architecture), inspected visually. No Argus artwork or code was copied. “Implemented public demo” was removed from the graphic. Detailed cost, session and infrastructure explanations remain in this README. The diagram keeps the reviewed-command branch and shared-storage relationships.

- Relay's logo recreates the existing `demo/index.html` / `demo/style.css` wordmark and italic-r badge as editable vector/text elements.
- The AWS mark uses the official paths served by [AWS](https://aws.amazon.com/), retrieved September 15, 2026, retained in `assets/aws-logo.svg` and embedded inline in the master. It identifies the cloud provider; it does not indicate endorsement. AWS and its logo are Amazon trademarks and are excluded from the original-code MIT grant.
- Component icons are original, generic line drawings, not official AWS service icons. Green denotes application/state components; blue denotes interpretation; amber denotes human review.
- The small recovery sequence is illustrative: recovery is conditional on a failed assignment, not mandatory for every rescue. Donor-return custody commands remain separate and are documented above.
