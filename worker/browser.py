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

VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080)]

# fallback Chromium build when the playwright package's own metadata can't be
# read — pinned to the playwright 1.62 bundled browser
_FALLBACK_CHROMIUM_VERSION = "151.0.7922.34"

# coherent desktop OS profiles: UA platform token, Sec-CH-UA platform, and a
# WebGL vendor/renderer that plausibly belongs to that OS (a GPU-less
# container reports SwiftShader — a loud datacenter tell)
_OS_PROFILES = [
    {"ua_os": "Windows NT 10.0; Win64; x64", "ch_ua_platform": "Windows",
     "webgl_vendor": "Google Inc. (Intel)",
     "webgl_renderer": ("ANGLE (Intel, Intel(R) UHD Graphics 630 "
                        "Direct3D11 vs_5_0 ps_5_0, D3D11)")},
    {"ua_os": "Macintosh; Intel Mac OS X 10_15_7", "ch_ua_platform": "macOS",
     "webgl_vendor": "Google Inc. (Intel)",
     "webgl_renderer": ("ANGLE (Intel Inc., Intel(R) UHD Graphics 630, "
                        "OpenGL 4.5)")},
    {"ua_os": "X11; Linux x86_64", "ch_ua_platform": "Linux",
     "webgl_vendor": "Google Inc. (Intel)",
     "webgl_renderer": ("ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 "
                        "(CFL GT2), OpenGL 4.5)")},
]


def _detect_chromium_version() -> str:
    """Full version of the Chromium playwright actually bundles (from the
    package's browsers.json), so the UA string and Sec-CH-UA never contradict
    the real engine build."""
    try:
        import playwright  # lazy, like sync_playwright — needs the package
        browsers = json.loads((Path(playwright.__file__).parent
                               / "driver" / "package" / "browsers.json").read_text())
        for entry in browsers.get("browsers", []):
            if entry.get("name") == "chromium" and entry.get("browserVersion"):
                return entry["browserVersion"]
    except Exception as exc:  # noqa: BLE001
        log.debug("chromium version detection failed: %s", exc)
    return _FALLBACK_CHROMIUM_VERSION


