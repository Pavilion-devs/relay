# Relay

**Recover a broken food-rescue commitment without discarding the commitments that still work.**

Relay is an operations workspace for food-rescue coordinators. It turns participant messages into reviewable updates, computes feasible assignments, collects revision-specific confirmations and tracks custody through partial replacement and recipient receipt.

The working public dashboard uses **Strands Agents SDK, Claude Sonnet 4.6 on Amazon Bedrock, FastAPI and a transactional recovery engine**. It runs on Amazon EC2 with encrypted EBS storage and S3 backups. Participants, travel inputs and food movement in the demonstration are synthetic. Model invocations and engine transitions are real.

[Temporary live demo](https://instrument-generations-reduce-experiments.trycloudflare.com) · [Demo instructions](docs/demo-presenter-notes.md) · [Architecture diagram](docs/architecture/relay-architecture.png) · [Architecture implementation](docs/architecture.md) · [Verification](docs/demo-dashboard-verification.json) · [MIT license](LICENSE)

The current temporary deployment is scheduled to stop at **2026-10-09 01:00 UTC**. This is not a durable judging-access commitment. See the local test build below and [deployment status](docs/public-runtime.md). The user-approved extension keeps the host running through judging; the temporary tunnel and finite AI allowance remain availability limits.

## What to try

1. In **Messages & AI**, select Amara and enter “I am available. I can carry 20 crates and must finish by minute 85.” Interpret, inspect the source-backed draft, then explicitly apply it. Twenty crates at the scenario's eight kilograms per crate normalize to 160 kg.
2. In **Commitments**, generate a plan. Confirm separately as each assigned participant, then approve dispatch as coordinator. The starting rescue allocates Tunde 192 kg and Amara 128 kg.
3. Record Tunde's pickup. In **Recovery**, propose Ada as the replacement after a synthetic breakdown. Record the failed driver's stop and the affected participants' confirmations.
4. Attempt to commit before donor-return evidence. Relay rejects the swap with `CUSTODY_UNRESOLVED`.
5. Record Amara's pickup, then donor evidence for Tunde's returned load. Commit the replacement. Amara retains her route, 128 kg assignment and pickup.
6. Record Ada's pickup and Harbour's 320 kg receipt. The rescue completes and reservations are released. Inspect **Activity log** for decisions and rejected commands.

The dashboard also supports editable participant facts, recalculation, cancellation acknowledgments, donor returns and exact receipt quantities. Each visitor gets an isolated synthetic network. Choosing a participant in the sandbox simulates that role; it is not proof of a real person's identity.

## Run the current dashboard locally

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). No Node build is needed for the current dashboard.

From this repository, start the authenticated engine:

```sh
uv sync --locked
RELAY_DB=.data/judging.sqlite3 uv run uvicorn relay_core.api:app --host 127.0.0.1 --port 8765
```

In a second terminal from the same directory, start the companion against the **same database**:

```sh
RELAY_DB=.data/judging.sqlite3 \
RELAY_DEMO_API=http://127.0.0.1:8765 \
RELAY_DEMO_SECURE_COOKIE=0 \
uv run python demo/server.py
```

Open **http://127.0.0.1:8780**. The companion provisions session-scoped synthetic roles automatically; do not expose grant bundles in the browser. Structured operations work without AWS credentials. These commands are for local testing; do not publicly expose the private engine API or this HTTP development setup.

For local durable follow-up processing, an optional third terminal runs:

```sh
uv run python -m relay_core.worker --db .data/judging.sqlite3
```

### Enable real local AI interpretation

Configure an AWS profile with access to `us.anthropic.claude-sonnet-4-6` and its supported foundation-model destinations. The caller needs the relevant `bedrock:InvokeModel` and `bedrock:CountTokens` permissions. Use your normal AWS sign-in flow; never commit credentials.

Restart only the companion with the additional environment settings:

```sh
AWS_PROFILE=relay AWS_REGION=us-east-1 RELAY_DEMO_AI=1 \
RELAY_DB=.data/judging.sqlite3 \
RELAY_DEMO_API=http://127.0.0.1:8765 \
RELAY_DEMO_SECURE_COOKIE=0 \
uv run python demo/server.py
```

