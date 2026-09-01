# GigHound stealth-browser worker

Playwright (Chromium) worker that executes the backend's `StealthTask` queue:
scraping (gig metrics, competitors, buyer requests/Briefs), gig draft creation,
and gated submission flows. See `evolution/architecture-decisions.md` (AD-4)
for the design.

## How it works

```
poll GET /api/gigs/stealth-tasks?platform=&status=pending   (worker token, all tenants)
  → POST /api/gigs/stealth-tasks/{id}/claim                 (atomic; 409 = another worker won)
  → execute handler (worker/handlers/)
  → POST /api/gigs/stealth-tasks/{id}/complete              (success | failure)
```

- Scraped data goes to the backend's result endpoints
  (`/api/gigs/buyer-requests/process`, `/api/gigs/metrics`, `/api/gigs/competitors`).
- CAPTCHA/challenge detection → `complete(success=false, result={captcha: true})`.
  Three failures within an hour trip the server-side circuit breaker for that
  platform and a human is alerted in the UI.
- Unhandled handler exceptions are reported as failed tasks, tasks already
  finalized server-side are skipped, and backend outages are logged and
  retried on the next sweep — the loop survives all of these; the container
  restart policy (`restart: unless-stopped`) covers truly fatal errors.

## Setup

```bash
python3 -m venv worker/.venv
worker/.venv/bin/pip install -r worker/requirements-dev.txt   # runtime + test deps
worker/.venv/bin/python -m playwright install chromium   # add --with-deps on a fresh Linux
```

(`requirements.txt` alone is the runtime set the production image installs.)

Python 3.12 is the supported runtime (the Dockerfile uses it). On Python 3.14
use playwright ≥ 1.49 — 1.45 pins a greenlet that doesn't build there.

## Environment

| Var | Default | Purpose |
|---|---|---|
| `GIGHOUND_API_URL` | `http://localhost:8000` | backend base URL |
| `GIGHOUND_WORKER_TOKEN` | — (required) | must match the backend's token |
| `WORKER_ID` | `hostname-pid` | identity recorded on claimed tasks |
| `WORKER_PLATFORMS` | all supported | comma list: `fiverr,upwork,peopleperhour,guru` |
| `WORKER_PROXY_{PLATFORM}` | — | per-platform proxy, e.g. `WORKER_PROXY_UPWORK=http://user:pass@host:8080` |
| `WORKER_CONTEXT_IDLE_SEC` | `1800` | close browser contexts idle this long (rebuilt on demand) |
| `WORKER_HEADLESS` | `true` | set `false` to watch the browser |
| `WORKER_SESSION_DIR` | `worker/.sessions` | persistent browser profiles (gitignored) |
| `WORKER_POLL_INTERVAL_SEC` / `WORKER_POLL_JITTER_SEC` | `45` / `15` | poll pacing (≈30–60s) |
| `WORKER_TIMEZONE` / `WORKER_LOCALE` | `America/New_York` / `en-US` | browser fingerprint defaults (per-account `timezone`/`locale` account settings override) |
| `WORKER_ACTIVE_HOURS` | `8-23` | circadian window (hours in `WORKER_TIMEZONE`); scrape/fetch tasks stay queued outside it, submit tasks always run. Empty/`off` disables |
| `WORKER_TASK_TIMEOUT_SEC` | `600` | per-task wall-clock budget; a task that exceeds it is failed with `{"error": "task timeout"}` (enforced at pacing checkpoints — the Playwright sync API is thread-affine, so a hard cancel isn't possible) |
| `WORKER_ALLOW_SUBMIT` | unset (off) | final-submit gate — see safety model |
| `WORKER_ALLOW_SUBMIT_{PLATFORM}` | falls back to `WORKER_ALLOW_SUBMIT` | per-platform override of the final-submit gate, e.g. `WORKER_ALLOW_SUBMIT_UPWORK=1` opens submit for Upwork only |

Run: `worker/.venv/bin/python -m worker` (or `--once` for a single sweep).

### Per-account proxies

A platform account can pin its own proxy via `settings.proxy_url` (editable on
the Accounts page, e.g. `http://user:pass@host:8080`); the stealth-session
payload carries it and the worker uses it for that tenant's context, falling
back to the platform-level `WORKER_PROXY_*` when unset. The proxy is sticky
per account — sharing one datacenter IP across tenants invites account
linkage — and its geo should match the account's profile location. Proxy URLs
must include an explicit port; a port-less URL fails fast with a config error.

## Session management (log in once per platform)

The worker keeps one persistent Chromium profile per `(platform, user_id)` under
`WORKER_SESSION_DIR`. There are two ways to seed a session:

1. **Via the UI (preferred):** in the Accounts view, enroll a
   `storage_state_json` credential for the platform account
   (`POST /api/accounts/{id}/credentials` — export the storage_state from an
   authenticated browser session). At context launch the worker fetches it
   from `GET /api/gigs/stealth-session` and seeds cookies + localStorage
   directly — no CLI step needed. Raw `username`+`password` enrollment also
   works, but password-based login is a fallback-only, challenge-prone path.
2. **Via the CLI (fallback):** when nothing is enrolled (or the backend is
   unreachable), the worker falls back to the local persistent profile, which
   a human seeds once — this is the sanctioned human moment for 2FA/CAPTCHA:

   ```bash
   worker/.venv/bin/python -m worker.login --platform upwork --user-id 1
   ```

   A headed browser opens on the platform's login page. Log in manually,
   press Enter in the terminal, and the session is saved. Repeat per
   platform/user.

If a session expires, tasks will start failing with `captcha: true`/login
redirects — re-enroll via the UI or re-run the login utility.

**One worker per `worker_sessions` volume.** Never scale the worker by
pointing two replicas at the same session volume: two Chromiums on one
`user_data_dir` corrupt the profile lock and put one account in two browsers
at once (a linkage/ban risk). Run one worker per volume (or per-worker named
volumes). At context launch the worker logs a loud warning when the profile's
Chromium `SingletonLock` is held by a live process — treat that warning as a
misconfiguration, not noise.

## Safety model (HITL boundary)

- The worker only executes tasks the **backend** creates, and submission tasks
  are only created from **human-approved** review-queue items. There is no path
  for the worker to invent work.
- Platform kill switches are server-side: the backend doesn't hand out pending
  tasks for disabled platforms (tasks are recorded as `skipped_circuit_open`),
  so the worker honors them implicitly — there is nothing to configure here.
- Per-platform caps (Fiverr 10 offers/day, 1 gig draft/hour, circuit breaker)
  are enforced server-side before tasks are created.
- `create_gig_draft` **never publishes** — it clicks "Save as Draft" only. The
  publish button selector exists in `worker/platforms.py` purely as a
  documented tripwire (`publish_button_do_not_click`).
- `submit_proposal` (PeoplePerHour/Guru) and `submit_fiverr_offer` are
  **manual-assist**: they fill the form with human-like typing, take a
  screenshot, and stop. The final submit click happens only when the operator
  opens the gate for that task's platform (`WORKER_ALLOW_SUBMIT_<PLATFORM>=1`,
  falling back to the global `WORKER_ALLOW_SUBMIT=1`); the task result always
  records which happened.
