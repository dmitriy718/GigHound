"""Canonical platform sets — the single source of truth for which platforms
each subsystem serves (P5-2). Previously these lists were hand-maintained in
5+ places and adding a platform meant editing all of them.

The pydantic `Platform` Literal in schemas.py stays the API type; its values
mirror ALL_PLATFORMS below (a test asserts they stay in sync).
"""

# platforms the stealth-browser worker can serve. Mirror of worker/config.py
# SUPPORTED_PLATFORMS (the worker is a separate package — a backend test
# asserts the two stay equal).
WORKER_PLATFORMS = frozenset({"fiverr", "upwork", "peopleperhour", "guru"})

# platforms with an OAuth/API credential flow
OAUTH_PLATFORMS = frozenset({"freelancer", "upwork"})

# platforms enrolled via browser-session (stealth) credentials; upwork is both
STEALTH_CREDENTIAL_PLATFORMS = frozenset({"fiverr", "peopleperhour", "guru", "upwork"})

# platforms the scheduled discovery tick searches. A tuple, NOT a frozenset:
# the search order is load-bearing (tests and pacing rely on it).
DISCOVERY_PLATFORMS = ("freelancer", "upwork", "linkedin")

# browser platforms with a read-only scrape_proposal_status page in the worker
BROWSER_SYNC_PLATFORMS = frozenset({"upwork", "fiverr", "peopleperhour", "guru"})

# every platform the API schema accepts. "indeed" is accepted for
# forward-compat but is served by NO subsystem — it appears in no set above.
ALL_PLATFORMS = frozenset({
    "upwork", "fiverr", "freelancer", "peopleperhour", "guru",
    "linkedin", "indeed",
})
