"""Playwright lifecycle + human-behavior helpers.

Playwright is imported lazily inside BrowserManager so the test-suite and
non-browser utilities (config, client, runner wiring) work without browser
binaries installed.

Sessions: one persistent Chromium context per (platform, user_id) under
WORKER_SESSION_DIR. When a storage_state was enrolled via the Accounts UI
(POST /api/accounts/{id}/credentials), the worker fetches it from
GET /api/gigs/stealth-session and seeds the fresh context with it; otherwise
it falls back to the local persistent profile, which a human can still seed
via `python -m worker.login --platform <p>` (see README).
"""
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from time import sleep as _real_sleep
from urllib.parse import urlparse

from .config import Config
from .platforms import challenge_markers

log = logging.getLogger(__name__)

# small curated rotation of current-ish desktop Chrome user agents
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080)]


class CaptchaDetectedError(Exception):
    """A bot challenge/CAPTCHA is on the page — escalate to a human."""

    def __init__(self, marker: str, platform: str):
        self.marker = marker
        self.platform = platform
        super().__init__(f"challenge detected on {platform}: {marker}")


class SessionExpiredError(Exception):
    """The stored session is dead (login redirect / logged-out page) — the
    account needs re-enrollment. Distinct from "no data": handlers must never
    post fabricated zeros for a logged-out session."""

    def __init__(self, platform: str, detail: str = ""):
        self.platform = platform
        self.detail = detail
        super().__init__(f"session expired on {platform}"
                         + (f": {detail}" if detail else ""))


def _sleep(seconds: float):
    """Indirection so tests can patch out real sleeping."""
    _real_sleep(seconds)


def human_delay(min_s: float = 0.4, max_s: float = 1.4):
    _sleep(random.uniform(min_s, max_s))


def mouse_wiggle(page, moves: int = 3):
    """A few small human-ish mouse movements."""
    for _ in range(moves):
        page.mouse.move(random.randint(100, 900), random.randint(100, 600),
                        steps=random.randint(3, 10))
        _sleep(random.uniform(0.05, 0.2))


def type_with_plan(page, selector: str, text: str,
                   typing_plan: list[dict] | None = None,
                   wpm: tuple[int, int] = (45, 80)):
    """Type text into `selector` with human cadence, consuming the backend's
    anti-detect typing plan: ops shaped {"word", "typo", "word_index"} mean
    "at word N, type the typo, backspace it away, then type the real word"
    (see backend/app/antidetect.py build_typing_plan).
    """
    plan_by_index = {op["word_index"]: op for op in (typing_plan or [])}
    words = text.split(" ")
    page.click(selector)
    for i, word in enumerate(words):
        op = plan_by_index.get(i)
        if op:
            typo = op["typo"]
            page.keyboard.type(typo, delay=random.randint(40, 110))
            _sleep(random.uniform(0.3, 0.9))  # "notices" the mistake
            for _ in range(len(typo)):
                page.keyboard.press("Backspace")
                _sleep(random.uniform(0.03, 0.08))
        lo, hi = wpm
        delay_ms = int(60000 / (random.randint(lo, hi) * 5))  # 5 chars/word
        page.keyboard.type(word, delay=delay_ms)
        if i < len(words) - 1:
            page.keyboard.type(" ", delay=delay_ms)


def detect_challenge(page, platform: str) -> str | None:
    """Return the first matching challenge marker selector, or None."""
    for marker in challenge_markers(platform):
        try:
            if page.query_selector(marker):
                return marker
        except Exception as exc:  # noqa: BLE001 — selector engines may reject a marker
            log.debug("challenge marker %r check failed: %s", marker, exc)
    return None


def raise_if_challenge(page, platform: str):
    marker = detect_challenge(page, platform)
    if marker:
        raise CaptchaDetectedError(marker, platform)


