"""scrape_proposal_status handler tests: canonical status mapping, page
extraction → backend posting, and the client post. Browser and backend are
fakes — no Playwright, no network beyond httpx.MockTransport."""
import httpx
import pytest

from worker.client import WorkerClient
from worker.config import Config
from worker.handlers import get_handler
from worker.handlers.base import HandlerContext
from worker.handlers.proposal_status import (canonical_status,
                                             handle_scrape_proposal_status)
from worker.platforms import platform_config


class _FakeTask:
    def __init__(self, items, task_id=5, user_id=7, platform="upwork"):
        self.id = task_id
        self.user_id = user_id
        self.platform = platform
        self.payload = {"items": items}


class _FakeEl:
    def __init__(self, text="", href=None):
        self._text = text
        self._href = href

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._href if name == "href" else None


class _FakeCard:
    """A proposal card: visible text, a /jobs/ link, optional status node and
    unread badge."""

    def __init__(self, text, href=None, status=None, unread=False):
        self._text = text
        self._href = href
        self._status = status
        self._unread = unread

    def inner_text(self):
        return self._text

    def query_selector_all(self, selector):
        if "a[href" in selector:
            return [_FakeEl(href=self._href)] if self._href else []
        return []

    def query_selector(self, selector):
        if "unread" in selector:
            return _FakeEl("1") if self._unread else None
        if "status" in selector or "badge" in selector:
            return _FakeEl(self._status) if self._status else None
        return None


class _FakePage:
    def __init__(self, cards):
        self._cards = cards

    def query_selector_all(self, selector):
        return self._cards


class _FakeClient:
    def __init__(self):
        self.posted = []

    def post_proposal_status(self, task_id, results):
        self.posted.append((task_id, results))
        return {"outcomes": 0, "replies": 0, "skipped": 0}


def _ctx(client):
    return HandlerContext(config=Config(worker_token="t"), client=client,
                          browser=object())


# ---------------- canonical status mapping ----------------

@pytest.mark.parametrize("raw,expected", [
    ("Hired", "hired"),
    ("awarded", "hired"),
    ("Offer accepted", "hired"),
    ("Declined by client", "declined"),
    ("Archived", "declined"),
    ("Withdrawn", "declined"),
    ("Interviewing", "interviewing"),
    ("Shortlisted", "interviewing"),
    ("New message", "interviewing"),
    ("Viewed", "viewed"),
    ("Won", "hired"),
    ("Awarded to you", "hired"),
    ("Lost", "declined"),
    ("Sent 3 days ago", "pending"),
    ("", "pending"),
])
def test_canonical_status(raw, expected):
    assert canonical_status(raw) == expected


# ---------------- handler ----------------

def test_handler_matches_extracts_and_posts(monkeypatch):
    items = [
        {"proposal_queue_item_id": 11, "job_external_id": "~abc",
         "job_url": "https://www.upwork.com/jobs/~abc"},
        {"proposal_queue_item_id": 12, "job_external_id": "~def",
         "job_url": "https://www.upwork.com/jobs/~def"},
        {"proposal_queue_item_id": 13, "job_external_id": "~missing",
         "job_url": "https://www.upwork.com/jobs/~missing"},
    ]
    cards = [
        _FakeCard("React dashboard — Hired", href="/jobs/~abc", status="Hired"),
        _FakeCard("API work", href="/jobs/~def", status="Declined", unread=True),
        _FakeCard("Unrelated proposal", href="/jobs/~other", status="Viewed"),
    ]
    monkeypatch.setattr("worker.handlers.proposal_status.fetch_page",
                        lambda ctx, platform, user_id, url: _FakePage(cards))
    client = _FakeClient()
    result = handle_scrape_proposal_status(_FakeTask(items), _ctx(client))

    assert result == {"checked": 3, "matched": 2, "posted": 2}
    task_id, results = client.posted[0]
    assert task_id == 5
    by_id = {r["proposal_queue_item_id"]: r for r in results}
    assert by_id[11]["platform_status"] == "hired"
    assert by_id[11]["has_unread_reply"] is False
    assert by_id[12]["platform_status"] == "declined"
    assert by_id[12]["has_unread_reply"] is True
    assert 13 not in by_id  # not found on the page → skipped, not posted


