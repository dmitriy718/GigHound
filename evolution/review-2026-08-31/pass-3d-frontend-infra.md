# Review Round 2 — Pass 3d: Frontend & Infra Deep Dive (line-level)

Date: 2026-08-31 · Scope: all 23 frontend source files (~8,200 lines: every view, component, hook, api/client.ts, types.ts, App.tsx, main.tsx, styles.css), index.html, vite.config.ts, tsconfig.json; infra: backend/Dockerfile, frontend/Dockerfile, frontend/nginx.conf, docker-compose.yml, docker-compose.worker.yml, scripts/bootstrap.sh, backend/scripts/, .env.example, .dockerignore, .gitignore, .github/workflows/ci.yml. Read every line.

## Part 1 — Prior-claim verdicts

**1. API_URL baked to localhost:8000 — CONFIRMED, worse than stated.** `client.ts:35-36` default; `backend/Dockerfile:7-12` (stage 1) accepts no `VITE_API_URL` ARG; only orphan `frontend/Dockerfile:11` does, and nothing builds it. All-in-one image serves SPA same-origin (`main.py:83`), so correct default is `''`. As shipped, any non-localhost deployment serves an app whose every API/WS call goes to the *viewer's* localhost:8000 (and would need CORS anyway — main.py:34 allows only :5173). Fix: `API_URL = import.meta.env.VITE_API_URL ?? ''`.

**2. WS reconnects forever on 4401; token in URL — CONFIRMED.** `useAlertsSocket.ts:107-116` — `ws.onclose = scheduleReconnect` unconditionally, backoff capped 30s, no close-code inspection. Backend closes auth failures 4401 (`alerts.py:80`). Scenario: 12h JWT expires mid-tab → reconnect with dead token every ≤30s indefinitely, UI shows "CLOSED". Token as `?token=` (:47, alerts.py:71) → access logs. Fix: `if (ev.code === 4401) return;`; prefer short-lived WS ticket.

**3. `??` blank-editor / approve-wipes-text — CONFIRMED, CRITICAL-adjacent.** `ProposalQueue.tsx:80`, `BuyerRequestInbox.tsx:23`. DB column `default=""`, **not nullable** (`models.py:280`); schema `humanized_text: str = ""` (`schemas.py:323`) → API returns `""` for every pre-humanization row. `"" ?? x` is `""` → editor blank while `proposal_text` holds the draft; Approve POSTs `proposal_text: ""`, written verbatim (`proposals.py:102-103`). Any migrated deployment with pending legacy rows hits this on first approve. Fix: `||` at both sites; treat `""` as NULL server-side on read.

