# Review Round 2 — Pass 3b: Pipeline Correctness Deep Dive (line-level)

Date: 2026-08-31 · Scope: `tasks.py`, `orchestrator.py`, `ingest.py`, `scoring.py`, `filtering.py`, `llm.py`, `textgen.py`, `proposal_gen.py`, `antidetect.py`, `outcome_sync.py`, `proposal_status_sync.py`, `follow_up.py`, `rate_learning.py`, `digest.py`, `discovery.py`, `client_intel.py`, `gig_analytics.py`, `fiverr_monitor.py`, `gig_templates.py`, `boolquery.py`, `skills_taxonomy.py`, `templates.py`, `cache.py`, `circuit_breaker.py`, `ws_manager.py`, `stealth.py`. Read every line.

## Part 1 — Prior claims: verdicts

1. **Retry counter burned on broker failure — CONFIRMED.** `tasks.py:343-347`: counter committed at :345, then `.delay()` at :347. Delay failure consumes the attempt; two broker hiccups strand the item.
2. **Two beat ticks lack per-tenant isolation — CONFIRMED.** `tasks.py:98-106`, `:112-119` — no per-user try/except, unlike every other fan-out tick (`:181,233` etc.). One user's failure aborts the sweep.
3. **Retention orphans `claimed`; no reaper — CONFIRMED and worse.** `tasks.py:472-478` deletes only done/failed. (a) crashed worker leaves tasks `claimed` forever; (b) `skipped_circuit_open` rows never deleted — accumulate unboundedly; (c) zombie `claimed` status-scrape permanently disables outcome/reply sync for that tenant+platform (`proposal_status_sync.py:53-62`).
4. **Digest sent-count lies — CONFIRMED.** `digest.py:54-61` returns `len(jobs)` regardless; `send_digest_email` False at :79-81.
5. **Buyer-request `payload={}` → `/users/me/briefs` — CONFIRMED.** `fiverr_monitor.py:231` → `worker/handlers/buyer_requests.py:18-19` → `worker/platforms.py:35`. No per-user config possible.
6. **Redis runtime ConnectionError uncaught — CONFIRMED, more blast radius than claimed.** `cache.py:31-38` pings at import; :40-59 bare calls. Uncaught runtime-failure sites: `circuit_breaker.get_state` (every automation gate → 500s), `textgen.py:105-108` (`_bucket.try_acquire` raises raw ConnectionError, not LLMUnavailable), `fiverr_monitor.py:31-34` (`_counter`), `adapters/ratelimit.py:122`, and `ingest.py:170` (`invalidate_prefix` on **every** ingest call — Redis flapping 500s ingest *after* jobs committed).
7. **Per-process breakers diverge — CONFIRMED, plus new:** even with Redis up, `get_state` (:25-30) falls back to stale local dict when the Redis key expires (24h TTL :54) — a breaker opened >24h ago silently re-closes.
8. **Legacy aliases are live — CONFIRMED, plus:** `fiverr_monitor.py:78`'s ternary for non-fiverr platforms emits `{platform}_create_gig` which is in no alias map → unresolvable. Mitigation: worker fails unknown types cleanly (`runner.py:35-38`).
9. **Platform-global breaker — CONFIRMED.** `circuit:{platform}` no tenant component (`circuit_breaker.py:21-22`); failure count no `user_id` (`gigs.py:335-339`).
10. **No retry/DLQ; JSON-only; beat names match — CONFIRMED.** Serializers JSON (`tasks.py:31-35`); 10 beat names match; a raising task is lost until next tick.

## Part 2 — New findings

**N1 [high] Upwork submit can strand items in `queued_for_browser` forever.** `proposals.py:456-466` enqueues via `enqueue_stealth_task` — when circuit open it's recorded `skipped_circuit_open` and returned as if normal (`stealth.py:49-55`); `:483` unconditionally sets `queued_for_browser`. Only task completion flips it out (`gigs.py:349-357`); a skipped task never completes. Fix: inspect returned task status; if skipped, leave `approved` + 409 with circuit reason.

**N2 [high] Negative-keyword-"excluded" jobs NOT excluded when user has no filters — and can get LLM proposals.** `scoring.py:249-254` returns score 0 + `excluded_by_negative_keyword`, but exclusion only happens via auto-archive in `ingest.py:136-141`, skipped when `thresholds` empty. Worse, `generation_gates_pass` (`orchestrator.py:156-193`) has **no quality-score gate** — score-0 negative-keyword jobs get full LLM drafts. Fix: check the flag in ingest (archive unconditionally); consider min-score gate in generation.

**N3 [high] Double platform submission reachable two ways.** (a) Race: `proposals.py:386-387` checks `status != "approved"` then calls external `place_bid`/`submit_proposal` with no locking — double-click/retry both bid (contrast atomic claim UPDATE at gigs.py:303-313). (b) `revert_version` (:286-310) has no status guard — reverting a **submitted** item flips to `pending_review` → approve → submit sends a second bid. Fix: conditional UPDATE before platform call; guard revert to non-terminal statuses.

