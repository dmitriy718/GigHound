# Review Round 2 — Pass 3c: Stealth Browser Worker Deep Dive (line-level)

Date: 2026-08-31 · Scope: entire `worker/` package (browser.py, client.py, config.py, login.py, `__main__.py`, platforms.py, runner.py, all handlers/), worker/Dockerfile, worker/requirements.txt, worker/README.md, docker-compose.worker.yml, cross-checked against backend `gigs.py`/`stealth.py`/`fiverr_monitor.py`/`gig_analytics.py`/`proposals.py`. Read every line.

## Prior-claim verdicts

**Claim 1 (two crash paths) — CONFIRMED both.**
- Path A: `proposal_status.py:113-114` posts results → backend `gigs.py:395-399` sets task `done` → runner's success branch `runner.py:56` calls `complete_task` → backend 409 (`gigs.py:325-326`) → `ClaimConflictError` (`client.py:62-63`). `runner.py:56` sits in the `else:`, outside the try; `poll_once` (`:63-66`) only wraps `poll_tasks`; `main` (`:91-99`) catches only KeyboardInterrupt → process exits. Compose has no `restart:`. Irony: crash happens exactly when the scrape *found data* (`proposal_status.py:113` guards `if results:`).
- Path B: `client.py:54-56` re-raises `httpx.TransportError` on final retry; `runner.py:65` catches only `(BackendError, ClaimConflictError)` → escapes → process dies. Same exposure at `runner.py:44,50` (failure-report `complete_task` calls).
- `worker/README.md:22` claims "the loop never dies" — false.

**Claim 2 (submitted:false reported as success) — CONFIRMED mechanism, latent.** `manual_assist.py:53-60` returns `submitted: False`; `runner.py:56` reports `success=True`; backend `_apply_submission_outcome` (`gigs.py:352-357`) flips to `submitted` on success alone. Latent only because no backend emitter exists for `submit_proposal`/`submit_fiverr_offer`.

**Claim 3 (`/users/me/briefs`) — CONFIRMED.** `buyer_requests.py:18-19` default `"me"` → `platforms.py:35`. Backend always enqueues `payload={}` (`fiverr_monitor.py:225,231`). "me" is not a valid Fiverr username → 404/login redirect → zero briefs forever, reported as `fetched: 0` success.

**Claim 4 (supported platforms) — CONFIRMED.** `config.py:12` = fiverr/upwork/peopleperhour/guru; `validate()` rejects linkedin/indeed (`:57-62`).

**Claim 5 (legacy aliases live) — CONFIRMED, worse.** Live legacy emitters: `fiverr_fetch_buyer_requests` (fiverr_monitor.py:225,231), `gig_scrape_metrics` (gig_analytics.py:93), `fiverr_create_gig` (:78), `upwork_catalog_upsert` (:105). Two new bugs: (a) `fiverr_monitor.py:78` emits `{platform}_create_gig` for non-Fiverr templates → in no alias map → every such task fails "no handler"; (b) `upwork_catalog_upsert` → CREATE_GIG_DRAFT handler → `gig_draft.py:30-35` raises "no gig form config for 'upwork'" (upwork dict in `platforms.py:96-133` has no `gig_new_url`/`gig_form`) — **every Upwork catalog task fails 100%**, and its `publish` flag is silently ignored.

**Claim 6 (pytest/root/no-restart) — CONFIRMED, plus:** compose sets **no `shm_size`** and browser.py:189 doesn't pass `--disable-dev-shm-usage` (see finding 10).

## New findings

1. **[critical] Worker dies on the two most common events — successful status sync or transient network blip — and nothing restarts it.** (Evidence: claim 1.) Every `scrape_proposal_status` matching ≥1 proposal kills the worker; pipeline silently stops. Fix: treat 409-on-complete as benign; catch `TransportError` around `process_task`/`complete_task`; `restart: unless-stopped` + healthcheck.

2. **[critical] No per-tenant IP isolation — all tenants share one datacenter IP per platform.** `config.py:46-48` keys proxies by *platform* only (`WORKER_PROXY_UPWORK`); `browser.py:191-193` applies it to every tenant's context. 50 tenants' Upwork accounts from one cloud IP = textbook account-linkage ban vector; one ban cascades. Fix: per-(tenant, platform) sticky proxy assignment served by backend (via stealth-session response), geo-matched to account profile.

3. **[high] Duplicate Upwork submission on post-click failure.** `upwork_proposal.py:59-62`: after `page.click(form["submit"])`, any exception (wait_for_load_state timeout, challenge, screenshot) reports *failed* — but the proposal may be submitted. Human re-approves → worker submits again. Two identical proposals from an agency BM = spam signal. Fix: post-click state verification (success toast/URL change/"already applied" marker); classify ambiguous as `submitted_unverified`, not `failed`.

