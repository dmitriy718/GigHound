# Review Round 2 — Pass 2b: Coupling & Domino-Effect Audit (frontend↔backend contract + infra)

Date: 2026-08-31 · Method: read every router, `schemas.py`, `models.py`, the whole frontend API layer (`api/client.ts` — all 70+ request functions), `types.ts`, WS hook, all views, both compose files, both Dockerfiles, worker config/client/runner, and `docs/api-contract.md`. Every frontend request traced to a live route — all methods/paths exist; auth header attached globally (`client.ts:68-76`). Below are the mismatches/drifts/domino risks.

## Findings

**1. [CRITICAL] Production SPA bundle hardcodes `http://localhost:8000` — default Docker deployment broken on any real host**
- `frontend/src/api/client.ts:35-36` — `API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'`. All-in-one image builds frontend in `backend/Dockerfile:7-12` with **no `ARG VITE_API_URL`** → fallback baked in. Only the orphan `frontend/Dockerfile:11` has the ARG (also defaults localhost).
- Domino: `docker compose up` on a server → backend serves SPA (`main.py:82-84`) but every browser calls the *visitor's* localhost:8000. Total API failure on first load; mixed-content blocked under TLS. Same for WS (`wsUrl()` derives from API_URL, client.ts:38-40).
- Fix: default `API_URL` to `''` (same-origin — backend already serves SPA + API + WS on one host), or add the ARG.

**2. [HIGH] Worker dies on any backend outage; Compose never restarts it**
- `worker/client.py:54-55` re-raises `httpx.TransportError` after 3 retries; `runner.py:65` catches only `BackendError | ClaimConflictError` → escapes `main()` (runner.py:91-99). `docker-compose.worker.yml`: no `restart:`, `depends_on` without condition; `backend` service has no healthcheck (`docker-compose.yml:36-49`).
- Domino: any backend restart/deploy or >~7s blip kills the worker permanently. Stealth tasks pile up; nobody alerted.
- Fix: catch `TransportError` in `poll_once`; `restart: unless-stopped`.

**3. [HIGH] Stealth-task completion flips proposal status with no WS event — queue UI silently stale**
- `gigs.py:349-367` `_apply_submission_outcome` never calls `alerts.broadcast` (ingest/orchestrator/outcome_sync all do). `ProposalQueue` reloads only on `proposal_queued`/`generation_failed`/`client_replied` (`ProposalQueue.tsx:152-170`), zero polling (:142).
- Domino: worker submits → DB `submitted`, UI shows `queued_for_browser` indefinitely → reviewer clicks Submit again → confusing 409.
- Fix: broadcast `proposal_status_changed` from `_apply_submission_outcome`; handle in queue (or 30–60s fallback poll).

**4. [HIGH] WS events fire-and-forget; reconnects never catch up**
- `ws_manager.py:47-60` Redis pub/sub, no persistence; messages published while tenant has no live connection are dropped. `useAlertsSocket.ts:107-116` reconnects but emits no "reconnected" signal; every view loads once on mount; no polling fallback anywhere.
- Domino: laptop sleep/network flap during a Celery burst → `job_ingested`/`proposal_queued`/`client_replied` lost forever — feed, queue, reply badges silently wrong all session.
- Fix: refetch on socket open; or persistent/replayable events.

**5. [MEDIUM] `humanized_text`: frontend types `string | null`, backend always sends `""` — editor fallback never fires**
- `frontend/src/types.ts:262` vs `backend/app/schemas.py:323` (`humanized_text: str = ""`; model default `""`, `models.py:280`). Editors use `?? item.proposal_text` (`ProposalQueue.tsx:80`, `BuyerRequestInbox.tsx:23`) — `""` is not nullish → editor opens **blank** while `proposal_text` has content. Approve POSTs `proposal_text: ""`, accepted and stored (`proposals.py:102-104`).
- Domino: reviewer approves a blank-looking draft → proposal text wiped → empty proposal can go out under human-approval audit trail.
- Fix: `||` instead of `??` at both sites, or schema emits `None` when empty. Align types.

**6. [MEDIUM] Documented WS `digest` message type never sent**
- `docs/api-contract.md:111` documents it; frontend handles it (`useAlertsSocket.ts:100-101`); backend never broadcasts it (digest.py only emails).
- Fix: delete from doc or emit from `digest_tick`.

**7. [MEDIUM] api-contract.md omits live endpoints (drift by omission)**
- Missing: `POST /api/auth/login`, `/register`, `GET /me`, `POST /logout` (`routers/auth.py:43,58,71,76`); the entire `/api/adapters/*` router; `POST /api/proposals/templates/generate` (proposals.py:322), `POST /api/profiles/templates/generate` (profiles.py:26); `POST /api/gigs/stealth-tasks/{id}/claim` (gigs.py:297); WS `?token=` JWT + 4401 close (`routers/alerts.py:70-81`).
- Fix: add Auth, Adapters, WS-handshake sections.

