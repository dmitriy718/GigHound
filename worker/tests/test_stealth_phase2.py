"""Phase-2 stealth truthfulness tests: submit-outcome verification on the
Upwork handler, session-expiry detection in fetch_page + the runner's
reporting branches, and the scrape handler's refusal to post fabricated
zeros. Browser and backend are fakes — no Playwright, no network."""
from types import SimpleNamespace

import pytest

from worker import runner
from worker.browser import SessionExpiredError
from worker.client import StealthTaskOut
from worker.config import Config
from worker.handlers import upwork_proposal
from worker.handlers.base import (HandlerContext, SelectorSuspectError,
                                  fetch_page)
from worker.handlers.scrape import handle_scrape_gig_metrics
from worker.platforms import platform_config


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr("worker.browser._real_sleep", lambda s: None)


class _FakeTask:
    def __init__(self, payload, user_id=7, platform="upwork", task_id=42):
        self.id = task_id
        self.user_id = user_id
        self.platform = platform
        self.payload = payload


class _FakePage:
    """Selector-driven page double: `markers` maps selector → element-ish
    object; anything else misses. wait_for_selector raises on a miss."""

    def __init__(self, markers=None, url="https://www.upwork.com/jobs/~abc"):
        self.markers = markers or {}
        self.url = url
        self.mouse = SimpleNamespace(move=lambda *a, **k: None)
        self.keyboard = SimpleNamespace(type=lambda *a, **k: None,
                                        press=lambda *a, **k: None)
        self.clicked = []

    def goto(self, url, wait_until=None):
        self.url = url

    def click(self, selector):
        self.clicked.append(selector)

    def wait_for_load_state(self, state):
        pass

    def query_selector(self, selector):
        return self.markers.get(selector)

    def wait_for_selector(self, selector, timeout=0):
        el = self.markers.get(selector)
        if el is None:
            raise TimeoutError(f"no element for {selector}")
        return el


class _FakeBrowser:
    def __init__(self, page=None):
        self.page = page
        self.shots = []

    def new_page(self, platform, user_id):
        return self.page

    def screenshot(self, page, platform, user_id, name):
        self.shots.append(name)
        return f"/shots/{name}.png"


class _FakeClient:
    def __init__(self):
        self.metrics = []

    def post_metrics(self, gig_id, impressions, clicks, orders, revenue):
        self.metrics.append((gig_id, impressions, clicks, orders, revenue))
        return {"id": 1}


def _ctx(browser, client=None):
    return HandlerContext(config=Config(worker_token="t"),
                          client=client or _FakeClient(), browser=browser)


# ---------------- upwork submit verification ----------------

def _submit(monkeypatch, markers):
    """Run the Upwork handler with the given post-submit page markers."""
    cfg = platform_config("upwork")
    form = cfg["proposal_form"]
    # the form fields must "exist" pre-submit so the flow reaches the click
    page_markers = {form["cover_letter"]: object(), **markers}
    page = _FakePage(markers=page_markers)
    monkeypatch.setattr("worker.handlers.upwork_proposal.fetch_page",
                        lambda ctx, platform, user_id, url: page)
    ctx = _ctx(_FakeBrowser(page))
    task = _FakeTask({"job_url": "https://www.upwork.com/jobs/~abc",
                      "job_external_id": "~abc", "proposal_text": "hello"})
    return upwork_proposal.handle_submit_upwork_proposal(task, ctx), page, ctx


def test_submit_confirmed_by_success_marker(monkeypatch):
    marker = platform_config("upwork")["submit_success"][0]
    result, _, ctx = _submit(monkeypatch, {marker: object()})
    assert result["submitted"] is True
    assert result["confirm_marker"] == marker
    assert result["job_external_id"] == "~abc"
    assert len(result["screenshots"]) == 2  # before + after
    assert ctx.browser.shots[0].endswith("before-submit")


def test_submit_rejected_by_failure_marker(monkeypatch):
    marker = platform_config("upwork")["submit_failure"][0]
    result, _, _ = _submit(monkeypatch, {marker: object()})
    assert result["submitted"] is False
    assert marker in result["reason"]
    assert len(result["screenshots"]) == 2


def test_submit_ambiguous_is_unverified_not_failed(monkeypatch):
    result, page, _ = _submit(monkeypatch, {})  # neither marker appears
    assert result["submitted"] is None
    assert result["state"] == "submitted_unverified"
    assert result["reason"]
    # the click DID happen — a failure here would invite a duplicate submit
    assert platform_config("upwork")["proposal_form"]["submit"] in page.clicked
    assert len(result["screenshots"]) == 2  # unverified case is screenshotted