def _parse_proxy(proxy_url: str, platform: str = "") -> dict:
    parsed = urlparse(proxy_url)
    if not parsed.scheme or not parsed.hostname or parsed.port is None:
        raise ValueError(
            f"invalid proxy URL for {platform or 'worker'}: {proxy_url!r} — "
            "expected scheme://[user:pass@]host:port with an explicit port "
            "(a missing port must not silently become ':None')")
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = parsed.username
        proxy["password"] = parsed.password or ""
    return proxy


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _mkdir_private(path: Path) -> Path:
    """Create a session dir (and parents) and lock it down: it holds browser
    profiles, storage_state and inbox screenshots — owner-only (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def apply_storage_state(context, state: dict):
    """Seed a browser context from a Playwright storage_state dict (cookies
    + per-origin localStorage), as enrolled via the Accounts UI."""
    cookies = state.get("cookies") or []
    if cookies:
        context.add_cookies(cookies)
    for origin in state.get("origins") or []:
        entries = [(e.get("name", ""), e.get("value", ""))
                   for e in origin.get("localStorage") or [] if e.get("name")]
        if not entries:
            continue
        _seed_local_storage(context, origin.get("origin", ""), entries)


def _seed_local_storage(context, origin: str, entries: list):
    """Write enrolled localStorage entries ONCE on the platform origin — the
    persistent profile (user_data_dir) retains them afterwards. A permanent
    add_init_script would instead re-apply the enrolled snapshot on every
    page load, stomping rotated CSRF/session tokens with weeks-old values."""
    page = context.new_page()
    try:
        page.goto(origin)
        items = json.dumps(entries)
        page.evaluate(
            f"(() => {{ for (const [k, v] of {items}) {{"
            " try { window.localStorage.setItem(k, v); } catch (e) {} } } })()")
    finally:
        page.close()


class BrowserManager:
    """Owns the Playwright runtime and one persistent context per
    (platform, user_id). Use as a context manager.

    `client` (a WorkerClient) is optional: when present, freshly launched
    contexts are seeded from the vault-enrolled storage_state served by the
    backend; without it (or when nothing is enrolled) the local persistent
    profile is used as before.
    """

    def __init__(self, config: Config, client=None):
        self.config = config
        self.client = client
        self._pw = None
        self._browser_type = None
        self._contexts: dict[tuple[str, int], object] = {}
        self._last_used: dict[tuple[str, int], float] = {}
        self._now = time.monotonic  # indirection so tests can fake time

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # lazy: needs browsers
        self._pw = sync_playwright().start()
        self._browser_type = self._pw.chromium
        return self

    def __exit__(self, *exc):
        for key in list(self._contexts):
            self.close_session(*key)
        if self._pw:
            self._pw.stop()

    def session_dir_for(self, platform: str, user_id: int) -> Path:
        return (self.config.session_dir / _safe_name(platform) / f"user_{user_id}")

    def get_context(self, platform: str, user_id: int):
        key = (platform, user_id)
        if key in self._contexts:
            self._last_used[key] = self._now()
            return self._contexts[key]
        user_data_dir = _mkdir_private(self.session_dir_for(platform, user_id))
        width, height = random.choice(VIEWPORTS)
        kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": self.config.headless,
            "user_agent": random.choice(USER_AGENTS),
            "viewport": {"width": width, "height": height},
            "locale": self.config.locale,
            "timezone_id": self.config.timezone,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        # fetch the enrolled session before launch: it may pin a per-account
        # proxy, which must be known when the context is created
        session = self._fetch_session(platform, user_id)
        proxy_url = session.get("proxy_url") or self.config.proxy_for(platform)
        if proxy_url:
            kwargs["proxy"] = _parse_proxy(proxy_url, platform)
            log.info("%s/user %s via proxy %s",
                     platform, user_id, proxy_url.split("@")[-1])
        context = self._browser_type.launch_persistent_context(**kwargs)
        self._seed_session(context, platform, user_id, session)
        self._contexts[key] = context
        self._last_used[key] = self._now()
        return context

    def reap_idle_contexts(self):
        """Close contexts idle beyond WORKER_CONTEXT_IDLE_SEC (flushing
        storage_state to disk as close_session does). The next get_context
        recreates the context fresh — which is also how a re-enrolled
        session takes effect without a worker restart."""
        ttl = self.config.context_idle_sec
        if ttl <= 0:
            return
        now = self._now()
        for platform, user_id in list(self._contexts):
            if now - self._last_used.get((platform, user_id), now) > ttl:
                log.info("closing idle %s/user %s context (>%ds unused)",
                         platform, user_id, ttl)
                self.close_session(platform, user_id)

    def _fetch_session(self, platform: str, user_id: int) -> dict:
        """Fetch the vault-enrolled stealth session from the backend; {} when
        there is no client, nothing is enrolled, or the backend is unreachable
        — never block task execution on seeding."""
        if self.client is None:
            return {}
        try:
            return self.client.get_stealth_session(platform, user_id) or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("could not fetch stealth session for %s/user %s (%s) — "
                        "falling back to the local profile", platform, user_id, exc)
            return {}

    def _seed_session(self, context, platform: str, user_id: int,
                      session: dict | None = None):
        """Prefer the vault-enrolled storage_state from the backend; fall back
        to the local persistent profile when absent or unreachable. `session`
        is the prefetched payload when the caller already fetched it (the
        per-account proxy is chosen before the context launches)."""
        if session is None:
            session = self._fetch_session(platform, user_id)
        state = session.get("storage_state")
        if not state:
            return
        try:
            apply_storage_state(context, state)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not apply enrolled storage_state for %s/user %s (%s) — "
                        "falling back to the local profile", platform, user_id, exc)
            return
        log.info("seeded %s session for user %s from enrolled credentials",
                 platform, user_id)

    def new_page(self, platform: str, user_id: int):
        context = self.get_context(platform, user_id)
        page = context.pages[0] if context.pages else context.new_page()
        return page

    def close_session(self, platform: str, user_id: int):
        context = self._contexts.pop((platform, user_id), None)
        self._last_used.pop((platform, user_id), None)
        if context is None:
            return
        try:
            # belt & suspenders alongside the persistent profile dir
            state_path = self.session_dir_for(platform, user_id) / "storage_state.json"
            context.storage_state(path=str(state_path))
            os.chmod(state_path, 0o600)  # session cookies — owner-only
        except Exception as exc:  # noqa: BLE001
            log.warning("could not save storage_state for %s/%s: %s",
                        platform, user_id, exc)
        context.close()

    def screenshot(self, page, platform: str, user_id: int, name: str) -> str:
        path = self.session_dir_for(platform, user_id) / f"{_safe_name(name)}.png"
        page.screenshot(path=str(path), full_page=True)
        os.chmod(path, 0o600)  # may show tenant inboxes/proposals — owner-only
        return str(path)
