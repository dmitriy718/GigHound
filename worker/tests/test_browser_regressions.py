"""Local Chromium regression: no platform accounts or network requests."""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from worker.browser import type_with_plan


def test_unicode_proposal_text_survives_real_browser(monkeypatch):
    monkeypatch.setattr('worker.browser._sleep', lambda *_: None)
    with sync_playwright() as p:
        if not Path(p.chromium.executable_path).exists():
            pytest.skip('Install Playwright Chromium to run browser regressions')
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content('<textarea id="proposal"></textarea>')
            text = 'Hello — café 世界 😀\nSecond paragraph with $125.'
            type_with_plan(page, '#proposal', text, [])
            assert page.locator('#proposal').input_value() == text
        finally:
            browser.close()


def test_task_cleanup_erases_credentials_and_screenshots(tmp_path):
    from worker.browser import BrowserManager
    from worker.config import Config
    manager = BrowserManager(Config(session_dir=tmp_path))
    directory = manager.session_dir_for('upwork', 7)
    directory.mkdir(parents=True)
    (directory / 'fingerprint.json').write_text('{}')
    (directory / 'screenshot.png').write_bytes(b'private screenshot')
    (directory / 'Default').mkdir()
    (directory / 'Default' / 'Cookies').write_text('private cookies')
    manager.purge_session('upwork', 7)
    assert [entry.name for entry in directory.iterdir()] == ['fingerprint.json']


def test_cleanup_refuses_live_browser_profile(tmp_path):
    import os
    from worker.browser import BrowserManager
    from worker.config import Config
    manager = BrowserManager(Config(session_dir=tmp_path))
    directory = manager.session_dir_for('upwork',7); directory.mkdir(parents=True)
    (directory/'SingletonLock').symlink_to(f'synthetic-host-{os.getpid()}')
    (directory/'Cookies').write_text('must not erase live profile')
    with pytest.raises(RuntimeError, match='live process'):
        manager.purge_session('upwork',7)
    assert (directory/'Cookies').exists()
