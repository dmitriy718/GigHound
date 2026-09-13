# GigHound deployment and recovery

The supported release workflow is discovery → source-grounded draft → review of a specific revision → an authorized submission channel → confirmed result or explicit reconciliation. A team draft approval permits export; the submitting account owner must separately review the dispatched proposal. User-recorded receipts and experiments are observations, not proven incremental revenue or causal lift.

## Capabilities and deployment gates

| Channel | Implemented contract | Required before live use |
|---|---|---|
| Freelancer | API discovery, reviewed bid, bid/reply polling, OAuth/token enrollment | Provider application approval, correct bidder ID, account-bound credentials and redirect URI; synthetic tests do not establish live permissions |
| Upwork | API discovery where permitted; local agency roster; reviewed browser task | Provider permission/scopes, enrolled browser session, actual authority for the selected member; a local roster entry is not a platform invitation or verified membership |
| Fiverr | Buyer-request processing, reviewed offer task, gig draft preparation and metrics | Confirm current account capabilities and permitted automation; selectors require controlled verification; draft creation does not publish |
| Guru / PeoplePerHour | Drafting and manual assistance | User submits on the original platform; automatic final-click overrides require separate provider authorization and verification |
| LinkedIn | Normalized discovery through the configured source; drafting and manual submission tracking | Authorized source credentials and source-specific limits; annual salary is not an hourly rate |
| Indeed | Manual job import, drafting and manual submission tracking only | No discovery or account connector is implemented. Historical records remain readable; importing a job does not authorize automated submission. |
| Follow-ups / conversation inbox | Draft, import, next-action tracking | Manual sending; no new-bid endpoint is used to send a follow-up |
| Workbench / team workspace | Evidence, capacity brief, scope pricing, accepted team reviews, client notes, observational experiments and attributed receipts | User verification of supplied evidence; account-owner approval before dispatch; reports reject supported-size overflows instead of silently reporting partial totals |

The connection doctor checks local enrollment, generation exhaustion, uncertain submissions and recent heartbeats from workers that have claimed this tenant's work. It does not certify a live provider connection. `/api/health` is process liveness; `/api/ready` also checks the database/schema and broker. Heartbeats expire after three minutes. Neither endpoint establishes external provider health.

## Before migration or rollout

1. Preserve the current image/commit and configuration. Keep `.env` and secret files owner-only (`0600`). Never paste a vault key, token or backup passphrase into an issue or an audit log.
2. Stop scheduling new external writes and quiesce the worker pool. Inventory proposals and tasks. Run `DATABASE_URL=... backend/.venv/bin/python scripts/deployment_preflight.py` using a securely injected URL. The report is read-only. Reconcile duplicate live proposals against platform evidence; never delete a possibly submitted proposal just to satisfy an index.
3. Back up the database together with its matching `GIGHOUND_VAULT_KEY` using the commands below. Prove restoration and credential decryption in an empty disposable database before migrating production.
4. Apply migrations from `backend/` with `.venv/bin/alembic upgrade head`, then `.venv/bin/alembic check`. Legacy approvals have no material snapshot: return them to review and approve the current version. Review old unbound tasks before releasing the worker pool. Historical missing submission dates and metric values remain unknown.
5. Configure `GIGHOUND_WORKER_CREDENTIALS` on the backend as a JSON object mapping stable worker IDs to different random secrets of at least 32 characters. Each worker receives only its own secret in `GIGHOUND_WORKER_TOKEN`, and its matching `WORKER_ID`. The registry disables the legacy shared-token fallback. Rotate one registry entry to revoke one worker. Keep backend registration settings and TLS configuration explicit.
6. Give each worker an exclusive session directory/volume. Do not share Chromium profiles across workers. Use a network egress boundary that blocks private/reserved addresses and DNS rebinding. Application hostname/cookie allowlists alone do not provide that network boundary. Encrypted disks and worker-volume retirement cover copies left on an unavailable or permanently retired host.
7. Set per-platform `GIGHOUND_DAILY_SUBMIT_CAP_<PLATFORM>` for durable browser write-attempt reservations; Fiverr defaults to 10, Upwork defaults to unlimited. These count reserved attempts, including work that stops before a final click. `GIGHOUND_DAILY_CAP_<PLATFORM>` limits API adapter actions and fails closed when a configured budget cannot be checked. Adapter per-second pacing uses atomic Redis-clock reservations across API/Celery processes by default and fails closed on broker loss. Set `GIGHOUND_DISTRIBUTED_PACING=0` only for isolated unit tests or an explicitly single-process environment. Requests with more than five seconds of reserved backlog are rejected. Start with one browser worker per account and conservative provider rates.
8. Keep broker AOF and its persistent volume enabled. AOF/every-second persistence has an approximately one-second loss window; durable SQL generation intent replays eligible work. Missing external-action confirmation always requires reconciliation, not blind replay.
9. Run a canary tenant through registration, enrollment, discovery, draft review, return-to-review, and manual reconciliation. Use a fake provider or read-only checks first. Enable a live channel only with documented permission and a confirmed account-specific test. Do not infer approval from an agency subscription or from a successful login.
10. Observe generation backlog/exhaustion, pending tasks, worker heartbeats, uncertain submissions, API/broker readiness and error rates. Stop the tenant platform circuit on unexpected behavior. Manual stops are stored in SQL and survive broker loss; closing a stop does not itself retry uncertain work.

