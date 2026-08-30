# GigHound — Evolution Program

Started: 2026-08-28

Goal: evolve GigHound from a job *finder/drafter* into a tool that measurably helps
**secure** jobs/gigs, advanced along 5 pillars — Scalability, Sellability, Runability,
Ease of use, Competitive advantage — while staying as close to fully automated as
platform enforcement allows (human-in-the-loop approval is the compliance boundary).

## Method

Three full end-to-end analysis passes, each from a distinct perspective, followed by a
cross-examination pass that re-verified the load-bearing findings against the code:

| Document | Perspective |
|---|---|
| `pass1-systems-audit.md` | Systems / code archaeologist — every function, data flow, contract drift, runability, tests |
| `pass2-security-compliance-audit.md` | Adversarial security & platform-compliance auditor |
| `pass3-product-strategy-audit.md` | Product strategist — does it help *win* gigs; 5-pillar scorecard |
| `cross-examination.md` | Verification of the findings each plan item relies on |
| `plan-of-attack.md` | The proposed implementation plan (pending user review) |

Verified during the passes: `pytest` 74/74 green, `tsc -b` clean, seed script idempotent.

## Headline conclusions (all three passes agree)

1. **The HITL review queue is real and well-built** — the strongest part of the product.
2. **The API has zero authentication.** Anyone who can reach port 8000 can place bids
   with the user's vaulted credentials. Blocking for any non-localhost use.
3. **The learning loop is open at three points**: discovery is unscheduled, outcomes are
   never auto-detected, and recorded feedback is half-dropped (`prompt_hints` computed
   but never passed) or half-bypassed (bulk-approve skips templates + audit).
4. **Submission is broken in-repo**: the stealth-browser worker does not exist, and the
   queue submit path can never succeed (no `bidder_id`/`on_behalf_of` source).
5. **User-facing surfaces that don't do anything**: SearchFilters don't gate the pipeline,
   Profile pitch templates are never consumed, `PlatformAccount.enabled` is never read,
   Fiverr "Buyer Requests" automation targets a platform feature retired in 2022.
6. GigManager has confirmed field mismatches with the API, one of which crashes the view.