def test_handler_falls_back_to_card_text_for_status(monkeypatch):
    items = [{"proposal_queue_item_id": 21, "job_external_id": "~abc",
              "job_url": ""}]
    # no dedicated status node → the card text itself is scanned
    cards = [_FakeCard("Senior React role\nInterviewing now",
                       href="/jobs/~abc", status=None)]
    monkeypatch.setattr("worker.handlers.proposal_status.fetch_page",
                        lambda ctx, platform, user_id, url: _FakePage(cards))
    client = _FakeClient()
    handle_scrape_proposal_status(_FakeTask(items), _ctx(client))
    results = client.posted[0][1]
    assert results[0]["platform_status"] == "interviewing"


def test_handler_empty_items_touches_nothing(monkeypatch):
    def _boom(*args):
        raise AssertionError("fetch_page must not run with no items")

    monkeypatch.setattr("worker.handlers.proposal_status.fetch_page", _boom)
    client = _FakeClient()
    result = handle_scrape_proposal_status(_FakeTask([]), _ctx(client))
    assert result == {"checked": 0, "matched": 0}
    assert client.posted == []


def test_handler_registered():
    assert get_handler("scrape_proposal_status") is handle_scrape_proposal_status


# ---------------- new browser platforms (fiverr/peopleperhour/guru) ----------------

def test_new_platform_configs_have_status_sync_keys():
    """Every browser-sync platform exposes the read-only sync page config."""
    for platform in ("upwork", "fiverr", "peopleperhour", "guru"):
        cfg = platform_config(platform)
        for key in ("proposals_url", "proposal_card",
                    "proposal_card_fields", "proposal_unread"):
            assert cfg.get(key), f"{platform} missing {key}"
        assert cfg["proposals_url"].startswith(cfg["base_url"])
        assert "status" in cfg["proposal_card_fields"]


@pytest.mark.parametrize("platform,status_text,expected,href", [
    # Fiverr inbox thread: an unread badge maps to the reply indicator
    ("fiverr", "New message", "interviewing", "/inbox/brief-9"),
    # PPH WorkStream: awarded proposal → hired
    ("peopleperhour", "Awarded", "hired", "/workstream/job-42"),
    # Guru quotes list: declined quote
    ("guru", "Declined", "declined", "/quotes/777"),
])
def test_handler_extracts_per_platform(monkeypatch, platform, status_text,
                                       expected, href):
    external_id = href.rsplit("/", 1)[-1]
    items = [{"proposal_queue_item_id": 31, "job_external_id": external_id,
              "job_url": f"https://example.com{href}"}]
    cards = [_FakeCard(f"Some thread\n{status_text}", href=href,
                       status=status_text, unread=True)]
    seen = {}

    def _fake_fetch(ctx, plt, user_id, url):
        seen["platform"] = plt
        seen["url"] = url
        return _FakePage(cards)

    monkeypatch.setattr("worker.handlers.proposal_status.fetch_page",
                        _fake_fetch)
    client = _FakeClient()
    result = handle_scrape_proposal_status(
        _FakeTask(items, platform=platform), _ctx(client))

    assert result == {"checked": 1, "matched": 1, "posted": 1}
    assert seen["platform"] == platform
    assert seen["url"] == platform_config(platform)["proposals_url"]
    posted = client.posted[0][1][0]
    assert posted["platform_status"] == expected
    assert posted["has_unread_reply"] is True


# ---------------- client post ----------------

def test_client_post_proposal_status():
    sent = {}

    def handler(request):
        sent["path"] = request.url.path
        sent["body"] = request.read()
        return httpx.Response(200, json={"outcomes": 1, "replies": 0,
                                         "skipped": 0})

    client = WorkerClient("http://backend", "secret-token", "w-1",
                          client=httpx.Client(
                              base_url="http://backend",
                              transport=httpx.MockTransport(handler),
                              headers={"Authorization": "Bearer secret-token"}))
    resp = client.post_proposal_status(5, [{"proposal_queue_item_id": 11,
                                            "platform_status": "hired",
                                            "has_unread_reply": False}])
    assert resp["outcomes"] == 1
    assert sent["path"] == "/api/gigs/proposal-status"
    assert b'"task_id": 5' in sent["body"]
