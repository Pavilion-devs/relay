# Public runtime

Verified September 14, 2026 UTC. The dashboard runs on one Amazon EC2 Linux host. Docker Compose runs the public FastAPI companion, private engine API, worker and maintenance services. The data store is SQLite/WAL on encrypted EBS. Backups go to encrypted, versioned S3. CloudFormation, an IAM instance role and Systems Manager manage infrastructure and access.

Public HTTPS is provided by a Cloudflare Quick Tunnel. The core engine API is not publicly routed. Every visitor receives an isolated synthetic rescue session; the companion keeps scoped internal role credentials out of the browser.

Live interpretation uses Strands Agent with BedrockModel and Claude Sonnet 4.6. Source-span extraction precedes deterministic normalization and human review. The companion reserves a conservative cost before the single inference attempt. Public inference has a finite shared allocation, with four requests per rescue. Failed provider outcomes retain reservations. No public action sends email.

The present URL is temporary and the host is scheduled to stop at **2026-10-09 01:00 UTC**. The user approved extending this host through judging. A durable public origin/test-build fallback remains important. Do not infer permanent availability from the current URL. Local run instructions are in the repository README.

The live path was tested from interpretation to complete receipt, including an unresolved-custody rejection and preservation of an unchanged pickup. Source hashes and checks are in `demo-dashboard-verification.json`; separate live interpretation checks are in `demo-public-ai-01.json`.

Cognito and SES were tested in separate integration rehearsals; they are outside the public demonstration. AgentCore, DynamoDB, SNS/SQS delivery and Amazon Location are not active here. All people, travel and food movement are simulated. No production or field-impact claim is made.