def build_fingerprint(timezone: str, locale: str) -> dict:
    """One coherent per-account fingerprint bundle: UA derived from the real
    bundled Chromium (userAgentData would leak it otherwise), matching
    Sec-CH-UA brands, viewport, WebGL strings, timezone and locale. Generated
    once per (platform, user_id) and persisted — accounts don't change
    browsers every few hours."""
    version = _detect_chromium_version()
    major = version.split(".")[0]
    os_profile = random.choice(_OS_PROFILES)
    width, height = random.choice(VIEWPORTS)
    return {
        "user_agent": (f"Mozilla/5.0 ({os_profile['ua_os']}) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       f"Chrome/{version} Safari/537.36"),
        "sec_ch_ua": [
            {"brand": "Not(A:Brand", "version": "24"},
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
        ],
        "sec_ch_ua_full_version": version,
        "sec_ch_ua_platform": os_profile["ch_ua_platform"],
        "viewport": {"width": width, "height": height},
        "webgl_vendor": os_profile["webgl_vendor"],
        "webgl_renderer": os_profile["webgl_renderer"],
        "timezone": timezone,
        "locale": locale,
    }


_STEALTH_INIT_BODY = """
  try { Object.defineProperty(navigator, 'webdriver',
        { get: () => undefined }); } catch (e) {}
  try {
    const langs = [fp.locale, fp.locale.split('-')[0]];
    Object.defineProperty(navigator, 'languages', { get: () => langs });
    Object.defineProperty(navigator, 'language', { get: () => fp.locale });
  } catch (e) {}
  try {
    const protos = [window.WebGLRenderingContext && WebGLRenderingContext.prototype,
                    window.WebGL2RenderingContext && WebGL2RenderingContext.prototype];
    for (const proto of protos) {
      if (!proto) continue;
      const orig = proto.getParameter;
      proto.getParameter = function (pname) {
        if (pname === 37445) return fp.webgl_vendor;    // UNMASKED_VENDOR_WEBGL
        if (pname === 37446) return fp.webgl_renderer;  // UNMASKED_RENDERER_WEBGL
        return orig.call(this, pname);
      };
    }
  } catch (e) {}
  try { Object.defineProperty(navigator, 'plugins',
        { get: () => [0, 1, 2] }); } catch (e) {}  // non-empty PluginArray shape
  try {
    if (navigator.userAgentData) {
      const brands = fp.sec_ch_ua;
      const full = fp.sec_ch_ua_full_version;
      const fullVersionList = brands.map(b => ({
        brand: b.brand, version: /Chrom/.test(b.brand) ? full : b.version + '.0.0.0' }));
      const data = {
        brands, mobile: false, platform: fp.sec_ch_ua_platform,
        getHighEntropyValues: () => Promise.resolve({
          brands, fullVersionList, platform: fp.sec_ch_ua_platform,
          uaFullVersion: full, mobile: false, model: '',
          architecture: 'x86', bitness: '64' }),
        toJSON: () => ({ brands, mobile: false, platform: fp.sec_ch_ua_platform }),
      };
      Object.defineProperty(navigator, 'userAgentData', { get: () => data });
    }
  } catch (e) {}
})();"""


def _stealth_init_script(fp: dict) -> str:
    """Permanent anti-detect init script built from the fingerprint bundle.
    Each patch is individually try/catch-guarded so one failure never breaks
    page loads."""
    payload = json.dumps({
        "locale": fp["locale"],
        "webgl_vendor": fp["webgl_vendor"],
        "webgl_renderer": fp["webgl_renderer"],
        "sec_ch_ua": fp["sec_ch_ua"],
        "sec_ch_ua_full_version": fp["sec_ch_ua_full_version"],
        "sec_ch_ua_platform": fp["sec_ch_ua_platform"],
    })
    return "(() => {\n  const fp = " + payload + ";" + _STEALTH_INIT_BODY


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


class TaskTimeoutError(Exception):
    """The task blew its wall-clock budget (WORKER_TASK_TIMEOUT_SEC). Raised
    from pacing checkpoints — see arm_task_deadline."""


# Cooperative per-task deadline. A hard cancel would mean running the handler
# on a worker thread, but the Playwright sync API is thread-affine: its
# greenlet event loop is bound to the thread where sync_playwright() started
# (main()), and any call from another thread dies with greenlet.error
# ("Cannot switch to a different thread"). So instead every pacing checkpoint
# (_sleep backs human_delay, type_with_plan's per-char cadence, mouse_wiggle —
# the places a task actually burns wall-clock) checks the deadline and bails.
# A wedge inside a single Playwright call is still bounded by Playwright's own
# navigation/selector timeouts.
_TASK_DEADLINE: float | None = None


def arm_task_deadline(timeout_sec: float):
    """Start the per-task wall-clock budget; <= 0 disables enforcement."""
    global _TASK_DEADLINE
    _TASK_DEADLINE = time.monotonic() + timeout_sec if timeout_sec > 0 else None


def disarm_task_deadline():
    global _TASK_DEADLINE
    _TASK_DEADLINE = None


def _sleep(seconds: float):
    """Indirection so tests can patch out real sleeping. Also the pacing
    checkpoint where the per-task wall-clock budget is enforced."""
    if _TASK_DEADLINE is not None and time.monotonic() > _TASK_DEADLINE:
        raise TaskTimeoutError("task exceeded WORKER_TASK_TIMEOUT_SEC")
    _real_sleep(seconds)


def human_delay(min_s: float = 0.4, max_s: float = 1.4):
    _sleep(random.uniform(min_s, max_s))


def mouse_wiggle(page, moves: int = 3):
    """A few small human-ish mouse movements."""
    for _ in range(moves):
        page.mouse.move(random.randint(100, 900), random.randint(100, 600),
                        steps=random.randint(3, 10))
        _sleep(random.uniform(0.05, 0.2))


def _type_chars(page, text: str, base_delay_ms: float):
    """Type `text` char-by-char with human cadence: the wpm base jittered
    ±40%, occasional 'thinking' pauses, a longer beat after punctuation, and
    rare double-strike bursts. A constant inter-keystroke cadence is itself
    a bot signature. Per-char delay is capped to keep runtime sane."""
    i = 0
    while i < len(text):
        # rare burst: two chars fired off inside one short interval
        burst = 2 if i + 1 < len(text) and random.random() < 0.06 else 1
        for ch in text[i:i + burst]:
            page.keyboard.press("Space" if ch == " " else ch)
            delay_ms = min(350.0, base_delay_ms * random.uniform(0.6, 1.4))
            if ch in ".,;:!?":
                delay_ms += random.uniform(100, 300)
            _sleep(delay_ms / 1000)
            if random.random() < 0.05:  # "thinking" pause
                _sleep(random.uniform(0.2, 0.6))
        i += burst


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
        base_ms = 60000 / (random.randint(lo, hi) * 5)  # 5 chars/word
        _type_chars(page, word, base_ms)
        if i < len(words) - 1:
            _type_chars(page, " ", base_ms)


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
        self._warn_if_profile_locked(platform, user_id, user_data_dir)
        # fetch the enrolled session before launch: it may pin a per-account
        # proxy and per-account fingerprint geo (timezone/locale), both of
        # which must be known when the context is created
        session = self._fetch_session(platform, user_id)
        fp = self._fingerprint_for(platform, user_id, session)
        kwargs = {
            "user_data_dir": str(user_data_dir),
            "headless": self.config.headless,
            "user_agent": fp["user_agent"],
            "viewport": fp["viewport"],
            "locale": fp["locale"],
            "timezone_id": fp["timezone"],
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        proxy_url = session.get("proxy_url") or self.config.proxy_for(platform)
        if proxy_url:
            kwargs["proxy"] = _parse_proxy(proxy_url, platform)
            log.info("%s/user %s via proxy %s",
                     platform, user_id, proxy_url.split("@")[-1])
        context = self._browser_type.launch_persistent_context(**kwargs)
        context.add_init_script(_stealth_init_script(fp))
        self._seed_session(context, platform, user_id, session)
        self._contexts[key] = context
        self._last_used[key] = self._now()
        return context

    def _fingerprint_for(self, platform: str, user_id: int, session: dict) -> dict:
        """Load the persisted fingerprint bundle for this (platform, user),
        generating + persisting it (owner-only, like storage_state) on first
        use. Session-provided timezone/locale (per-account geo, aligned with
        the proxy exit) win over the config defaults — but only at generation
        time; afterwards the bundle is stable across launches."""
        path = self.session_dir_for(platform, user_id) / "fingerprint.json"
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            log.warning("unreadable fingerprint for %s/user %s (%s) — regenerating",
                        platform, user_id, exc)
        fp = build_fingerprint(timezone=session.get("timezone") or self.config.timezone,
                               locale=session.get("locale") or self.config.locale)
        path.write_text(json.dumps(fp, indent=2))
        os.chmod(path, 0o600)  # sits beside session cookies — owner-only
        return fp

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
        """A FRESH page on the (platform, user) context — never a reused one:
        a crashed task's leftover DOM state (open modal, half-filled form)
        must not leak into the next task. Pages accumulate on the context
        until the runner calls close_pages after each task."""
        return self.get_context(platform, user_id).new_page()

    def close_pages(self, platform: str, user_id: int):
        """Close every page on the (platform, user) context (task hygiene:
        called by the runner after each task). Best-effort per page."""
        context = self._contexts.get((platform, user_id))
        if context is None:
            return
        for page in list(context.pages):
            try:
                page.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("page close failed for %s/user %s: %s",
                          platform, user_id, exc)

    def _warn_if_profile_locked(self, platform: str, user_id: int,
                                user_data_dir: Path):
        """Loud warning when the profile dir looks claimed by a LIVE Chromium
        — the signature of two workers sharing one worker_sessions volume
        (profile-lock corruption + one account active from two browsers).
        Warning-only: a stale SingletonLock after a crash is normal and
        Chromium cleans it up itself."""
        lock = user_data_dir / "SingletonLock"
        try:
            pid = int(lock.readlink().rsplit("-", 1)[-1])
        except (ValueError, OSError):
            return  # no lock (or not a Chromium symlink) — nothing to check
        try:
            os.kill(pid, 0)  # raises ProcessLookupError when the holder is dead
        except ProcessLookupError:
            return  # stale lock after a crash — Chromium cleans it up itself
        except PermissionError:
            pass  # alive, owned by another user — still a live holder
        log.warning("%s/user %s profile %s is locked by LIVE pid %d — is a "
                    "second worker sharing this worker_sessions volume? Two "
                    "Chromiums on one user_data_dir corrupt the profile and "
                    "put one account in two browsers at once",
                    platform, user_id, user_data_dir, pid)

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
