# Review Round 2 — Pass 1: Third-Party / Supply-Chain Audit

Date: 2026-08-31 · Scope: every dependency surface (pip, npm, Docker images, CI actions, runtime CDNs/installers, licenses) · Method: read all manifests/lockfiles/Dockerfiles/CI; ran `pip-audit` (backend venv) and `npm audit` (frontend); verified maintenance status via PyPI/npm/endoflife.date.

## Findings

**1. [high] Web framework pinned to a line with 7 known-vulnerability advisories, all suppressed in CI**
Evidence: `backend/requirements.txt:1` (`fastapi==0.115.*`); installed tree resolves to `fastapi==0.115.14` / `starlette==0.46.2`; `.github/workflows/ci.yml:98-107` ignores PYSEC-2026-161, -248, -249, -1941, -1942, -2280, -2281. Ran `backend/.venv/bin/pip-audit -r backend/requirements.txt` — reports exactly these 7 IDs (9 rows), all in `starlette 0.46.2`, fix versions 0.47.2 / 0.49.1 / 1.0.1 / 1.1.0 / 1.3.0 / 1.3.1. The ignore list is accurate *for this pin* — but the pin is the problem: fastapi 0.115 caps starlette `<0.47`, while latest fastapi is `0.141.1` (requires `starlette>=0.46.0`, no upper cap).
Why it matters: internet-facing multi-tenant SaaS whose HTTP layer carries 7 known advisories; the workaround is an ever-growing ignore list rather than a fix. CI comment sets a revisit deadline of 2026-10.
Fix: upgrade to `fastapi==0.141.*` (allows `starlette>=1.3.1`, clearing all 7 advisories), re-run the test suite, delete the entire `--ignore-vuln` block. Keep pip-audit as a hard gate.

**2. [high] Node.js 20 runtime is EOL — used in both Dockerfiles and CI**
Evidence: `backend/Dockerfile:7` (`node:20-slim`), `frontend/Dockerfile:9` (`node:20-slim`), `.github/workflows/ci.yml:68` (`node-version: 20`). Node 20 EOL was **2026-04-30** (endoflife.date).
Fix: `node:22-slim` (LTS to 2027-04) or `node:24-slim` (to 2028-04) in both Dockerfiles and CI.

**3. [medium] No Python lockfile or hashes — builds not reproducible**
Evidence: `backend/requirements.txt:1-15`, `worker/requirements.txt:1-5` use wildcard minor pins only; no lock/hashes. Drift visible: venv resolved `uvicorn==0.30.6` (latest 0.52.4); `cryptography==50.0.1`, `httpx==0.27.2` float freely.
Fix: `pip-tools` or `uv` compiled `requirements.lock` with hashes; Docker installs with `--require-hashes`.

**4. [medium] `passlib` abandoned; bcrypt pinned back to accommodate it**
Evidence: `backend/requirements.txt:12-13` (`passlib[bcrypt]==1.7.*`, `bcrypt==4.0.*`). passlib last release 1.7.4, **2020-10-08**. Verified working on Python 3.12 (hash/verify clean, no warnings), but passlib's `import crypt` **breaks on Python 3.13+** — a migration landmine.
Fix: replace passlib with direct `bcrypt` (or `pwdlib`), unpin bcrypt to `>=4.2`. Small surface (auth.py hash/verify only).

**5. [medium] All Docker images float; `ollama/ollama` implicit `:latest`; zero digest pinning**
Evidence: `backend/Dockerfile:7,15`, `frontend/Dockerfile:9,18`, `docker-compose.yml:7` (`postgres:16-alpine`), `:23` (`redis:7-alpine`), `:83` (`ollama/ollama` — no tag); `worker/Dockerfile:1`; `ci.yml:12,23`. No `@sha256:` anywhere.
Fix: pin minor tags at minimum; ideally digest-pin with Dependabot/Renovate to bump.

**6. [medium] pip-audit CI covers backend only; worker deps and npm never audited**
Evidence: `.github/workflows/ci.yml:98-107` audits `backend/requirements.txt` only. Ran `npm audit` in frontend/: prod deps clean; dev deps have 2 advisories — `esbuild@0.21.5` (GHSA-67mh-4wv8-2f99) via `vite@5.4.21`.
Fix: add `pip-audit -r worker/requirements.txt`; add `npm audit --omit=dev` gate; upgrade vite to clear esbuild advisory.

