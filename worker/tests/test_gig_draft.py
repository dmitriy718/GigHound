"""create_gig_draft tests: the save-as-draft click is gated on enough of the
form actually filling — a drifted form must fail loudly with the missed
selectors instead of saving a half-empty draft on the tenant's account."""
from types import SimpleNamespace

import pytest

from worker.config import Config
from worker.handlers.base import HandlerContext
from worker.handlers.gig_draft import handle_create_gig_draft
from worker.platforms import platform_config

FORM = platform_config("fiverr")["gig_form"]

TEMPLATE = {"title": "Minimal logo design", "category": "Graphics",
            "tags": ["logo", "branding"], "price": 50,
            "description": "I will design a minimal logo for your brand."}


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr("worker.browser._real_sleep", lambda s: None)


class _El:
    def __init__(self):
        self.values = []

    def fill(self, value):
        self.values.append(value)


class _FakePage:
    """Selector-driven page double: only the `present` selectors match."""

    def __init__(self, present):
        self.els = {selector: _El() for selector in present}
        self.clicked = []
        self.keyboard = SimpleNamespace(press=lambda *a, **k: None,
                                        type=lambda *a, **k: None)

    def query_selector(self, selector):
        return self.els.get(selector)

    def click(self, selector):
        self.clicked.append(selector)

    def wait_for_load_state(self, state):
        pass


class _FakeBrowser:
    def screenshot(self, page, platform, user_id, name):
        return f"/shots/{name}.png"


class _FakeTask:
    id = 42
    user_id = 7
    platform = "fiverr"

    def __init__(self, payload):
        self.payload = payload


def _run(monkeypatch, page, template=TEMPLATE):
    monkeypatch.setattr("worker.handlers.gig_draft.fetch_page",
                        lambda ctx, platform, user_id, url: page)
    ctx = HandlerContext(config=Config(worker_token="t"),
                         client=None, browser=_FakeBrowser())
    task = _FakeTask({"template_id": 3, "template": template,
                      "typing_plan": []})
    return handle_create_gig_draft(task, ctx)


def test_full_form_saves_draft_and_reports_fills(monkeypatch):
    page = _FakePage(present=[FORM["title"], FORM["category"], FORM["tags"],
                              FORM["price"], FORM["description"]])
    result = _run(monkeypatch, page)
    assert FORM["save_draft"] in page.clicked
    assert result["draft"] is True and result["published"] is False
    assert sorted(result["filled"]) == ["category", "description", "price",
                                        "tags", "title"]
    assert result["missed"] == []
    assert page.els[FORM["title"]].values == [TEMPLATE["title"]]


def test_drifted_form_aborts_save_with_missed_selectors(monkeypatch):
    page = _FakePage(present=[FORM["title"], FORM["price"]])  # 2/5 fields
    with pytest.raises(RuntimeError) as excinfo:
        _run(monkeypatch, page)
    assert FORM["save_draft"] not in page.clicked  # no half-empty draft saved
    msg = str(excinfo.value)
    assert "2/5" in msg
    for field in ("category", "tags", "description"):
        assert field in msg  # the drift report is actionable


def test_majority_filled_still_saves_and_lists_misses(monkeypatch):
    page = _FakePage(present=[FORM["title"], FORM["category"],
                              FORM["price"]])  # 3/5 — at the floor
    result = _run(monkeypatch, page)
    assert FORM["save_draft"] in page.clicked
    assert sorted(result["missed"]) == ["description", "tags"]