def test_post_click_exception_degrades_to_unverified(monkeypatch):
    def boom(page, cfg):
        raise RuntimeError("page exploded")

    monkeypatch.setattr(upwork_proposal, "_verify_submission", boom)
    result, _, _ = _submit(monkeypatch, {})
    assert result["submitted"] is None
    assert result["state"] == "submitted_unverified"
    assert "page exploded" in result["reason"]


# ---------------- fetch_page session-expiry detection ----------------

def test_fetch_page_raises_on_login_redirect():
    page = _FakePage(url="https://www.upwork.com/ab/account-security/login")
    page.goto("https://www.upwork.com/ab/account-security/login?redir=/nx/")
    ctx = _ctx(_FakeBrowser(page))
    with pytest.raises(SessionExpiredError) as excinfo:
        fetch_page(ctx, "upwork", 7, "https://www.upwork.com/nx/proposals/")
    assert excinfo.value.platform == "upwork"


def test_fetch_page_raises_when_logged_in_marker_absent():
    page = _FakePage()  # no markers → logged_in_marker never appears
    ctx = _ctx(_FakeBrowser(page))
    with pytest.raises(SessionExpiredError) as excinfo:
        fetch_page(ctx, "upwork", 7, "https://www.upwork.com/nx/proposals/")
    assert excinfo.value.platform == "upwork"


def test_fetch_page_returns_on_live_session():
    marker = platform_config("upwork")["logged_in_marker"]
    page = _FakePage(markers={marker: object()})
    ctx = _ctx(_FakeBrowser(page))
    assert fetch_page(ctx, "upwork", 7, "https://x/jobs/~abc") is page


# ---------------- runner reporting branches ----------------

class _RunnerClient:
    def __init__(self):
        self.completed = []

    def claim_task(self, task_id):
        return StealthTaskOut(id=task_id, user_id=1, platform="fiverr",
                              task_type="scrape_gig_metrics", payload={})

    def complete_task(self, task_id, success, result):
        self.completed.append({"id": task_id, "success": success,
                               "result": result})
        return {"id": task_id, "status": "done" if success else "failed"}


def _runner_ctx(client):
    return HandlerContext(config=Config(worker_token="t"), client=client,
                          browser=object())


def test_runner_reports_session_expired(monkeypatch):
    client = _RunnerClient()

    def handler(task, ctx):
        raise SessionExpiredError("fiverr", "redirected to /login")

    monkeypatch.setattr(runner, "get_handler", lambda tt: handler)
    runner.process_task(StealthTaskOut(id=1, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"),
                        _runner_ctx(client))
    outcome = client.completed[0]
    assert outcome["success"] is False
    assert outcome["result"] == {"session_expired": True, "platform": "fiverr"}


def test_runner_reports_selector_suspect(monkeypatch):
    client = _RunnerClient()

    def handler(task, ctx):
        raise SelectorSuspectError("no metrics fields extracted")

    monkeypatch.setattr(runner, "get_handler", lambda tt: handler)
    runner.process_task(StealthTaskOut(id=2, user_id=1, platform="fiverr",
                                       task_type="scrape_gig_metrics"),
                        _runner_ctx(client))
    outcome = client.completed[0]
    assert outcome["success"] is False
    assert outcome["result"]["selector_suspect"] is True
    assert "no metrics fields" in outcome["result"]["error"]


# ---------------- scrape refuses fabricated zeros ----------------

def test_scrape_refuses_to_post_zeros(monkeypatch):
    fields = platform_config("fiverr")["metrics_fields"]

    class _EmptyPage(_FakePage):
        def query_selector(self, selector):
            return None  # every metric selector misses

    monkeypatch.setattr("worker.handlers.scrape.fetch_page",
                        lambda ctx, platform, user_id, url: _EmptyPage())
    client = _FakeClient()
    task = _FakeTask({"gigs": [{"id": 11, "url": "https://x/gig"}]},
                     platform="fiverr")
    with pytest.raises(SelectorSuspectError):
        handle_scrape_gig_metrics(task, _ctx(_FakeBrowser(), client))
    assert client.metrics == []  # zeros were NOT posted


def test_scrape_posts_extracted_metrics(monkeypatch):
    fields = platform_config("fiverr")["metrics_fields"]

    class _El:
        def __init__(self, text):
            self._text = text

        def inner_text(self):
            return self._text

    markers = {fields["impressions"]: _El("1,240"), fields["clicks"]: _El("37")}
    monkeypatch.setattr("worker.handlers.scrape.fetch_page",
                        lambda ctx, platform, user_id, url: _FakePage(markers))
    client = _FakeClient()
    task = _FakeTask({"gigs": [{"id": 11, "url": "https://x/gig"}]},
                     platform="fiverr")
    result = handle_scrape_gig_metrics(task, _ctx(_FakeBrowser(), client))
    assert client.metrics == [(11, 1240, 37, 0, 0.0)]
    assert result["scraped"][0]["gig_id"] == 11
