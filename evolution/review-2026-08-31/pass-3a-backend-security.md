# Review Round 2 — Pass 3a: Backend Security Deep Dive (line-level)

Date: 2026-08-31 · Scope: `auth.py`, `main.py`, `config.py`, `database.py`, all 12 router files (every endpoint), `schemas.py`, `models.py`, `adapters/vault.py`, plus cross-verification files. Read every line.

## Prior-claim verdicts

**Claim 1 (stealth-session leaks sessions) — CONFIRMED with correction.** `GET /api/gigs/stealth-session` (gigs.py:243-275) gated only by `get_worker`; returns decrypted Playwright `storage_state` (account-takeover material) for any caller-supplied `user_id` (:264-275). No task binding, no rate limit, **no audit row for the read**. Correction: it returns only `storage_state_json`, not username/password (:268-275) — but storage_state *is* the session. Also: account filter checks `enabled.is_(True)` but not `mode != "disabled"` (:255-259) — mode-disabled accounts still leak sessions, inconsistent with kill-switch semantics (`auth.py:186-196`).

**Claim 2 (worker posts trust body tenancy) — CONFIRMED, deeper than claimed.** `POST /metrics`: worker path does `db.get(Gig, body.gig_id)` cross-tenant (gigs.py:171-174) — fabricated metrics into any tenant's gig. `POST /competitors` trusts `body["user_id"]` (:204-211); no AuditLog in `store_competitor_snapshot` (gig_analytics.py:72-83). `POST /buyer-requests/process` trusts `body["user_id"]` (:232-238) — burns victim's 10-offers/day Fiverr budget on fabricated requests (fiverr_monitor.py:172,211). Deeper: `POST /stealth-tasks/{id}/complete` never checks completer is `claimed_by` (gigs.py:317-346) and accepts `pending→done/failed`; `POST /api/gigs/proposal-status` lets caller pick **any** `task_id` — per-row tenancy binds results to `task.user_id` (proposal_status_sync.py:106-109) but the worker chooses the task → can flip any tenant's proposals to `hired`/`rejected`, corrupting win-rate learning.

**Claim 3 (tenant scoping consistent) — CONFIRMED with two exceptions.** Spot-checked all ~70 endpoints. Exceptions: (a) `SearchProfileIn.keyword_group_id`/`filter_id` never ownership-validated (orchestration.py:23-29,32-42) and consumed **unscoped** in discovery.py:60-65,73-77 — see finding 6; (b) `register_gig` accepts unvalidated `template_id` (gigs.py:152).

**Claim 4 (passlib) — CONFIRMED working as pinned.** Executed installed env: passlib 1.7.4 + bcrypt 4.0.1 hashes/verifies cleanly, no warnings. Residual: unmaintained since 2020; blocks bcrypt updates; `import crypt` breaks on Python 3.13 (Docker is 3.12 — landmine, not live bug).

**Claim 5 (JWT/WS) — CONFIRMED.** 12h TTL (auth.py:25); HS256 pinned at encode and decode (:24,74,79 — no alg confusion); secret env-only, fail-fast (:32-45). WS via `?token=` (alerts.py:70-81), verified before accept, closes 4401 — but see finding 4.

## New findings

1. **[critical] Shared worker token = cross-tenant master credential, non-constant-time compare, no rotation/identity.** One static env string (config.py:26); `is_worker_token` uses `==` not `hmac.compare_digest` (auth.py:133-134); grants cross-tenant read of all stealth-task payloads (gigs.py:284-294) and every tenant's session cookies. One leaked worker host = full SaaS compromise. Fix: per-worker tokens or mTLS; scope stealth-session to claimed task; `compare_digest`; rotation docs; audit every credential read.

2. **[high] Stealth-task lifecycle lacks claimer binding — result injection + breaker DoS.** `complete` doesn't verify caller claimed the task (gigs.py:317-346); three injected `success=false` completions trip the platform breaker for ALL tenants (:335-343, threshold 3/hr at :29-30). `_apply_submission_outcome` does check `item.user_id == task.user_id` (:356 — good), but the task itself can be anyone's. Fix: require `task.claimed_by == body.worker_id` + `status == "claimed"`; drop pending backcompat; per-tenant failure counting.

3. **[high] No server-side token revocation; password change leaves 12h tokens valid.** `logout` acknowledged no-op (auth.py router :76-80); `change_password` leaves tokens valid (:83-92). Stateless HS256, no jti/denylist → stolen token unstoppable 12h. Account deletion does kill tokens (`is_active` re-check, auth.py:94-96). Fix: short-lived access + refresh rotation, or Redis jti denylist bumped on password change.