**4. WS jobs ignore filters; no app-level error boundary — CONFIRMED.** `JobFeed.tsx:50-61` prepends any job regardless of active filters (doesn't bump `total`). `App.tsx:257-270` renders views bare; only `GigManager.tsx:912-935` has an ErrorBoundary.

**5. Stale queue after worker submission — CONFIRMED.** Every view loads once on mount; no polling, no refetch-on-reconnect. `_apply_submission_outcome` (`gigs.py:349-367`) flips `queued_for_browser → submitted/failed` with no WS broadcast → row sits stale until manual Refresh.

**6. No runtime payload validation — CONFIRMED.** `client.ts:95` `as T`. Combined with N1 below = whole-app crash vector.

**7. Manual OAuth paste; unroutable redirect — CONFIRMED.** `Accounts.tsx:163-197`; default `FREELANCER_REDIRECT_URI=http://localhost:5173/oauth/freelancer/callback` (`.env.example:66`); SPA has no router (App.tsx view-state only) → redirect renders Job Feed with `?code=` in the bar; user hand-copies. Prod SPA fallback (`main.py:72-83`) same result.

**8. Supply-chain/asset findings — all CONFIRMED.** esbuild 0.21.5 in package-lock.json (GHSA-67mh-4wv8-2f99, dev-only; no npm audit CI job); Google Fonts CDN `styles.css:5`; `node:20-slim` both Dockerfiles + `node-version: 20` ci.yml:68 (EOL 2026-04); `.env` absent from root `.dockerignore`; frontend/Dockerfile + nginx.conf referenced by no compose file.

**9. celery-worker/beat skip migrations — CONFIRMED.** Only backend image CMD runs `alembic upgrade head` (`backend/Dockerfile:37`); compose overrides command for celery services (:57,71) with `depends_on` on db/redis health only. Fresh deploy: beat dispatches against unmigrated schema. Fix: one-shot `migrate` service + `depends_on: { migrate: { condition: service_completed_successfully } }`.

## Part 2 — New findings

**N1 [high] One bad payload blanks the entire app.** No global boundary + unchecked `as T` + crash vectors: `BuyerRequestInbox.tsx:73` `items.find(...)!` (non-null assertion can miss after WS-triggered reload), `ProposalQueue.tsx:600` `item.analysis.required_skills.length` (schema drift). Scenario: backend deploys shape change → every user's screen white, no recovery UI. Fix: ErrorBoundary around `<main>`; drop the `!`.

**N2 [high] Cross-tenant WS message leak on re-login.** `useAlertsSocket.ts:29-37`: on logout (token→null) the effect early-returns — `messages` never cleared. Next user on same browser inherits previous tenant's last 50 alerts (job titles, proposal IDs, client-reply snippets). Fix: `setMessages([])` when token falsy/changes.

**N3 [high] Mid-session 401 silently destroys unsaved work.** `client.ts:80-83` clears token; App unmounts all views instantly. 12h JWT expiring while reviewer has typed edits (component state only, `ProposalQueue.tsx:96`) → edits vanish, no warning, no draft persistence. Fix: persist drafts (sessionStorage by proposal id) or defer logout until acknowledged.

**N4 [high] Job Feed has no pagination despite `total`.** `JobFeed.tsx:35` hardcodes `limit: 50`, no offset controls (unlike ProposalQueue's pager :1032-1052). Header says "{total} jobs total" but jobs past 50 unreachable. Same shape: `BuyerRequestInbox.tsx:39` fetches 200 pending then filters `request_type === 'buyer_request'` client-side — requests past 200 pending never appear.

**N5 [medium] `generation_failed` proposals unactionable zombies.** UI renders Approve/Reject only for `pending_review` (:910); backend 409s approve otherwise (proposals.py:97-98); no regenerate/retry endpoint call anywhere in client. Every LLM failure = permanent unfixable row.

**N6 [medium] Proposal version history off-by-one — original draft unrecoverable.** `proposals.py:103` overwrites `item.proposal_text`, then :118 appends the *new* text as version. Pre-edit original never versioned → Revert to v1 restores first *approved* text, not AI draft; label "v1 · reviewer" implies otherwise. Fix: append previous text before overwriting.

**N7 [medium] Bulk approve silently discards typed edits.** `ProposalQueue.tsx:331-344` sends only ids+reviewer (client.ts:290-294); text typed into expanded editors of selected rows ignored, no warning. Fix: warn or include drafts.

**N8 [medium] No fetch timeout anywhere.** `client.ts:66-96` — hung connection leaves "Approving…", "Loading analytics…" (Analytics.tsx:56), "Loading settings…" (AlertsPanel.tsx:205) spinning forever. Fix: `AbortSignal.timeout(30_000)` in `request()`.

**N9 [medium] Zero fetch abort/sequencing.** No `AbortController` anywhere. Cases: min-score slider fires request per drag tick (JobFeed.tsx:157-163 → load :30-43), last response wins regardless of order; rapid Prev/Next in ProposalQueue; `openJob` A→B can land job A's clientHistory on job B's drawer (:63-69). Fix: debounce slider; sequence/abort in load.

**N10 [medium] Destructive deletes without confirmation in 7 places.** `Accounts.tsx:420-432` (platform account), `KeywordIntelligence.tsx:108`, `SearchFineTuning.tsx:92`, `SearchProfiles.tsx:114`, `ProfileManager.tsx:117/260/416`, `GigManager.tsx:470` (delete template while editing it). Only account-deletion (AccountModals.tsx:157-163) and credential deletion (Accounts.tsx:146) confirm.

**N11 [medium] Modal backdrop click discards forms; no Escape.** `common.tsx:56` `onClick={onClose}` on backdrop — one misclick loses a 430-line Search Filter form, no dirty check. No keydown handler, no focus trap.

**N12 [medium] Accessibility gaps.** Clickable `<div>` rows with no role/tabIndex/key handler: `JobFeed.tsx:186`, `ProposalQueue.tsx:422`, `Accounts.tsx:472`, item-rows in KeywordIntelligence/SearchProfiles/ProfileManager. Most `<label>`s lack `htmlFor`. `AlertsPanel.tsx:215` keys a prepending log by array index.

**N13 [medium] bootstrap.sh breaks on macOS; .env perms loose.** `scripts/bootstrap.sh:51,53` GNU-only `sed -i`. `.env` created :18 inherits umask (often world-readable) — should `chmod 600`. `printf` append :55 concatenates without trailing newline.

**N14 [medium] Demo backdoor account via documented step.** `bootstrap.sh:70-71` instructs running the seed → `demo@gighound.local / demo1234` (`seed_defaults.py:25-26`). Published-credential login on any reachable deployment; `GIGHOUND_ALLOW_REGISTRATION` also defaults open. 127.0.0.1 binding is the only thing saving it.

**N15 [medium] db/redis have no restart policy.** `docker-compose.yml:6-30` — only backend/celery set `restart: unless-stopped`; daemon/host restart leaves datastores stopped. celery-beat has no volume for `celerybeat-schedule` — container recreate loses last-run state → digest/discovery re-fires.

**N16 [medium] Token storage + transport exposure stack.** localStorage JWT (client.ts:56-58, AD-2) + token-in-WS-URL (#2) + font CDN (#8) + no CSP anywhere → any single XSS/asset-injection yields a 12h full-access token. Add CSP at minimum.

**N17 [low] Toast/timer hygiene.** `ProposalQueue.tsx:126-129`, `JobFeed.tsx:57`, `Accounts.tsx:87`, `ProfileManager.tsx:29` — overlapping setTimeouts untracked: older timer blanks newer toast early; fire after unmount.

**N18 [low] UX papercuts.** `JobFeed.tsx:76-85` archive updates row in place — archived job stays visible under "new" filter until reload; `ProposalQueue.tsx:415-416` bid-advice filter client-side over current 50-row page (counts lie); `OnboardingChecklist.tsx:43-56` counts unanswered over max 200 submitted; `Accounts.tsx:540-549` allows changing an existing account's platform (strands settings); `GigManager.tsx:288-293` URL input not in a form (`type="url"` validates nothing).

**N19 [low] Latent API-client bugs.** `client.ts:70-76` spreads `...init` after `headers` — any future caller passing `headers` silently drops Authorization; `Content-Type: application/json` sent on GETs (needless CORS preflights); `Number(draft.bid_amount)` on `"e"`/`NaN` → `null` (ProposalQueue.tsx:239).

**N20 [low] backend/Dockerfile runtime details.** `CMD ["sh","-c","…"]` (:37) → sh as PID 1, SIGTERM not forwarded, `docker stop` escalates to SIGKILL (use `exec`); no `HEALTHCHECK`; worker `depends_on: [backend]` no `service_healthy` condition.

**N21 [low] nginx.conf minimal to broken for its purpose.** No gzip, no cache headers for hashed assets, no security headers, no `/api`/`/ws` proxy — standalone image only works with fully-qualified VITE_API_URL + manual CORS, unmentioned.

**N22 [low] CI gaps.** No `timeout-minutes`, no `concurrency` cancel-in-progress, no npm audit/SBOM, no frontend tests at all (no runner configured; `eslint-disable` at ProfileManager.tsx:83 references a config that doesn't exist). Double backend test run (:36-48) re-runs the same SQLite tests against live services — thin value vs a real `alembic upgrade head` smoke test on the provided Postgres.

**N23 [info]** vite.config.ts no dev proxy (works via absolute URL + CORS_ORIGINS); no manifest icons; no meta description; `.env.example:22` DATABASE_URL password `gighound` for dev (bootstrap only rotates POSTGRES_PASSWORD).

## Top-10 risks (ranked)
1. **CRITICAL** Approve-wipes-proposal via `??` + non-nullable `humanized_text=""` (#3).
2. **HIGH** Prod bundle hardwired to localhost:8000; no VITE_API_URL in the shipping image (#1).
3. **HIGH** Cross-tenant WS message leak across logout/login (N2).
4. **HIGH** Mid-session 401 destroys unsaved reviewer edits + no draft persistence (N3).
5. **HIGH** Whole-app white screen on payload drift: no global ErrorBoundary + no runtime validation (N1, #6).
6. **HIGH** Jobs past 50 unreachable; buyer requests past 200 pending vanish (N4).
7. **MEDIUM** Infinite WS reconnect with dead token; JWT in URL logs (#2, N16).
8. **MEDIUM** generation_failed zombies + bulk-approve discards edits + version history loses original (N5, N7, N6) — review loop leaks work.
9. **MEDIUM** Fresh-deploy schema race (#9); db/redis no restart policy (N15).
10. **MEDIUM** Documented demo login with published credentials (N14) + Node 20 EOL + unaudited frontend deps (#8).

## Ease-of-use scorecard

| View | Score | Justification |
|---|---|---|
| JobFeed | 5/10 | Live updates, bulk actions, good drawer — no pagination, WS items ignore filters, slider spams requests |
| ProposalQueue | 6/10 | Richest view — undermined by blank-editor bug, edits lost on 401/bulk-approve, zombie failures |
| BuyerRequestInbox | 5/10 | Simple, focused, quota pill — 200-cap invisibility, crash-prone `!`, edits lost on expiry |
| Accounts | 6/10 | Strong credential UX — OAuth code-paste ordeal, no confirm on account delete, editable platform |
| Analytics | 7/10 | Clear empty states, honest messaging; read-only, load-once, no timeout |
| Settings/Onboarding | 7/10 | Onboarding strip, live boolean validation, filter preview, scorer playground — confirm-less deletes, backdrop-click form loss |

Weighted overall: **~5.5/10** — unusually complete for this stage, but the review loop (product's core) has three separate ways to silently lose user work, and the feed can't reach old data.
