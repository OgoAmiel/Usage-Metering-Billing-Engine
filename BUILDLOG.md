# Build log

- Authentication implementation: Codex added tenant bearer keys stored as hashes, admin-only provisioning/rotation and job access, and ownership checks before metering replay or Checkout calls. Existing tests now authenticate with real test keys; dedicated isolation tests exercise authorization without bypassing dependencies. This entry documents AI work, not a claim of manual user review.

- Codex helped scaffold the FastAPI service, database schema, and tests.
- I reviewed the idempotency design: uniqueness is enforced in SQLite and the check, quota decision, event insert, and stored response run in one `BEGIN IMMEDIATE` transaction.
- I will test the Stripe test-mode configuration with my own keys and document any changes here.
