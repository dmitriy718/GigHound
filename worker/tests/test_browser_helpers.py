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

    def click(self, selector):
        self.events.append(("click", selector))

    def query_selector(self, selector):
        return object() if selector in self.markers else None


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


# ---------------- API session seeding ----------------

class FakeContext:
    def __init__(self):
        self.cookies = []
        self.init_scripts = []

    def add_cookies(self, cookies):
        self.cookies.extend(cookies)

    def add_init_script(self, script):
        self.init_scripts.append(script)


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
    assert len(ctx.init_scripts) == 1
    assert "https://www.fiverr.com" in ctx.init_scripts[0]
    assert "localStorage.setItem" in ctx.init_scripts[0]

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