4. **[medium-high] JWT in WS query string → tokens in access/proxy logs.** alerts.py:71; uvicorn access log records full request line. Combined with finding 3: log leak = 12h impersonation. Fix: first-message auth or single-use WS ticket endpoint.

5. **[medium] Vault key not validated at startup; single global key, no rotation.** `validate_auth_config` (auth.py:32-48) doesn't check `GIGHOUND_VAULT_KEY` — prod boots "healthy", 500s on first credential use (vault.py:52-63). One Fernet key for all tenants, no key-id/rotation. Positives: ciphertext-only at rest (models.py:196); dev key 0600 + `O_EXCL` + gitignored (vault.py:26-49). Fix: startup check; key-id prefixing; consider per-tenant DEKs.

6. **[medium] Cross-tenant FK references on search profiles (IDOR).** create/update spread `**body.model_dump()` incl. `keyword_group_id`/`filter_id` unchecked (orchestration.py:25,38-39); discovery fetches those rows without `user_id` filter (discovery.py:60-77) → attacker profile runs discovery with victim's keyword terms/platforms (terms leak by observation). LLM-queue path re-scopes correctly (orchestrator.py:62-65,184-187). Fix: ownership-validate FKs in router; scope the discovery gets.

7. **[medium] Brute-force and enumeration gaps.** Login limiter keyed `email:ip` (routers/auth.py:27-40) — spraying from many IPs or across accounts sails past; no account lockout; increments on *successful* logins (5 legit logins/5min → self-429). Register: no rate limit/CAPTCHA; distinguishing 409 enumeration (:48-49). Timing oracle: unknown email returns fast, known pays ~250ms bcrypt. Fix: per-IP and per-account buckets; dummy-verify on unknown users; rate-limit register.

8. **[medium] Internal exception text returned to clients.** `HTTPException(502, f"submission failed: {exc}")` (proposals.py:479) and `HTTPException(502, str(exc))` (adapters.py:77,110,143,178,235) surface adapter internals/upstream bodies. Remaining `body: dict` endpoints → bare 500s on KeyError (gigs.py:149,210). Fix: log detail, return generic; typed pydantic bodies for the remaining dict endpoints (gigs.py:42,47,145,199,228,318,373; adapters.py:205; proposals.py:287).

9. **[medium] No security headers; untrusted platform content flows to SPA.** main.py:32-38 only CORS — no CSP/X-Content-Type-Options/X-Frame-Options/HSTS. Scraped titles/descriptions persisted and re-served (URL scheme validated, schemas.py:140-165 — good); if frontend renders as HTML → stored XSS. `allow_credentials=True` means sloppy `CORS_ORIGINS=*` in prod is dangerous; nothing validates it. Fix: headers middleware; startup CORS validation.

10. **[low] Worker-token timing side channel** — one-line fix: `hmac.compare_digest`.

11. **[low] Self-serve resource exhaustion.** `POST /api/filters/{id}/preview` loads ALL user jobs into memory (filters.py:75); unbounded `list[JobIngest]` per call (mitigated by 30/min bucket, jobs.py:17-34); LLM-cost endpoints (faqs/templates/follow-up/interview-prep) have no per-user rate limit. Fix: paginate preview; cap list sizes; extend Redis bucket to LLM endpoints.

12. **[low/info] Checked clean.** Zero raw SQL/`text()` (only ORM `update()`, gigs.py:303-307) — no SQLi. No SSRF (all httpx targets env-derived; user URLs scheme-validated, never fetched server-side). No secret logging (key names only, credentials.py:124-125,156-157; gigs.py:273). boolquery DoS-hardened (1000-char cap, depth 32, `re.escape` — boolquery.py:16-17,123). Mass assignment contained to typed schemas. No cookies/uploads. `.env.example` ships no real secrets; compose requires POSTGRES_PASSWORD; ports bind 127.0.0.1. proposal-status per-row tenancy + complete_submission owner checks correct.

## Ranked top-10 security risks
1. Worker token = cross-tenant master key pulling live session cookies.
2. Unbound stealth-task lifecycle — result injection, win-rate corruption, breaker DoS.
3. No token revocation — stolen JWT valid 12h after logout/password change.
4. JWT in WS query string → logs.
5. Vault operability/crypto agility — no startup check, one global key, no rotation.
6. Cross-tenant FK references on search profiles.
7. Credential brute-force/enumeration.
8. Session-cookie reads never audit-logged (writes are, credentials.py:109-112).
9. Error-message leakage of adapter internals.
10. Dependency/runtime debt — passlib, missing security headers with untrusted scraped content.
