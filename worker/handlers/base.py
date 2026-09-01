"""Shared handler scaffolding: context, page fetch, extraction helpers."""
import json
import logging
import re
from dataclasses import dataclass

from ..browser import (BrowserManager, CaptchaDetectedError, human_delay,
                       raise_if_challenge)
from ..client import WorkerClient
from ..config import Config
from ..platforms import platform_config

log = logging.getLogger(__name__)


@dataclass
class HandlerContext:
    config: Config
    client: WorkerClient
    browser: BrowserManager


def fetch_page(ctx: HandlerContext, platform: str, user_id: int, url: str):
    """Navigate to `url` on the (platform, user) session with human pacing.
    Raises CaptchaDetectedError when a challenge marker is present."""
    page = ctx.browser.new_page(platform, user_id)
    page.goto(url, wait_until="domcontentloaded")
    human_delay(1.0, 2.5)
    raise_if_challenge(page, platform)
    return page


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
