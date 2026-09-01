"""Shared handler scaffolding: context, page fetch, extraction helpers."""
import json
import logging
import random
import re
from dataclasses import dataclass

from ..browser import (BrowserManager, CaptchaDetectedError, human_delay,
                       raise_if_challenge, SessionExpiredError)
from ..client import WorkerClient
from ..config import Config
from ..platforms import platform_config

log = logging.getLogger(__name__)


class SelectorSuspectError(Exception):
    """Extraction yielded nothing at all on a verified session — the platform
    selectors in worker/platforms.py likely drifted. Failing loudly beats
    posting fabricated zeros into tenant analytics."""


@dataclass
class HandlerContext:
    config: Config
    client: WorkerClient
    browser: BrowserManager


def fetch_page(ctx: HandlerContext, platform: str, user_id: int, url: str):
    """Navigate to `url` on the (platform, user) session with human pacing.
    Raises CaptchaDetectedError when a challenge marker is present and
    SessionExpiredError when the session is logged out — so "no data" is
    never mistaken for a dead session."""
    page = ctx.browser.new_page(platform, user_id)
    _warm_entry(page, platform, url)
    page.goto(url, wait_until="domcontentloaded")
    human_delay(1.0, 2.5)
    raise_if_challenge(page, platform)
    _raise_if_session_expired(page, platform)
    _simulate_reading(page)
    return page


def _warm_entry(page, platform: str, url: str):
    """Vary the entry path: a cold direct goto on every visit is anomalous,
    so drop in on the platform's base URL first (as a returning user would)
    before heading to the target. Best-effort — never fatal."""
    base = platform_config(platform).get("base_url")
    if not base or url == base or not url.startswith(base):
        return
    try:
        page.goto(base, wait_until="domcontentloaded")
        human_delay(0.6, 1.6)
    except Exception as exc:  # noqa: BLE001 — warm-up is nice-to-have
        log.debug("warm-up navigation to %s failed: %s", base, exc)


def _simulate_reading(page):
    """Light pre-extraction behavior: a few smooth-ish scrolls and a short
    read pause scaled to content length — humans scroll and skim before they
    would ever 'extract' anything. Best-effort, never fatal."""
    try:
        for _ in range(random.randint(1, 3)):
            page.mouse.wheel(0, random.randint(300, 900))
            human_delay(0.2, 0.6)
    except Exception as exc:  # noqa: BLE001
        log.debug("pre-extraction scroll failed: %s", exc)
    try:
        length = page.evaluate(
            "() => document.body ? document.body.innerText.length : 0") or 0
    except Exception as exc:  # noqa: BLE001
        log.debug("content-length probe failed: %s", exc)
        length = 0
    human_delay(0.5, max(0.6, min(2.5, 0.5 + length / 4000)))


def _raise_if_session_expired(page, platform: str):
    """Best-effort session liveness check against the platform's configured
    markers (see worker/platforms.py — they REQUIRE live validation). Only
    runs when markers are configured."""
    cfg = platform_config(platform)
    login_redirect = cfg.get("login_redirect")
    logged_in_marker = cfg.get("logged_in_marker")
    if not login_redirect and not logged_in_marker:
        return
    if login_redirect and login_redirect in (page.url or ""):
        raise SessionExpiredError(platform, f"redirected to {page.url}")
    if logged_in_marker:
        try:
            page.wait_for_selector(logged_in_marker, timeout=5000)
        except Exception as exc:  # noqa: BLE001 — timeout means "not logged in"
            raise SessionExpiredError(
                platform, f"logged-in marker {logged_in_marker!r} absent") from exc


def extract_jsonld(page) -> list[dict]:
    """All JSON-LD blocks on the page, parsed (semantic, drift-resistant)."""
    blocks = page.eval_on_selector_all(
        "script[type='application/ld+json']",
        "els => els.map(e => e.textContent)",
    )
    out = []
    for raw in blocks or []:
        try:
            data = json.loads(raw)
            out.extend(data if isinstance(data, list) else [data])
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def extract_fields(page, field_map: dict[str, str]) -> dict[str, str]:
    """{field: inner_text} for each selector that matches; missing → absent."""
    out = {}
    for field, selector in field_map.items():
        el = page.query_selector(selector)
        if el is not None:
            text = el.inner_text()
            if text and text.strip():
                out[field] = text.strip()
    return out


def extract_cards(page, card_selector: str,
                  field_map: dict[str, str], limit: int = 10) -> list[dict]:
    """Extract up to `limit` repeated cards (listings, briefs) from a page."""
    cards = page.query_selector_all(card_selector)[:limit]
    rows = []
    for card in cards:
        row = {}
        for field, selector in field_map.items():
            el = card.query_selector(selector)
            if el is not None:
                text = el.inner_text()
                if text and text.strip():
                    row[field] = text.strip()
        link = card.query_selector("a[href]")
        if link is not None:
            href = link.get_attribute("href")
            if href:
                row["url"] = href
        if row:
            rows.append(row)
    return rows


def parse_number(text: str | None) -> float | None:
    """'1,234' / '$1.2k' / '≈ 300' → number, else None."""
    if not text:
        return None
    m = re.search(r"([\d,.]+)\s*([kKmM]?)", text.replace(",", ""))
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2).lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return value


_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "₱": "PHP"}
_CURRENCY_CODES = ("USD", "EUR", "GBP", "AUD", "CAD", "NZD", "INR", "PKR",
                   "BDT", "PHP", "PLN", "BRL", "NGN", "ZAR")


def parse_currency(text: str | None) -> str | None:
    """'$120' / 'EUR 300' → ISO code, else None (never a guessed default)."""
    if not text:
        return None
    m = re.search(r"\b(" + "|".join(_CURRENCY_CODES) + r")\b", text.upper())
    if m:
        return m.group(1)
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None


def url_for(ctx: HandlerContext, platform: str, template_key: str, **fmt) -> str:
    template = platform_config(platform)[template_key]
    return template.format(**fmt)