## Backup and restoration

Use PostgreSQL client tools compatible with the server version. The local proof used PostgreSQL 16 tools with PostgreSQL 16. Supply secrets through a protected environment or secret manager. Retain the backup passphrase separately from the encrypted archive.

```sh
backend/.venv/bin/python scripts/backup_restore.py backup /secure/new-backup.enc
backend/.venv/bin/python scripts/backup_restore.py verify /secure/new-backup.enc
# DATABASE_URL must now identify a newly created EMPTY restore target.
backend/.venv/bin/python scripts/backup_restore.py restore /secure/new-backup.enc \
  --confirm-db exact_restore_database_name --key-output /secure/restored-vault.key
```

Required environment: `DATABASE_URL`, `GIGHOUND_BACKUP_PASSPHRASE` (at least 20 characters), and for backup `GIGHOUND_VAULT_KEY`. Output files are created exclusively with mode `0600`. Restoration refuses a nonempty target and uses a single PostgreSQL restore transaction. Verify a synthetic encrypted credential after restoration. The helper supports custom-format dumps up to 256 MiB and buffers encryption in memory; provision sufficient memory. Larger databases require an operator-managed streaming encrypted backup and matching-key retention process. A successful decrypt alone is not a database restoration drill.

For rollback, quiesce producers and workers, reconcile externally attempted actions, and restore the pre-migration database/key pair into a new target. Point the prior image at that target after verification. Do not downgrade nullable unknown metrics into zeros. Do not replay a backup's old pending submissions against live platforms.

## Money and delivery

Native-currency bids preserve their units. Cross-currency rate-card bids require `GIGHOUND_FX_RATES_JSON` with an aware ISO `as_of` timestamp no older than 24 hours, nonempty `source`, and positive finite `usd_per_unit` rates. USD is one. Expired/missing conversion data produces a manual-pricing prompt. Quality scoring still uses legacy approximate FX heuristics; it is not a financial quote. No annual salary is treated as an hourly or project budget.

Digest delivery requires an operator-verified `GIGHOUND_VERIFIED_DIGEST_RECIPIENTS` mapping from user IDs to their registered email addresses. Global `DIGEST_TO` is ignored. Delivery attempts are reserved durably; uncertain SMTP delivery is not blindly repeated. Self-service verified-email enrollment is not implemented.

## Reproducible local verification

```sh
PYTHONPATH=backend backend/.venv/bin/pytest backend/tests -q
PYTHONPATH=. worker/.venv/bin/pytest worker/tests -q
npm --prefix frontend run build
backend/.venv/bin/python scripts/check_ui.py
```

Database concurrency tests additionally require `GIGHOUND_TEST_POSTGRES_URL`; each creates and drops its own schema. Redis tests require `GIGHOUND_TEST_REDIS_URL` pointing to a disposable database: the test fixture flushes that database. Never point these variables at the running application's database or broker. The browser journey creates an isolated temporary database, binds ports 8059/4179, and closes its servers afterward. CI runs the same journey. These tests do not use real customer accounts or send real platform messages.

## Bounded collections

Configuration, keyword-group, account, portfolio, rate-card and gig list APIs accept `limit` (1–500, default 100) and `offset` (0–100000), ordered by ID. The UI fetches bounded pages and reports an explicit error above 5,000 interactive records. It does not silently display an incomplete collection. Export larger collections through the paginated API. Large-scale latency and cost are deployment-specific; a 10,000-job/100-tenant synthetic PostgreSQL fixture proves use of the tenant/status/time access path, not unlimited scalability.

## September 13 implementation changes (not a release certification)

- The Compose browser worker now requires `WORKER_ID` explicitly. Set a stable ID matching the backend registry; the worker receives only the secret for that ID. A hostname/PID fallback in a standalone development process is not a registered production identity.
- Upwork account settings now expose `agency_id` in addition to the selected member's `on_behalf_of` UID. Newly queued tasks carry that agency identity. Review again after changing account settings. Old tasks lacking the reviewed identity are rejected rather than filled using platform defaults. The current handler supports verified native select controls; unsupported custom controls require further provider-specific work. It reads back identity, amount and exact text before clicking. These are local safeguards, not live-selector certification.
- `GIGHOUND_MONTHLY_BID_CAP_FREELANCER` defaults to 50 and now limits reserved monthly write attempts per credential principal. Zero blocks bids. Reservations occur before dispatch and remain charged after uncertain failures; this is a conservative local allowance, not a live provider quota balance. Historical tenant-wide usage is conservatively included for each principal for the current month because its original account attribution is unknown. Reconcile historical usage before changing production allowances; never silently reset it during rollout.
- Workbench → Connection doctor → Generation recovery lists unfinished generation even when no proposal row exists. Active leases and reviewed proposals cannot be replaced. An expired final attempt can be manually retried; stale worker tokens cannot complete the replacement attempt. A broker outage leaves the retry intent pending for delivery.
- Workbench record lists now have a Load more control. Evidence matching explicitly rejects a library above 1,000 entries instead of returning incomplete matches. Larger-library support remains an explicit capacity decision.