**7. [medium] Google Fonts loaded from CDN at runtime**
Evidence: `frontend/src/styles.css:5` — `@import url('https://fonts.googleapis.com/css2?...')`. Only runtime CDN reference found repo-wide.
Why: leaks visitor IPs to Google (GDPR exposure), render-blocking third-party SPOF.
Fix: self-host via `@fontsource/space-grotesk` + `@fontsource/inter`.

**8. [low] GitHub Actions pinned to floating major tags; pip-audit itself unpinned**
Evidence: `ci.yml:30-31,66` (`@v4`/`@v5` tags), `:89` (`pip install pip-audit` no version).
Fix: pin actions to commit SHAs with version comments; pin pip-audit; enable Dependabot.

**9. [low] `pytest` shipped in production worker image; worker container runs as root**
Evidence: `worker/requirements.txt:5` (`pytest==8.*`), `worker/Dockerfile:6` installs it; no `USER` directive (contrast `backend/Dockerfile:31-32`).
Fix: split worker dev deps; add non-root user.

**10. [low] Redis 7 image is SSPL/RSALv2-licensed, not BSD**
Evidence: `docker-compose.yml:23` (`redis:7-alpine` → ≥7.4). Redis relicensed from BSD to RSALv2/SSPLv1 in 7.4 (AGPLv3 added as option in Redis 8).
Why: internal broker use is permitted, but SSPL in the shipping stack is what customer license scans flag.
Fix: switch to `valkey:8-alpine` (BSD, drop-in; `redis==5.0.*` client works unchanged) or pin `redis:7.2-alpine` (last BSD line).

**11. [low] `.env` not excluded from Docker build context**
Evidence: `.dockerignore:1-12` lacks `.env`; build context is repo root (`docker-compose.yml:38`). No Dockerfile COPYs `.env` (verified) — context-leak only.
Fix: add `.env`, `**/.env` to `.dockerignore`.

**12. [info] Local dev runs Python 3.14 while CI/Docker run 3.12**
Evidence: venvs are 3.14.7; Docker/CI use 3.12. Invites works-on-my-machine drift (see passlib/py3.13 note above).
Fix: standardize on one minor.

**13. [info] Everything else clean**
- `npm audit --omit=dev`: 0 vulns; lockfile v3 in sync (115 locked / 7 direct). React 18.3.1 final 18.x (React 19 = consideration, not vuln).
- `playwright==1.62.*` is the latest release; browser download at image build is the standard mechanism.
- No `curl|bash` installers; `scripts/bootstrap.sh` uses `openssl`/python `secrets` only. No raw.githubusercontent fetches.
- Ollama integration opt-in via compose profile; no phone-home in app code. Model pulls are unvetted by nature — note in security docs.
- Licenses: all MIT/BSD/Apache except `psycopg2` (LGPLv3, fine server-side). No GPL/AGPL/SSPL in app deps. Postgres/nginx/Node/Ollama permissive.
- Behind current but maintained: celery 5.4.* (latest 5.6.3), uvicorn 0.30.*, httpx 0.27.* — no CVEs for installed versions; schedule minor-bump.

## Dependency inventory summary

| Area | Direct deps | Pinning | Worst offender |
|---|---|---|---|
| backend/requirements.txt | 15 | wildcard minor, no hashes | fastapi 0.115.* → 7 starlette CVEs suppressed; passlib abandoned 2020 |
| worker/requirements.txt | 5 | wildcard minor, no hashes; never audited | pytest in prod image |
| frontend (npm) | 7 (115 locked) | caret + healthy lockfile | vite 5.4.21 → esbuild GHSA-67mh-4wv8-2f99 (dev-only) |
| Docker/compose images | 7 | 0 digest-pinned, all floating | `ollama/ollama` implicit `:latest`; `node:20-slim` EOL |
| CI actions | 3 | floating tags, scanner unpinned | 7 ignored CVEs (verified all firing, all starlette) |
| Runtime hand-me-downs | — | — | Google Fonts CDN (`styles.css:5`) only one found |

**Top three to act on:** (1) fastapi upgrade + delete CVE ignore list, (2) Node 22/24 LTS, (3) extend audit gating to worker+npm; pin ollama image.