This makes billable Bedrock requests in the configured AWS account. `demo/demo_ai.py` reserves a conservative maximum cost before each invocation, limits output and prevents automatic retries. Its persistent $1.50 allocation is per database; creating another database creates another allowance and is not an account-wide billing cap. The hosted instance uses its IAM role, not a developer's access keys. The core API's separate model endpoint remains disabled in the public deployment.

No model is substituted when AI is disabled or unavailable. The dashboard reports the limitation and leaves structured controls usable.

## How the technologies fit

- **Strands Agents SDK:** `Agent`, `BedrockModel`, a before-model-call hook, request metrics and a single extraction attempt. The current agent has no execution tools. It interprets messages; it does not approve or dispatch.
- **Amazon Bedrock:** live Claude Sonnet 4.6 inference. A CountTokens call precedes the persistent reservation and Converse request.
- **Typed validation and normalization:** Pydantic validates model output. Python verifies source spans, units, time boundaries and supported conditions before creating a draft or clarification.
- **FastAPI:** public session-bound companion and private role-authorized engine API. Reviewed commands are distinct from interpretation.
- **SQLite/WAL:** atomic authorization, state transitions, command receipts, shared reservations and durable work queues. Storage resides on encrypted EBS in the hosted demo.
- **Recovery engine:** revision checking, required confirmations, custody evidence and preservation of unchanged assignments.
- **AWS hosting and operations:** EC2, IAM, Systems Manager, CloudFormation, S3 backups and an EventBridge Scheduler stop deadline. Public HTTPS currently uses a temporary Cloudflare Tunnel.

Cognito identity and SES email-response paths were exercised in separate integration rehearsals. They are not active in the public sandbox. AgentCore, DynamoDB and Amazon Location are not deployed here.

## Verification

```sh
uv run pytest -q
uv run ruff check relay_core tests scripts demo
node --check demo/app.js  # optional JavaScript syntax check when Node is installed
```

**277 automated tests pass** at the dashboard milestone. They cover domain transitions, custody, stale revisions, duplicate/concurrent requests, permissions, persistence, SDK protocol behavior and metering. Scripted model tests are not live language-accuracy evidence.

The full public browser workflow was exercised from a real AI draft through review, changed facts, confirmations, custody rejection, partial replacement and a 320 kg receipt. Separate live requests produced a time-limit draft and a missing-units clarification. See [public AI checks](docs/demo-public-ai-01.json) and [dashboard verification](docs/demo-dashboard-verification.json). Historical evaluation reports retain their original results and known failures; they are not combined into a misleading accuracy score.

## Scope and limitations

- The solver is deliberately bounded: at most three available drivers and four recipients. Oversized problems escalate instead of being declared impossible.
- Travel matrices and handling inputs are supplied fixtures. This is not live navigation, a food-safety certification or real rescue-impact measurement.
- Post-receipt reconciliation, volume constraints and return-route planning are not implemented.
- Temporary browser sessions and finite public model quotas are demonstration controls, not a production identity or availability guarantee.
- A real-practitioner study and a matched comparison with other products have not been completed.

## Repository map

| Path | Purpose |
| --- | --- |
| `demo/` | Current functional dashboard, isolated public API, metered live AI |
| `relay_core/agent.py`, `relay_core/extraction.py` | Strands interpretation, typed source validation and normalization |
| `relay_core/engine.py`, `relay_core/planner.py`, `relay_core/replacement.py` | Commitments, feasible assignments and partial recovery |
| `relay_core/store.py`, `relay_core/execution.py`, `relay_core/worker.py` | Transactions, shared reservations and durable work |
| `relay_core/access.py`, `relay_core/identity.py` | Scoped grants and separate identity integration |
| `relay_core/email_*.py` | Separate opt-in email and response integration |
| `deploy/`, `infra/` | Container packaging and reviewed AWS infrastructure |
| `tests/`, `evaluation/` | Automated checks and synthetic language cases |
| `docs/` | Dated evidence and implementation notes; older milestones are historical |
| `web/` (local history only) | Earlier React/Vinext prototype, omitted from this active-dashboard release |

## License and development provenance

Relay is MIT licensed. It uses open-source libraries including Strands Agents SDK, boto3/botocore, FastAPI, Pydantic and SQLite; their respective licenses apply. The earlier `web/` prototype used a Sites-generated Vinext starter and associated UI components; that prototype is not the active judging interface. AI coding assistance was used to develop Relay. The synthetic scenarios and tests are development evidence, not practitioner-supplied data.
