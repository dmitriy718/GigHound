"""Fingerprint coherence tests: the per-(platform, user) bundle is generated
once, persisted owner-only, and reused on later launches; the UA and
Sec-CH-UA agree with the real bundled Chromium; session-provided
timezone/locale overrides win at generation time; the stealth init script is
registered exactly once per context. Browser is a fake — no Playwright."""
import json
import os
import re
import stat

from worker.browser import BrowserManager, build_fingerprint


class _FakeContext:
    def __init__(self):
        self.init_scripts = []

    def add_init_script(self, script):
        self.init_scripts.append(script)


class _FakeBrowserType:
    def __init__(self):
        self.context = _FakeContext()
        self.kwargs = None

    def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class _FakeClient:
    def __init__(self, session=None):
        self.session = session or {}

    def get_stealth_session(self, platform, user_id):
        return self.session


def _manager(tmp_path, session=None):
    from worker.config import Config
    mgr = BrowserManager(Config(session_dir=tmp_path), client=_FakeClient(session))
    mgr._browser_type = _FakeBrowserType()
    return mgr


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _fp_path(tmp_path, platform="upwork", user_id=7):
    return tmp_path / platform / f"user_{user_id}" / "fingerprint.json"


def test_fingerprint_generated_once_persisted_and_reused(tmp_path):
    mgr = _manager(tmp_path)
    mgr.get_context("upwork", 7)
    first = dict(mgr._browser_type.kwargs)
    fp_path = _fp_path(tmp_path)
    assert fp_path.exists() and _mode(fp_path) == 0o600

    # a later launch (fresh manager, same session dir) reuses the bundle
    mgr2 = _manager(tmp_path)
    mgr2.get_context("upwork", 7)
    second = mgr2._browser_type.kwargs
    assert second["user_agent"] == first["user_agent"]
    assert second["viewport"] == first["viewport"]
    assert json.loads(fp_path.read_text())["user_agent"] == first["user_agent"]


def test_fingerprint_ua_matches_sec_ch_ua(tmp_path):
    mgr = _manager(tmp_path)
    mgr.get_context("upwork", 7)
    kwargs = mgr._browser_type.kwargs
    fp = json.loads(_fp_path(tmp_path).read_text())
    ua_major = re.search(r"Chrome/(\d+)\.", kwargs["user_agent"]).group(1)
    assert kwargs["user_agent"] == fp["user_agent"]
    # the bundle's UA major agrees with its Sec-CH-UA fullVersion/brands
    assert fp["sec_ch_ua_full_version"].split(".")[0] == ua_major
    chromium = next(b for b in fp["sec_ch_ua"] if b["brand"] == "Chromium")
    assert chromium["version"] == ua_major


def test_build_fingerprint_tracks_real_bundled_chromium():
    fp = build_fingerprint("America/New_York", "en-US")
    ua_major = re.search(r"Chrome/(\d+)\.", fp["user_agent"]).group(1)
    assert ua_major == fp["sec_ch_ua_full_version"].split(".")[0]
    # derived from the actual bundled build — not the stale 124-126 list
    assert int(ua_major) >= 130


def test_session_timezone_locale_override_wins(tmp_path):
    session = {"timezone": "Europe/Berlin", "locale": "de-DE",
               "storage_state": None, "credentials_present": False}
    mgr = _manager(tmp_path, session)
    mgr.get_context("fiverr", 3)
    kwargs = mgr._browser_type.kwargs
    assert kwargs["timezone_id"] == "Europe/Berlin"
    assert kwargs["locale"] == "de-DE"


def test_config_defaults_when_session_has_no_geo(tmp_path):
    mgr = _manager(tmp_path, {"storage_state": None, "credentials_present": False})
    mgr.get_context("fiverr", 3)
    kwargs = mgr._browser_type.kwargs
    assert kwargs["timezone_id"] == mgr.config.timezone
    assert kwargs["locale"] == mgr.config.locale


def test_stealth_init_script_registered_once_per_context(tmp_path):
    mgr = _manager(tmp_path)
    context = mgr.get_context("upwork", 7)
    assert len(context.init_scripts) == 1
    script = context.init_scripts[0]
    assert "webdriver" in script
    assert "37445" in script and "37446" in script  # WebGL vendor/renderer
    assert "userAgentData" in script
    # cached-context path registers nothing again
    assert mgr.get_context("upwork", 7) is context
    assert len(context.init_scripts) == 1
