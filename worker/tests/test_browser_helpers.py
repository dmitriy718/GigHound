"""Browser-helper tests with a fake page: typing-plan consumption and
challenge detection. No Playwright import, no browser."""
from pathlib import Path

import pytest

from worker import browser
from worker.browser import (CaptchaDetectedError, detect_challenge,
                            raise_if_challenge, type_with_plan, _parse_proxy)


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    def type(self, text, delay=0):
        self.page.events.append(("type", text))

    def press(self, key):
        self.page.events.append(("press", key))


class FakeMouse:
    def __init__(self, page):
        self.page = page

    def move(self, x, y, steps=1):
        self.page.events.append(("move", x, y))


class FakePage:
    def __init__(self, markers=()):
        self.events = []
        self.keyboard = FakeKeyboard(self)
        self.mouse = FakeMouse(self)
        self.markers = set(markers)
        self.navigated = []
        self.evaluated = []
        self.closed = False

    def click(self, selector):
        self.events.append(("click", selector))

    def query_selector(self, selector):
        return object() if selector in self.markers else None

    def goto(self, url):
        self.navigated.append(url)

    def evaluate(self, script):
        self.evaluated.append(script)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(browser, "_sleep", lambda *_: None)


def typed_text(page):
    return "".join(text for kind, text in page.events if kind == "type")


def test_typing_plan_typo_correction():
    page = FakePage()
    text = "the project is yours"
    plan = [{"word": "the", "typo": "teh", "word_index": 0}]
    type_with_plan(page, "#field", text, plan)

    assert page.events[0] == ("click", "#field")
    types = [e for e in page.events if e[0] == "type"]
    # typo typed first, then the real word
    assert types[0] == ("type", "teh")
    assert ("type", "the") in types
    # one backspace per typo char, before the correction
    teh_idx = page.events.index(("type", "teh"))
    the_idx = page.events.index(("type", "the"))
    backspaces = [e for e in page.events[teh_idx:the_idx] if e == ("press", "Backspace")]
    assert len(backspaces) == 3
    # final rendered text is intact (typo chars are erased by backspaces)
    assert typed_text(page).replace("teh", "", 1) == "the project is yours"


def test_typing_without_plan_types_verbatim():
    page = FakePage()
    type_with_plan(page, "#field", "hello there world", [])
    assert typed_text(page) == "hello there world"


def test_detect_challenge_markers():
    page = FakePage(markers={"#challenge-running"})
    assert detect_challenge(page, "fiverr") == "#challenge-running"
    with pytest.raises(CaptchaDetectedError) as exc:
        raise_if_challenge(page, "fiverr")
    assert exc.value.marker == "#challenge-running"
    assert exc.value.platform == "fiverr"


def test_detect_challenge_clean_page():
    assert detect_challenge(FakePage(), "upwork") is None


def test_parse_proxy_with_credentials():
    proxy = _parse_proxy("http://user:pass@proxy.example:8080")
    assert proxy == {"server": "http://proxy.example:8080",
                     "username": "user", "password": "pass"}


def test_parse_proxy_without_credentials():
    assert _parse_proxy("http://proxy.example:8080") == {
        "server": "http://proxy.example:8080"}


def test_parse_proxy_requires_explicit_port():
    with pytest.raises(ValueError) as exc:
        _parse_proxy("http://user:pass@proxy.example", "upwork")
    assert "upwork" in str(exc.value)  # names the offending platform
    assert "port" in str(exc.value)


# ---------------- API session seeding ----------------

class FakeContext:
    def __init__(self):
        self.cookies = []
        self.init_scripts = []
        self.pages_made = []

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def new_page(self):
        page = FakePage()
        self.pages_made.append(page)
        return page


class FakeBrowserType:
    def __init__(self, context=None):
        self.context = context or FakeContext()
        self.kwargs = None

    def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class FakeClient:
    def __init__(self, session=None, raises=False):
        self.session = session
        self.raises = raises
        self.calls = []

    def get_stealth_session(self, platform, user_id):
        self.calls.append((platform, user_id))
        if self.raises:
            raise RuntimeError("backend down")
        return self.session


