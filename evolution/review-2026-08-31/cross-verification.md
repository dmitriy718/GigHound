# Review Round 2 — Cross-Verification Log

Date: 2026-08-31 · Purpose: verify every load-bearing finding before it enters the implementation plan. Two independent verification methods were applied:

## Method 1 — Direct source re-read (by the orchestrator, after all passes)

Every critical/high finding that anchors a plan workstream was re-verified by reading the cited lines directly. Results (all CONFIRMED, zero refuted):

| # | Finding | Cited at | Re-read result |
|---|---|---|---|
| 1 | Worker crash path A: `complete_task` outside try, `poll_once` guards only `poll_tasks`, main loop bare | worker/runner.py:56, 63-66, 91-99 | CONFIRMED — exact structure as reported |
| 2 | Worker crash path B: `TransportError` re-raised after 3 tries; 409→`ClaimConflictError` | worker/client.py:54-56, 62-63 | CONFIRMED |
| 3 | Complete endpoint: accepts claimed+pending, no `claimed_by` binding; failure count platform-only; `_apply_submission_outcome` guards `queued_for_browser` + `item.user_id == task.user_id` | backend/app/routers/gigs.py:317-346, 349-367 | CONFIRMED verbatim |
| 4 | Submit path A sets `queued_for_browser` unconditionally at :483; 501 for other platforms :467-472 | routers/proposals.py:440-490 | CONFIRMED |
| 5 | Submit path B (adapters) enqueues identical task, never touches `item.status` | routers/adapters.py:149-196 | CONFIRMED |
| 6 | `revert_version` has no status guard — reverts submitted items to `pending_review` | routers/proposals.py:286-310 | CONFIRMED |
| 7 | Upwork OAuth-only; linkedin/indeed stealth-accepted | routers/credentials.py:36-37, 71-84 | CONFIRMED verbatim |
| 8 | `API_URL` defaults `http://localhost:8000`; `as T` cast; 401 clears token | frontend/src/api/client.ts:35-36, 95, 80-83 | CONFIRMED verbatim |
| 9 | `??` blank-editor fallback | frontend/src/views/ProposalQueue.tsx:80 | CONFIRMED |
| 10 | WS hook: no message clear on logout; `onclose = scheduleReconnect` unconditional; token in URL | frontend/src/hooks/useAlertsSocket.ts:29-37, 107-116, 47 | CONFIRMED verbatim |
| 11 | Worker token `==` compare | backend/app/auth.py:133-134 | CONFIRMED |
| 12 | `inject_personality` paragraph collapse (`split` on `\s+`, `" ".join`); `strip_ai_tells` missing `,,` cleanup | backend/app/antidetect.py:176-186, 147-166 | CONFIRMED |
| 13 | Raw description inside `<job_posting>` fence, no escaping | backend/app/proposal_gen.py:131-136 | CONFIRMED |
| 14 | Retry counter committed before `.delay()` | backend/app/tasks.py:343-347 | CONFIRMED verbatim |
| 15 | Retention deletes only done/failed stealth tasks | backend/app/tasks.py:472-478 | CONFIRMED |
| 16 | Redis client fixed at import; bare `get/set/delete/scan_iter` calls | backend/app/cache.py:31-59 | CONFIRMED verbatim |
| 17 | Buyer-request enqueue `payload={}` | backend/app/fiverr_monitor.py:225-231 | CONFIRMED verbatim |
| 18 | localStorage `add_init_script` re-applied every navigation; UA random per launch; single antidetect flag; per-platform proxy | worker/browser.py:137-140, 181-193 | CONFIRMED verbatim |

## Method 2 — Cross-pass corroboration (independent discovery)

The strongest findings were discovered independently by 2-4 different passes with different mandates — an accidental but effective second verification:

| Finding | Found by |
|---|---|
| Worker crash-on-success / crash-on-blip, no restart | Pass 2a (#1, #10), Pass 2b (#2), Pass 3c (claim 1, F1) |
| `API_URL` localhost baked into prod bundle | Pass 2b (#1), Pass 3d (#1) |
| `??` approve-wipes-text | Pass 2b (#5), Pass 3d (#3) |
| Platform-list drift (upwork enrollment impossible; linkedin/indeed dead) | Pass 2a (#4), Pass 2b (#9), Pass 3a (claim 3 exception context), Pass 3c (claim 4) |
| Platform-global circuit breaker / failure counting | Pass 2a (#5), Pass 3b (claim 9, N4) |
| Worker token = cross-tenant master key; stealth-session unbound | Pass 2a (#11), Pass 3a (claim 1, finding 1) |
| Legacy task_type aliases live | Pass 2a (#8), Pass 3b (claim 8), Pass 3c (claim 5) |
| `payload={}` buyer-request fetch → `/users/me/briefs` | Pass 2a (#12), Pass 3b (claim 5), Pass 3c (claim 3) |
| Retention orphans claimed tasks; no reaper | Pass 2a (#2), Pass 3b (claim 3) |
| Retry counter before `.delay()` | Pass 2a (#16), Pass 3b (claim 1) |
| Redis runtime-error escape | Pass 2a (#9), Pass 3b (claim 6, N11) |
| celery migration race | Pass 2a (#15), Pass 3d (#9) |
| WS staleness (no broadcast on submission outcome; no catch-up) | Pass 2b (#3, #4), Pass 3d (#5) |
| JWT in WS URL / 4401 infinite reconnect | Pass 2b (#10), Pass 3a (finding 4), Pass 3d (#2) |
| Dead handoffs (fiverr/PPH/guru submit, competitor scrape) | Pass 2a (#6), Pass 2b (#8), Pass 3c (#22) |
| `humanized_text` null-vs-empty semantics | Pass 2b (#5, #12), Pass 3d (#3) |

## Accuracy caveats recorded
- Pass 3c's ban-risk scorecard and Pass 3d's ease-of-use scorecard are expert-judgment scores, not measurements; they're planning inputs, not verified facts.
- Live-platform selector behavior (worker/platforms.py against real Upwork/Fiverr/PPH/Guru) remains unverifiable without real sessions — unchanged from the previous round's designated maintenance point.
- pip-audit/npm audit outputs are point-in-time (2026-08-31); the CI gates must keep them fresh.
- Findings marked "latent" (manual_assist submitted-flag, `submit_proposal` emitters) are dormant only because the producing code doesn't exist yet — the plan must fix them *before or while* wiring those producers, not after.

## Verdict
All 18 directly re-checked claims CONFIRMED with exact file:line evidence; 16 findings independently corroborated across passes; 0 refutations; 4 accuracy caveats logged. The findings set is approved as the basis for the implementation plan.
