# Cross-Examination — Verification of Findings

**Date:** 2026-08-28 · **Method:** after the three passes, every finding that a plan item
depends on was re-verified against the code by direct read/grep, independent of the pass that
reported it. Empirical checks run during the passes (pytest 74/74, tsc -b clean, seed
idempotency) are also recorded here.

## Verified by direct read (plan-critical claims)

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| 1 | API has zero authentication; WS accepts all connections | `main.py:13-31` (no auth middleware, routers mounted bare); `alerts.py:84-93` / `ws_manager.py:15-17` | **Confirmed** |
| 2 | Queue submit can never succeed: `bidder_id` / `on_behalf_of` read from `item.submission_result`, which nothing populates | `proposals.py:229-233, 253` read directly; no writer exists anywhere | **Confirmed** |
| 3 | Upwork path sets `status="submitted"` when merely queued for browser execution | `proposals.py:272` (unconditional after upwork branch) | **Confirmed** |
| 4 | `bulk_approve` writes no AuditLog and saves no templates/versions | `proposals.py:114-130` read directly | **Confirmed** |
| 5 | `revert` rewrites text/bid in any status without resetting to `pending_review` | `proposals.py:133-149` | **Confirmed** |
| 6 | `generation_tuning()["prompt_hints"]` computed but never passed to generation | `orchestrator.py:161-166` passes only `temperature`; `templates.py:79-99` returns both | **Confirmed** |
| 7 | `job_matches_filter` never gates the pipeline (preview endpoint only) | grep: only `routers/filters.py:73` calls it; ingest uses only `quality_threshold` | **Confirmed** |
| 8 | `PlatformAccount.enabled`/`mode` never read outside its own CRUD | grep across `backend/app` | **Confirmed** |
| 9 | No scheduled discovery in Celery beat | `tasks.py:26-35` — only fiverr tick + weekly gig analytics | **Confirmed** |
| 10 | Seeded pitch templates use `{{...}}`; live path checks `"{job_title}" in tpl` and the body is `pass` — pitch templates have zero influence | `seed_defaults.py:73-122` vs `proposal_gen.py:304-311` | **Confirmed** |
| 11 | `GigTemplate.is_active` (backend) vs `active` (frontend) — toggle always shows "Activate" | `schemas.py:292` vs `types.ts:380`, `GigManager.tsx:538` | **Confirmed** |
| 12 | `GigMetric.suggestions` is `list[dict]` backend vs `string[]` frontend → React render crash | `schemas.py:325` + `gig_analytics.py:28` vs `types.ts:345`, `GigManager.tsx:254-256` | **Confirmed** (static analysis; not re-run live) |
| 13 | `CompetitorSnapshot` has `created_at`, frontend expects `date` → blank headings, undefined keys | `schemas.py:332` vs `types.ts:390`, `GigManager.tsx:848-849` | **Confirmed** |
| 14 | Direct-write adapter endpoints bypass the queue with a free-text `approved_by` | `routers/adapters.py:71-88, 117-130` | **Confirmed** |

## Verified during the passes (empirical)

- `pytest` → 74/74 green (Python 3.14; heavy pytest-asyncio deprecation warnings).
- `tsc -b` → clean (the frontend/backend mismatches are *declared* wrong types, so the compiler
  cannot catch them).
- `seed_defaults` idempotent on SQLite, twice.
- `test_orchestration.py` made real LLM calls to the hardcoded LAN Ollama address
  (192.168.1.68:11434) because it lacks the `force_offline` fixture — tests are not hermetic.

## Consistency check across the three passes

- All three passes independently converge on: no auth; the queue/HITL design being the strongest
  asset; the learning loop being open (unscheduled discovery, no outcome sync, dropped
  prompt_hints, bulk-approve hole); the stealth worker being absent; fiverr_monitor targeting a
  retired platform feature; GigManager mismatches. No contradictions between passes.
- Pass 2's "HITL boundary is a string" and Pass 1's "submit button is broken" describe the same
  submit path from opposite sides — consistent, and both verified (items 2, 14).
- Pass 3's "filters are a preview toy" matches Pass 1's "filtering.py untested and unused in
  ingest" — consistent, verified (item 7).

## Caveats / not fully verified

- The React render crash (item 12) is a static-analysis conclusion from exact response shapes vs
  render code; not re-run against a live server. Confidence: high (object as React child is a
  hard crash, no error boundary exists).
- ScoringConfig playground polluting the production feed was read from code
  (`ScoringConfig.tsx:41-47` → real ingest call); not executed. Confidence: high.
- Dependency CVE notes in Pass 2 §7 are version-lag observations, not a vulnerability scan —
  `pip-audit` in CI is the recommended remedy rather than a hand-maintained CVE list.
- antidetect "theater" assessments (word lists won't move modern classifiers) are expert
  judgment, consistent with the project's own intel report; not empirically testable here.

## Corrections made during cross-examination

- None of the plan-critical findings were overturned. Two findings were *strengthened* on
  re-read: the submit dead-end (Pass 1 said "effectively broken"; direct read confirms it is
  broken for 100% of freelancer and upwork submissions), and the pitch-template deadness (the
  `pass` body at proposal_gen.py:311 makes it absolute, not conditional).

## Conclusion

All findings the plan relies on are accurate as of 2026-08-28 and will pan out as described.
The plan of attack proceeds on this basis.