- `submit_upwork_proposal` does click submit — that click *is* the approved
  action (agency BM flow for an approved queue item). Any challenge mid-flow
  escalates to a human instead of retrying.
- `scrape_proposal_status` is **read-only**: it loads the platform's
  proposals/inbox listing (Upwork proposals, Fiverr seller inbox incl. brief
  responses, PeoplePerHour WorkStream, Guru quotes — best-effort selectors in
  `worker/platforms.py`), extracts per-proposal status + unread-reply flags,
  and posts them to `POST /api/gigs/proposal-status`. It never clicks, types,
  or submits — outcome/reply sync carries no HITL risk.

## Selector maintenance

All site-specific selectors, URLs, and CAPTCHA markers live in **one file:
`worker/platforms.py`**, as per-platform config dicts. When a platform's UI
changes, edit that dict — handler code is generic (JSON-LD first, then
semantic attributes, then structure selectors). Extraction helpers are in
`worker/handlers/base.py`.

Task kinds (canonical names in `backend/app/stealth.py`; the worker also maps
legacy type strings): `fetch_buyer_requests`, `scrape_gig_metrics`,
`scrape_competitors`, `create_gig_draft`, `submit_upwork_proposal`,
`submit_fiverr_offer`, `submit_proposal`, `scrape_proposal_status`.

## Tests

```bash
worker/.venv/bin/python -m pytest worker/tests -q
```

Runs without a browser (Playwright is faked/mocked). Covers client auth/retry,
claim-before-execute ordering, CAPTCHA escalation result shape, and
typing-plan consumption.

## Docker

```bash
docker compose -f docker-compose.yml -f docker-compose.worker.yml up worker
```

The override adds a `worker` service (python:3.12 + Chromium, running as a
non-root `gighound` user with runtime-only deps) that depends on the
`backend` service — note the base `docker-compose.yml` currently has the
backend service commented out; uncomment it (or point `GIGHOUND_API_URL` at a
host backend) before bringing the worker up. The build context is `./worker`;
`worker/.dockerignore` keeps local `.venv/`, `__pycache__/`, `.pytest_cache/`,
and `.sessions/` out of the image.