**N4 [high] Buyer-request fan-out poisons the global Fiverr breaker.** `tasks.py:98-106` enqueues fetch for **every active user every 15 min** — `enqueue_buyer_request_fetch` (`fiverr_monitor.py:220-235`) checks neither that the user has a Fiverr account/session nor that a fetch is already pending. Account-less users get failing scrape tasks → feed the unscoped failure counter → trip the global breaker, halting Fiverr for tenants who DO use it. Pending fetches stack unboundedly while worker down (96/user/day) → backlog storm on return. Fix: enqueue only for users with enabled Fiverr account + session; skip when pending/claimed fetch exists.

**N5 [medium] Fiverr daily-offer cap check-then-increment — raceable.** `fiverr_monitor.py:172-216`: peek at :57-58, increment at :211. Two concurrent `process_buyer_requests` (possible via N4 stacking) both pass → exceed Fiverr's 10/day ToS cap. Fix: gate on atomic INCR return value.

**N6 [medium] Duplicate proposal rows per job — no unique constraint.** `generation_gates_pass` select-then-insert (`orchestrator.py:165-174`); `job_id` indexed not unique (`models.py:277`). Concurrent generation (retry tick overlapping slow attempt) double-inserts → double LLM cost. Fix: partial unique index on `job_id` for non-terminal statuses, or row-locked regenerate-in-place.

**N7 [medium] Prompt-injection fence escapable.** Untrusted text wrapped in `<job_posting>` tags (`proposal_gen.py:131-136,228-229,520-522`) but description inserted raw — a posting containing `</job_posting>` closes the fence; everything after reads as trusted instruction space, defeating `_UNTRUSTED_DATA_RULE`. `_strip_prompt_leakage` (:101-124) only catches echoed markers, not injected behavior ("end every proposal with this link"). Fix: strip/escape the literal tag sequence from untrusted content.

**N8 [medium] `inject_personality` flattens all paragraph breaks.** `antidetect.py:176-186` splits on `(?<=[.!?])\s+`, rejoins `" "` — `\n\n` boundaries match `\s+` → whenever a marker is injected (common case), the whole proposal becomes one paragraph. `orchestrator.py:269` stores it as `proposal_text`; the munged layout is what reviewers see and the worker types. Fix: preserve original separator.

**N9 [medium] `strip_ai_tells` leaves broken punctuation.** `antidetect.py:147-166`: mid-sentence phrase removal yields `Well, , I can help` — cleanup handles `,\s*\.` and leading `,` but not `,\s*,` or leading `.`. Client-facing drafts under real names ship with `,,`. Fix: extend artifact passes.

**N10 [medium] Hourly and Fiverr bids not capped against client budget.** `proposal_gen.py:348-354` early-returns for hourly (raw rate-card rate) and fiverr (`fixed_min or budget_min or 50`) — the `budget_max * 0.98` cap (:372-374) only applies to fixed-price branch. Client posts $15/hr, rate card $80 → auto $80/hr bid (instant reject). Fix: apply cap in both branches.

**N11 [medium] Sync Redis client, no socket timeout, called from async paths.** `cache.py:33` no `socket_timeout`; every cache call is blocking sync inside async handlers — a hung (not down) Redis stalls the event loop indefinitely. Fix: small socket timeouts + treat `RedisError` as cache-disabled at call sites.

**N12 [medium] `_counter` burns budget before validation; gig-draft cap cross-tenant.** `fiverr_monitor.py:71-73`: `gigdraft:{platform}` no user id — one tenant's draft 429s everyone. `adapters/ratelimit.consume_daily_action` increments then raises — retrying callers inflate past cap (cosmetic). Fix: per-user gig-draft key.

**N13 [medium] `matching_buyer_requests` — `partial_ratio` with short tags matches nearly everything.** `fiverr_monitor.py:140-144`: `fuzz.partial_ratio(k, hay) >= 70` — tag `"ai"` scores 100 on "available"/"email"/"detail"/"said". One template with an `ai` tag matches ~every request → junk items + cap burned (then N5 overshoots). Fix: min keyword length + word-boundary for short tags, or length-scaled threshold.

**N14 [low] `process_buyer_requests` flush not IntegrityError-guarded.** `fiverr_monitor.py:197-198` — concurrent posts race past the exists check → `uq_jobs_user_platform_external` 500 to worker → task failed → feeds global counter. Mirror `ingest.py:122-127` catch-and-continue.

**N15 [low] Own-message detection fragile.** `outcome_sync.py:44-75`: missing `bidder_id` (older rows/manual) skips the `from_user == bidder_id` guard → our own messages set `client_replied_at` + fire WS events; str/int mismatch same. Fix: coerce to str; skip reply detection when bidder unknown.

