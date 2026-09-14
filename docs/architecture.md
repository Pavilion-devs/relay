# Relay architecture — current public implementation

The current topology is verified by `docs/demo-dashboard-verification.json` and `docs/public-runtime.md`. Earlier design targets must not be presented as deployed services.

Browser HTML/CSS/JavaScript → Cloudflare HTTPS tunnel → public FastAPI companion on EC2. The companion separates two paths:

1. **Interpretation:** scoped participant message → Strands Agent / BedrockModel → metered Claude Sonnet 4.6 call → typed validation and source-span normalization → persisted draft or clarification. The model has no execution tools. The user reviews the proposed facts separately.
2. **Operations:** reviewed or structured command → private authenticated FastAPI engine → revision, feasibility and custody checks → atomic SQLite state/receipt/reservation transaction.

Both the scoped AI persistence path and the engine use SQLite/WAL on encrypted EBS. The durable worker handles scheduled follow-ups through the local inbox. Maintenance backs the database up to encrypted/versioned S3. Docker Compose runs these services on EC2. IAM scopes service access; CloudFormation provisions infrastructure; Systems Manager provides administration; EventBridge Scheduler and a host timer enforce the temporary expiry.

The public browser receives a session cookie, not the internal participant grants. Visitors operate isolated synthetic networks. The engine API's independent model endpoint remains disabled; live public interpretation goes through the companion's cost guard.

Cognito identity and SES email-response flows were exercised separately. Neither is active in the public demo. AgentCore, DynamoDB, SNS/SQS delivery and Amazon Location are not part of this deployed path. People, travel inputs and food movement are simulated.

The diagram task is specified in `docs/submission/architecture-agent-prompt.md`; finished visual artifacts belong under `docs/architecture/`. The explanatory diagram is not yet verified or attached at the time of this document's update.