def _manager(client):
    from worker.browser import BrowserManager
    from worker.config import Config
    return BrowserManager(Config(session_dir=Path("/tmp/gh-test-sessions")),
                          client=client)


def test_seed_session_applies_enrolled_storage_state():
    from worker.browser import apply_storage_state
    state = {
        "cookies": [{"name": "session", "value": "s3cret", "domain": ".fiverr.com",
                     "path": "/", "expires": -1, "httpOnly": True,
                     "secure": True, "sameSite": "Lax"}],
        "origins": [{"origin": "https://www.fiverr.com",
                     "localStorage": [{"name": "k", "value": "v"}]}],
    }
    ctx = FakeContext()
    apply_storage_state(ctx, state)
    assert ctx.cookies == state["cookies"]
    # localStorage is written ONCE on the origin (the persistent profile
    # retains it) — no init script re-applying stale values on every load
    assert ctx.init_scripts == []
    assert len(ctx.pages_made) == 1
    page = ctx.pages_made[0]
    assert page.navigated == ["https://www.fiverr.com"]
    assert len(page.evaluated) == 1
    assert "localStorage.setItem" in page.evaluated[0]
    assert '["k", "v"]' in page.evaluated[0]
    assert page.closed

    client = FakeClient({"storage_state": state, "credentials_present": True})
    mgr = _manager(client)
    ctx2 = FakeContext()
    mgr._seed_session(ctx2, "fiverr", 7)
    assert client.calls == [("fiverr", 7)]
    assert ctx2.cookies == state["cookies"]


def test_seed_session_falls_back_when_absent_or_unreachable():
    # nothing enrolled → no seeding, no error
    ctx = FakeContext()
    _manager(FakeClient({"storage_state": None,
                         "credentials_present": False}))._seed_session(ctx, "guru", 1)
    assert ctx.cookies == [] and ctx.init_scripts == []

    # backend unreachable → fall back to the local profile, never raise
    ctx2 = FakeContext()
    _manager(FakeClient(raises=True))._seed_session(ctx2, "guru", 1)
    assert ctx2.cookies == [] and ctx2.init_scripts == []

    # no client at all (e.g. worker.login) → no-op
    _manager(None)._seed_session(FakeContext(), "guru", 1)


# ---------------- per-account proxy selection ----------------

def _launch_kwargs(monkeypatch, tmp_path, session, platform="upwork",
                   platform_proxy=None):
    """Run get_context with a fake browser type; return its launch kwargs."""
    from worker.browser import BrowserManager
    from worker.config import Config
    if platform_proxy:
        monkeypatch.setenv(f"WORKER_PROXY_{platform.upper()}", platform_proxy)
    mgr = BrowserManager(Config(session_dir=tmp_path),
                         client=FakeClient(session))
    browser_type = FakeBrowserType()
    mgr._browser_type = browser_type
    assert mgr.get_context(platform, 7) is browser_type.context
    return browser_type.kwargs


def test_per_account_proxy_wins_over_platform_default(monkeypatch, tmp_path):
    kwargs = _launch_kwargs(
        monkeypatch, tmp_path,
        {"storage_state": None, "credentials_present": True,
         "proxy_url": "http://user:pw@tenant-proxy:9000"},
        platform_proxy="http://platform-proxy:8080")
    assert kwargs["proxy"] == {"server": "http://tenant-proxy:9000",
                               "username": "user", "password": "pw"}


def test_platform_proxy_fallback_when_account_has_none(monkeypatch, tmp_path):
    kwargs = _launch_kwargs(
        monkeypatch, tmp_path,
        {"storage_state": None, "credentials_present": True, "proxy_url": None},
        platform_proxy="http://platform-proxy:8080")
    assert kwargs["proxy"] == {"server": "http://platform-proxy:8080"}


def test_no_proxy_configured_launches_direct(monkeypatch, tmp_path):
    monkeypatch.delenv("WORKER_PROXY_UPWORK", raising=False)
    kwargs = _launch_kwargs(
        monkeypatch, tmp_path,
        {"storage_state": None, "credentials_present": False, "proxy_url": None})
    assert "proxy" not in kwargs


