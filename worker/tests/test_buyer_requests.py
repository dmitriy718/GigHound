"""fetch_buyer_requests handler tests: seller-username requirement, stable
external IDs, URL absolutization, and currency parsing. Browser and backend
are fakes — no Playwright, no network."""
import pytest

from worker.config import Config
from worker.handlers.base import HandlerContext, parse_currency
from worker.handlers.buyer_requests import handle_fetch_buyer_requests
from worker.platforms import platform_config

_FIELDS = platform_config("fiverr")["brief_fields"]


class _FakeTask:
    def __init__(self, payload, user_id=7, platform="fiverr"):
        self.user_id = user_id
        self.platform = platform
        self.payload = payload


class _FakeEl:
    def __init__(self, text="", href=None):
        self._text = text
        self._href = href

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._href if name == "href" else None


class _FakeCard:
    """A brief card: field texts keyed by the platform config selectors,
    plus a link with a possibly-relative href."""

    def __init__(self, title="", description="", budget="", href=None):
        self._texts = {_FIELDS["title"]: title,
                       _FIELDS["description"]: description,
                       _FIELDS["budget"]: budget}
        self._href = href

    def query_selector(self, selector):
        if selector == "a[href]":
            return _FakeEl(href=self._href) if self._href else None
        text = self._texts.get(selector)
        return _FakeEl(text) if text else None


class _FakePage:
    def __init__(self, cards):
        self._cards = cards

    def query_selector_all(self, selector):
        return self._cards


class _FakeClient:
    def __init__(self):
        self.posted = []

    def post_buyer_requests(self, user_id, requests):
        self.posted.append((user_id, requests))
        return {"queued": 0}


def _ctx(client):
    return HandlerContext(config=Config(worker_token="t"), client=client,
                          browser=object())


def _scrape(monkeypatch, payload, cards, client):
    seen = {}

    def _fake_fetch(ctx, platform, user_id, url):
        seen["url"] = url
        return _FakePage(cards)

    monkeypatch.setattr("worker.handlers.buyer_requests.fetch_page", _fake_fetch)
    result = handle_fetch_buyer_requests(_FakeTask(payload), _ctx(client))
    return result, seen


# ---------------- seller username requirement ----------------

def test_handler_fails_loudly_without_seller_username():
    with pytest.raises(ValueError, match="no seller username configured"):
        handle_fetch_buyer_requests(_FakeTask({}), _ctx(_FakeClient()))

    with pytest.raises(ValueError, match="no seller username configured"):
        handle_fetch_buyer_requests(_FakeTask({"username": ""}),
                                    _ctx(_FakeClient()))


def test_handler_scrapes_the_seller_briefs_url(monkeypatch):
    client = _FakeClient()
    _, seen = _scrape(monkeypatch, {"username": "seller1"}, [], client)
    assert seen["url"] == "https://www.fiverr.com/users/seller1/briefs"


# ---------------- extraction: ids, urls, currency ----------------

def test_stable_ids_absolute_urls_and_parsed_currency(monkeypatch):
    cards = [
        _FakeCard(title="Need a logo", description="logo design",
                  budget="$120", href="/briefs/logo-123"),
        _FakeCard(title="Voice over", description="narration",
                  budget="negotiable", href=None),
    ]
    client = _FakeClient()
    result, _ = _scrape(monkeypatch, {"username": "seller1"}, cards, client)
    assert result == {"fetched": 2, "queued": 0}

    user_id, reqs = client.posted[0]
    assert user_id == 7
    # relative href absolutized against the platform base_url
    assert reqs[0]["url"] == "https://www.fiverr.com/briefs/logo-123"
    # external id from the brief URL path — stable across scrapes
    assert reqs[0]["id"] == "briefs/logo-123"
    assert reqs[0]["budget"] == 120.0
    assert reqs[0]["currency"] == "USD"
    # no link → content-hash id, empty url; unparseable budget → currency None
    assert reqs[1]["url"] == ""
    assert reqs[1]["id"].startswith("brief-")
    assert reqs[1]["budget"] is None
    assert reqs[1]["currency"] is None

    # a second scrape of the same cards yields the SAME ids (backend dedup)
    client2 = _FakeClient()
    _scrape(monkeypatch, {"username": "seller1"}, cards, client2)
    assert [r["id"] for r in client2.posted[0][1]] == [r["id"] for r in reqs]


@pytest.mark.parametrize("text,expected", [
    ("$120", "USD"),
    ("€300", "EUR"),
    ("£50-£100", "GBP"),
    ("Budget: USD 500", "USD"),
    ("250 EUR", "EUR"),
    ("negotiable", None),
    ("120", None),
    ("", None),
    (None, None),
])
def test_parse_currency(text, expected):
    assert parse_currency(text) == expected