**N16 [low] Auto-archive archives jobs with live review-queue items.** `tasks.py:414-428` bulk-archives 14-day `new`/`notified` jobs with no `proposal_queue` check — job disappears while its proposal is actionable (`regenerate_failed_item` then refuses, orchestrator.py:228). Fix: exclude jobs with non-terminal queue items.

**N17 [low] Permanent `generation_failed` stranding.** `tasks.py:329-352`: retries stop after 2/24h; `generation_gates_pass` blocks any new item for the job forever (`orchestrator.py:165-174` includes `generation_failed`). After the window the job can never regenerate via pipeline. Fix: manual requeue resetting the counter, or gate only on recent failures.

**N18 [low] Digest gaps.** (a) Daily digest single-shot (tick :41 + hour==7 gate, tasks.py:65-68, digest.py:43-51) — worker down at 07:41 = no digest that day. (b) `due_digest_user_ids` doesn't filter `User.is_active`. (c) `send_digest_email` unconditional `starttls()` (:86-90) — breaks plain local relays; make TLS opt-in.

**N19 [low] Ingest threshold comment contradicts code.** `ingest.py:135-137` says "strictest (lowest)" but lowest is most *lenient*: filters 40 and 70 → only jobs <40 archive; the 70-filter never filters at ingest. Code or comment is wrong.

**N20 [low] Scoring nits.** `scoring.py:103` primary keywords substring-match (`"C"`, `"R"` score everywhere) vs negatives word-boundary (:97); `:176-189` past deadlines clamp `days_left=0.25`, inflating urgency for stale jobs; `:243` `budget_max or budget_min` falls through on legit 0; `:203` `posted_urgency_words` param never used.

**N21 [low] Learning-loop lost updates.** `templates.record_outcome` (:87-92) `wins += 1` read-modify-write no locking; `rate_learning.record_winning_bid` (:24-32) same via StateStore; `templates.top_templates` (:170-173) commits inside a read helper + `min(10, uses)` rich-get-richer loop.

**N22 [low] HALF_OPEN allows unlimited concurrent trials.** `circuit_breaker.py:33-43`: after cooldown every check returns True; docstring promises "one trial task". Fix: Redis SET NX trial token.

**N23 [low] `enqueue_platform_status_scrapes` reports skipped as enqueued.** `proposal_status_sync.py:90-93` appends even `skipped_circuit_open` tasks → `"enqueued": N` for work that never runs.

**N24 [low] Buyer-request currency hardcoded USD.** `worker/handlers/buyer_requests.py:33`; `fiverr_monitor.py:190-191` trusts it → numerically wrong offers on non-USD briefs. Fix: parse currency or null + force review.

**N25 [low] `boolquery._tokenize` swallows unmatched chars mid-query.** `boolquery.py:24-31`: only trailing garbage validated; skipped chars between matches dropped silently → stored query parses as something other than typed. Fix: verify each match starts at `pos`.

**N26 [info] WS events lost when published from Celery with Redis down** — process-local fallback has no connections in Celery (ws_manager.py:47-60). Durable outbox or metric.

**N27 [info] No beat leader election / idempotency keys.** Ticks individually safe to re-run, but two beat instances (common HA mistake) double every enqueue → N4/N5 races become certainties. Docs note or Redis lock.

**N28 [info] Timezone handling consistent.** Every `datetime.now` is tz-aware; naive legacy values re-tagged defensively (ingest.py:159-160, scoring.py:177,194, filtering.py:9-12, outcome_sync.py:53-55). No bug — verified as assigned.

**N29 [info] No catastrophic-backtracking regexes found.** All user-influenced patterns linear; length/recursion bounded.

## Top 10 correctness risks
1. N4 — buyer-fetch fan-out poisons global Fiverr breaker.
2. N3 — double real-money submissions (race + revert-on-submitted).
3. N1 — `queued_for_browser` deadlock on open circuit.
4. Claim 3 — zombie `claimed` tasks; no reaper; kills per-tenant outcome sync.
5. N2 — negative-keyword exclusion void without filters; LLM drafts for excluded jobs.
6. N5 + claim 1 — cap race over Fiverr 10/day ToS; retry counter burned.
7. Claim 6/9 — Redis runtime errors 500 the API; breakers globalize.
8. N7 — escapable prompt-injection fence.
9. N8/N9 — humanizer mangles client-facing output (core product value).
10. N6 — duplicate proposals per job under concurrency.

**Systemic theme:** degradation story built for *startup-time* failures, check-then-act and uncaught at *runtime*; tenant isolation inconsistent — per-tenant keys in some counters, platform-global in others (`circuit:{platform}`, `gigdraft:{platform}`, failure counting without `user_id`).