def test_port_less_proxy_fails_fast(monkeypatch, tmp_path):
    from worker.browser import BrowserManager
    from worker.config import Config
    monkeypatch.setenv("WORKER_PROXY_UPWORK", "http://proxy.example")
    mgr = BrowserManager(Config(session_dir=tmp_path), client=None)
    mgr._browser_type = FakeBrowserType()
    with pytest.raises(ValueError, match="upwork"):
        mgr.get_context("upwork", 7)


# ---------------- idle context reaper ----------------

class FakeStorageContext:
    def __init__(self):
        self.closed = False

    def storage_state(self, path):
        Path(path).write_text("{}")

    def close(self):
        self.closed = True


def test_reap_idle_contexts_closes_stale_keeps_fresh(tmp_path):
    from worker.browser import BrowserManager
    from worker.config import Config
    mgr = BrowserManager(Config(session_dir=tmp_path, context_idle_sec=1800),
                         client=None)
    now = [10_000.0]
    mgr._now = lambda: now[0]
    stale, fresh = FakeStorageContext(), FakeStorageContext()
    mgr.session_dir_for("upwork", 1).mkdir(parents=True)
    mgr._contexts[("upwork", 1)] = stale
    mgr._last_used[("upwork", 1)] = now[0] - 1900  # past the TTL
    mgr._contexts[("fiverr", 2)] = fresh
    mgr._last_used[("fiverr", 2)] = now[0] - 60

    mgr.reap_idle_contexts()

    assert stale.closed and ("upwork", 1) not in mgr._contexts
    assert ("upwork", 1) not in mgr._last_used
    # closing flushed the storage_state to disk, owner-only
    assert _mode(tmp_path / "upwork" / "user_1" / "storage_state.json") == 0o600
    assert not fresh.closed and mgr._contexts[("fiverr", 2)] is fresh


def test_reap_disabled_with_nonpositive_ttl(tmp_path):
    from worker.browser import BrowserManager
    from worker.config import Config
    mgr = BrowserManager(Config(session_dir=tmp_path, context_idle_sec=0),
                         client=None)
    mgr._contexts[("guru", 1)] = FakeStorageContext()
    mgr._last_used[("guru", 1)] = 0.0  # ancient — would always be stale
    mgr.reap_idle_contexts()
    assert ("guru", 1) in mgr._contexts


# ---------------- session-dir / artifact permissions ----------------

def _mode(path):
    import os
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


def test_mkdir_private_creates_owner_only_dirs(tmp_path):
    from worker.browser import _mkdir_private
    d = _mkdir_private(tmp_path / "platform" / "user_3")
    assert d.is_dir() and _mode(d) == 0o700
    # re-running tightens a pre-existing loose dir instead of leaving it
    import os
    os.chmod(d, 0o755)
    _mkdir_private(d)
    assert _mode(d) == 0o700


def test_storage_state_saved_owner_only(tmp_path):
    from worker.browser import BrowserManager
    from worker.config import Config

    class FakeStorageContext:
        def storage_state(self, path):
            Path(path).write_text("{}")

        def close(self):
            pass

    mgr = BrowserManager(Config(session_dir=tmp_path), client=None)
    mgr.session_dir_for("upwork", 1).mkdir(parents=True)
    mgr._contexts[("upwork", 1)] = FakeStorageContext()
    mgr.close_session("upwork", 1)
    assert _mode(tmp_path / "upwork" / "user_1" / "storage_state.json") == 0o600


def test_screenshot_saved_owner_only(tmp_path):
    from worker.browser import BrowserManager
    from worker.config import Config

    class FakeShotPage:
        def screenshot(self, path, full_page=False):
            Path(path).write_bytes(b"png")

    mgr = BrowserManager(Config(session_dir=tmp_path), client=None)
    mgr.session_dir_for("guru", 2).mkdir(parents=True)
    shot = mgr.screenshot(FakeShotPage(), "guru", 2, "task9-manual-assist")
    assert _mode(Path(shot)) == 0o600