## Durable automation controls (migration e06d713ca502)

Circuit state and trial admission now live in `automation_circuits`, not Redis or process memory. Circuit resolution is part of the task-completion database transaction. Trial ownership includes the circuit revision, so an old completion cannot close a newer stop/trial cycle. A database failure blocks admission. Existing Redis circuit keys no longer grant permission.

**Rollout behavior:** upgrading an existing database pauses every existing tenant's platform scopes. This is intentional: the migration cannot prove what the old Redis state was. New users created after migration start without a recorded stop. Quiesce workers before upgrading, reconcile historical/uncertain work, then review each account and use **Accounts → Automation safety → Resume reviewed automation** for the appropriate platform. This action does not replay uncertain submissions. A deployment-wide stop is displayed separately and must be resolved by the deployment operator. The API readiness check now also requires the circuit schema.

Account deletion and credential writes serialize on the owner row. Enrollment/OAuth writes are bound to the originating account ID, and token exchange/refresh writes compare the credential version captured before the provider request. Deleting or rotating credentials while a request is in flight makes its late write fail; it cannot restore the old secret. OAuth token persistence occurs once in the adapter, avoiding a second unguarded route-level write. These controls are covered by isolated PostgreSQL races; they do not revoke tokens at an external provider or erase retired worker media.


## Account selection and review recovery (September 13 continuation)

Apply migrations through `a28f935ec724` before starting this application version. Proposal review now records a selected account in `platform_account_id`; when several enabled accounts exist, choose one in the proposal or buyer-request editor. Approval through the API accepts `platform_account_id`. Bulk approval does not guess an account. Changing account settings/credentials still invalidates material approval, and selecting a different account clears inherited bidder/member defaults. Existing approved snapshots retain their original account identity until renewed review.

Freelancer and Upwork search requests accept `account_id`. Freelancer quota and Upwork local agency roster endpoints accept the same query parameter; the Accounts editor supplies its edited account. Local rosters are separated by principal, with the historical `agency_manager` roster retained in its original key. The Accounts editor can explicitly assign an unassigned legacy roster once to the reviewed account. Browser proposal status polling and Fiverr buyer monitoring now group work by account. Seller gig creation/metrics binding and historical unbound proposals still need completion; multi-account production qualification remains open.

Each platform account now has an internal identity epoch. Deleting/recreating an account—even with the same numeric ID—does not make an old enrollment/OAuth transaction valid for the new account. OAuth flows started before the identity migration must be restarted. This fence complements credential-version checks; it does not revoke an already issued token at the external provider.

After a proposal changes elsewhere, the editor keeps unsaved work and displays the current saved version. Choose **Discard my edits and use saved version** or **Keep my edits for fresh review**, then review and approve explicitly. These actions neither send nor approve work themselves. Stale drafts remain in this browser's session storage across refresh/re-login; they do not overwrite server revisions automatically.


## Guided applications and writing preferences

New users start in Guided applications unless an explicit view URL is opened. The guide saves its active job and step to the signed-in owner's account; its startup checkbox controls future visits to the root URL. Manual navigation remains available. Advancing a step does not submit a proposal or record a hire. The embedded proposal controls retain revision, account-selection, approval, submission and outcome checks. Starting another application clears the active job and retains reusable profile data.

In Profiles → My writing voice, save style notes and up to five writing examples. Examples are shared with the configured text provider during generation; they supply style, not proof of experience. The per-job Application tone selector is available in guided drafting, Proposal Queue and Buyer Requests. Presets are informed by general writing research; none has an established individual hiring advantage. Proposal Queue can generate a fresh text preview for a pending proposal; the saved version stays unchanged until reviewed approval. Existing local edits that change while generation is in flight must be preserved.

Business Workbench → Hiring posts & service ads supports a source brief, editable draft, versioned save, copy, export and manually recorded publication state. Generation does not save or publish. The state `manually_published` is the user's record, not a provider receipt. Missing AI service leaves manual drafting available. Upwork/Fiverr/Freelancer publishing capabilities still require their separate R01 qualification.

The existing hosted generation configuration is `LLM_PROVIDER=openai`, `LLM_API_KEY`, optional `LLM_BASE_URL`, and `LLM_MODEL`. This implementation batch did not change live settings or make paid generation requests. Tenant cost budgets and live quality evaluation remain open; consult the implementation register before offering hosted subscriptions.


The supplied Compose worker identity flow was qualified locally using `astra/09132026_implementation_evidence/verify_worker_identity.py`. It builds the actual image and exercises registered authentication and the fenced task protocol against synthetic owned services. It does not certify hostile-page isolation, provider access, or production networking. Build contexts exclude environment files and worker sessions; the worker does not inherit the root environment file.