**8. [MEDIUM] Approved proposals on fiverr/linkedin/indeed/peopleperhour/guru are a permanent dead end**
- `proposals.py:467-472` 501 promises pickup that never happens (no emitters for `SUBMIT_FIVERR_OFFER`/`SUBMIT_PROPOSAL`; only Upwork dispatched, proposals.py:456). Frontend hides Submit for those platforms (`ProposalQueue.tsx:58-64`); BuyerRequestInbox offers only approve/reject.
- Domino: approved offers sit forever; funnel `approved` count overstates; 501 message lies.
- Fix: implement dispatch or add "mark as manually submitted" + correct the 501 text.

**9. [MEDIUM] Credential enrollment accepts linkedin/indeed; worker can never serve them**
- `credentials.py:37` vs `worker/config.py:12` (`validate()` rejects linkedin/indeed, config.py:57-61).
- Domino: user enrolls LinkedIn; UI reports "connected" (`OnboardingChecklist.tsx:49-50`); tasks pend forever.
- Fix: align the lists.

**10. [MEDIUM] WS token in URL + infinite reconnect on 4401 with dead token**
- Token as `?token=` (`useAlertsSocket.ts:47`) → uvicorn/proxy access logs. Server closes 4401 before accept (`alerts.py:79-81`); `onclose` (:107-116) doesn't inspect the code, retries forever. JWT TTL 12h (`auth.py:25`); idle tab past expiry never fires the 401→logout path (`client.ts:80-83`).
- Fix: `ev.code === 4401` → unauthorized handler; consider short-lived WS ticket.

**11. [MEDIUM] Split-tier deployment can't work out of the box**
- `frontend/vite.config.ts` no `server.proxy`; `frontend/nginx.conf` no `/api`/`/ws` proxy; `frontend/Dockerfile:11` bakes localhost default; Compose never sets `CORS_ORIGINS` (backend default allows only :5173, `config.py:12`).
- Fix: proxy in nginx.conf + empty `VITE_API_URL` default, or document required env pair.

**12. [LOW] Type drift samples**
- `GigMetric.week`: frontend `string` (types.ts:470) vs schema `Optional[str] = None` (schemas.py:449) — latent (record_metrics always fills it).
- Frontend accepts nulls backend never sends (`bid_rationale`, `analysis`, `Gig.external_id`, `Gig.created_at` read-but-undeclared) — masks drift.
- `ProposalQueueOut` `Optional[...]` without defaults = required-nullable; brittle.
- Systemic: `client.ts:95` `(await res.json()) as T` — zero runtime validation; shape change compiles fine, explodes in components.

**13. [LOW] JobFeed prepends WS jobs ignoring filters; no error boundary**
- `JobFeed.tsx:50-54` inserts regardless of active status/platform filters. `App.tsx` has no error boundary; one bad render throw white-screens the app.

**14. [LOW] Freelancer OAuth default redirect targets a route the SPA doesn't handle**
- `credentials.py:41` default `http://localhost:5173/oauth/freelancer/callback`; SPA has no router; flow only works via manual code paste (`Accounts.tsx:163-189`).
- Fix: handle callback path in SPA; default redirect to serving origin.

**15. [LOW] Infra nits**
- `frontend/Dockerfile` + `nginx.conf` unreferenced by any compose file — dead-but-inviting infra.
- `CACHE_TTL_SECONDS` read (`config.py:13`), set nowhere.
- Worker: no healthcheck/restart/depends-on-condition.
- `.env.example` complete for backend vars (verified against every `os.getenv`); `LINKEDIN_PROVIDER` read (discovery.py:107, adapters.py:220) but documented nowhere.

## Verified-OK contract points (sample)
- Every frontend request path/method exists and envelope shapes match (`{jobs,total}`, `{items,total}`, `{archived,skipped}`, `{queued_tasks}`, credential status, funnel/trend incl. nullable `win_rate`).
- 422 gig-template envelope `{detail:{validation:[...]}}` matches `ApiError.detail` handling.
- Worker↔backend protocol matches both ends (poll/claim/complete/stealth-session/buyer-requests/metrics/competitors/proposal-status).
- WS ping: client 30s, server tolerates. 12h JWT assumed both sides. 401 → Login without loops.

## Top 10 contract points most likely to break in future refactors
1. `VITE_API_URL` bake-time vs serve-time coupling (finding 1).
2. WS event-type string set hand-mirrored across hook + types + 5 broadcast sites.
3. Fire-and-forget WS with no catch-up (3, 4).
4. `ProposalQueueItem` null-vs-empty-string semantics (5).
5. The `as T` deserialization boundary (client.ts:95).
6. Worker retry/crash contract + Compose restart topology (2).
7. Dual-auth (`get_worker_or_user`) tenancy convention.
8. Platform enum across 4 codebases — already drifted (9).
9. Proposal status machine — transitions and UI filters already disagree on terminality (8).
10. `docs/api-contract.md` as unenforced artifact (7).