4. **[high] Submission never verified — failures reported as success.** `upwork_proposal.py:59-72` returns `submitted: True` unconditionally; `wait_for_load_state("domcontentloaded")` returns immediately on SPAs. Upwork validation error / insufficient connects / closed job → backend still flips to `submitted` (gigs.py:356-357). Same in `manual_assist.py:44-48`. Fix: per-platform `submit_success` confirmation selector + screenshot-on-failure.

5. **[high] Stale enrolled localStorage force-written on every navigation, forever.** `_seed_session` → `apply_storage_state` registers `context.add_init_script` (`browser.py:137-140`) — runs on every page load for the context's lifetime, overwriting live localStorage with weeks-old values (rotated CSRF/session tokens stomped). Fix: apply once after first navigation; prefer cookie-only seeding.

6. **[high] No session-expiry detection; logged-out sessions produce empty *successes*.** `fetch_page` (`base.py:23-30`) only checks CAPTCHA markers; `buyer_requests.py:22-38` posts empty list as success; `scrape.py:27-31` posts `impressions=0, clicks=0, orders=0, revenue=0.0` when extraction yields nothing — silently corrupting analytics with fabricated zeros. Fix: per-platform `logged_in_marker`/login-redirect check → fail `session_expired`; skip `post_metrics` when zero fields extracted + report `selector_suspect`.

7. **[high] Fingerprint internally inconsistent, mutates over time.** UA random *per context launch* (`browser.py:185`) — same account's UA changes across restarts; curated UAs are Chrome 124–126 (`browser.py:28-37`) while playwright 1.62 bundles far newer Chromium — `userAgentData`/Sec-CH-UA leak the real version, contradicting the UA; timezone/locale global constants (`config.py:42-44`, America/New_York, en-US) for every tenant; viewport re-rolled each launch (`:181`). Fix: pin a coherent fingerprint bundle per (tenant, platform), persisted; derive UA from actual Chromium build; align TZ/locale with proxy geo.

8. **[high] Antidetect surface is one flag.** `browser.py:189` only `--disable-blink-features=AutomationControlled`. No init-script patches for `navigator.webdriver` (exposed in headless), WebGL vendor/renderer (GPU-less container reports SwiftShader — loud datacenter tell), canvas/audio entropy, plugins, permissions. Fix: minimal stealth shim (webdriver delete, WebGL UNMASKED spoof, plugins/languages coherence) or a maintained stealth layer.

9. **[high] Browser contexts never close during the run — memory grows with tenant count.** `_contexts` (`browser.py:158,197`) caches one full persistent Chromium per (platform, user_id) for process lifetime; `close_session` only at exit. N tenants × 4 platforms = 4N Chromium processes idling for days. Fix: idle-TTL reaper (also fixes stale-session refresh, #23).

10. **[high] Docker: no /dev/shm mitigation.** No `shm_size`, no `--disable-dev-shm-usage`; Chromium in Docker defaults to 64MB /dev/shm → renderer crashes ("Aw, snap") under load → flaky failures → trips breaker. Fix: `shm_size: '1gb'` (one line).

11. **[medium] Same profile dir usable by multiple workers concurrently.** All replicas mount the same `worker_sessions` volume (compose :15-17); two workers on same tenant+platform launch two Chromiums on one `user_data_dir` → profile-lock corruption + one account active from two browsers/IPs. Fix: per-worker volumes or backend-claimed (platform, user) lock.

12. **[medium] Metronomic typing; token mouse movement.** `keyboard.type(word, delay=delay_ms)` (`browser.py:88-92`) constant inter-keystroke interval — fixed cadence is a bot signature; no fatigue/punctuation pauses/bursts. `mouse_wiggle` (3 moves) only in Upwork handler (`upwork_proposal.py:32`); scrapes do no scrolling/hovering; every visit is cold direct-URL `goto` (`base.py:27`), no referrer/warm-up. Fix: randomized per-keystroke delay + long pauses; scroll/read pauses in scrapes; dashboard→target navigation.

13. **[medium] No circadian model.** Tasks execute instantly at any hour in one fixed timezone. Accounts active 3:47 AM nightly = anomalous. Fix: server-side scheduling windows per tenant timezone.

14. **[medium] Partial-fill drafts saved without validation.** `gig_draft.py:38-56` fills what it can (`_fill` silently skips missing selectors, :17-23), unconditionally clicks Save-as-Draft — drifted form → half-empty draft on tenant's real account. Fix: require ≥N expected fields filled before saving; report missed selectors.

15. **[medium] Shared page object across tasks.** `new_page` reuses `context.pages[0]` (`browser.py:225`), never closes — crashed task's leftover DOM state (open modal, half-filled form) persists into next task. Fix: fresh page per task + `page.close()` in finally.

16. **[medium] No task-level timeout; one slow task starves the fleet.** Single-threaded loop (`runner.py:91-99`); `type_with_plan` on a long letter takes minutes; nothing bounds a handler end-to-end. Fix: per-task wall-clock budget + cancellation + failure report.

17. **[medium] Root `.env` (all backend secrets) injected into worker container.** `docker-compose.worker.yml:8-10` `env_file` — DB creds, LLM keys, everything — into a root container running a browser processing hostile web content. Fix: pass only the 4 worker vars via `environment:`.

18. **[medium] Session secrets + PII screenshots at rest unencrypted, root-owned.** Cookies/tokens in `user_data_dir` + `storage_state.json` (`browser.py:234`), full-page screenshots of tenant inboxes/proposals (`browser.py:241-243`, `manual_assist.py:39-40`), default 0644/0755. Fix: 0700 dirs, encrypt storage_state, document retention/wipe.

19. **[low] Unstable buyer-request IDs and relative URLs.** `buyer_requests.py:27` falls back to `f"brief-{i}"` (index-based → different ID every scrape → backend dedup impossible); `extract_cards` stores raw possibly-relative `href` (`base.py:74-78`). Fix: absolutize URLs; stable ID from brief URL slug.

20. **[low] Fragile proxy parsing / selector edges.** `_parse_proxy` (`browser.py:112-114`) emits `http://host:None` for port-less URLs → launch fails. `buyer_requests.py:22` passes possibly-empty `brief_card` selector to Playwright. `proposal_status._matches` (:58-67) substring-matches external IDs — false positives on short IDs. Fix: validate port; guard empty selectors; anchor ID matching.

21. **[low] `WORKER_ALLOW_SUBMIT` is a global all-tenant all-platform switch.** `config.py:37` — one env var turns on real submissions for everyone. Fix: per-platform (or per-tenant) gates.

22. **[info] Manual-assist handlers currently dead code** — no backend emitter. Wire or drop; claim 2 goes live the moment they're wired.

23. **[info] Enrolled-credential updates never picked up at runtime.** `get_stealth_session` only at context creation (`browser.py:196-206`); contexts live forever (#9) — re-enrolling a session has no effect until restart.

24. **[info] XSS posture acceptable within worker.** Scraped text read-only (`inner_text`), POSTed as JSON; `apply_storage_state` encodes via `json.dumps`. Residual risk: frontend rendering — covered in pass 3d.

## Top-10 risks (ranked)
1. Crash-on-success (409) + crash-on-blip, no restart — pipeline silently dead.
2. One datacenter IP per platform for all tenants — mass linkage ban vector.
3. Duplicate Upwork submissions after post-click ambiguity.
4. Unverified submit reported as success — backend lies to tenants about their livelihood.
5. `fetch_buyer_requests` returns nothing forever, silently.
6. Fingerprint incoherence (rotating UA, stale versions vs UA-CH, one global TZ).
7. One-flag antidetect with SwiftShader/WebGL headless tells.
8. `upwork_catalog_upsert` fails 100%; non-Fiverr `{platform}_create_gig` has no handler.
9. Zeroed metrics/empty scrapes posted as success — analytics corrupted, dead sessions invisible.
10. Unbounded Chromium accumulation + 64MB /dev/shm crashes.

## Ban-risk scorecard (10 = safest)

| Dimension | Score | Justification |
|---|---|---|
| Per-tenant IP isolation | 1 | Per-platform proxy only; all tenants one IP (config.py:46-48) |
| Fingerprint consistency | 2 | UA re-rolled per launch, stale versions vs UA-CH, global TZ/locale |
| JS-environment patching | 2 | Single Chromium flag; no webdriver/WebGL/canvas shims |
| Headless concealment | 3 | New headless + flag helps; SwiftShader renderer leaks |
| Behavioral humanization | 4 | Typo-plan + WPM cadence real; per-keystroke timing constant; token mouse |
| Timing/circadian plausibility | 3 | Good micro-delays; no active-hours model; instant execution |
| Session realism & integrity | 4 | Persistent profiles good; stale localStorage re-seed; no expiry detection |
| Navigation naturalness | 2 | Cold direct-URL goto every task; no referer/warm-up/scroll |
| Safety rails (caps, HITL, breaker) | 7 | Server-side caps, draft-only gig creation, CAPTCHA escalation — solid |
| Failure-mode truthfulness | 2 | Unverified submits, duplicate-submit window, zeros-as-success |

**Overall stealth posture: ~3/10.** HITL/circuit-breaker architecture is thoughtful, but the network- and fingerprint-level isolation that keeps platform accounts alive is essentially absent — and the crash bugs mean the protective machinery stops the first time a status sync succeeds.
