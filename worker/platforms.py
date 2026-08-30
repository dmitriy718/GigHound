"""Per-platform site configuration — THE selector maintenance location.

Everything site-specific (URLs, CSS selectors, challenge markers, extraction
maps) lives here so a platform UI change means editing one dict, not handler
code. Handlers read config from this module only.

Selector strategy, in order of preference:
  1. JSON-LD (`script[type="application/ld+json"]`) — stable, semantic.
  2. Semantic/ARIA attributes ([data-testid], [aria-label], role).
  3. Class/structure selectors — last resort, most fragile.
"""
from typing import Any

# ---------------------------------------------------------------- challenge
# Heuristics for CAPTCHA / bot-challenge detection. A task whose page matches
# any marker is failed with result.captcha=true so the circuit breaker trips
# and a human is alerted. Generic markers apply to every platform.
GENERIC_CHALLENGE_MARKERS: list[str] = [
    "iframe[src*='captcha']",
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "iframe[src*='challenges.cloudflare.com']",
    "#challenge-running",               # Cloudflare managed challenge
    ".cf-browser-verification",
    "text=/verify you are human/i",
    "text=/are you a robot/i",
]

PLATFORMS: dict[str, dict[str, Any]] = {
    "fiverr": {
        "base_url": "https://www.fiverr.com",
        "login_url": "https://www.fiverr.com/login",
        # Briefs / buyer-request surface (Fiverr retired "Buyer Requests" in
        # 2022; the current equivalent is the Briefs inbox)
        "briefs_url": "https://www.fiverr.com/users/{username}/briefs",
        "brief_card": "[data-testid='brief-card'], .brief-card, article:has(a[href*='brief'])",
        "brief_fields": {
            "title": "h3, [data-testid='brief-title']",
            "description": "[data-testid='brief-description'], .description",
            "budget": "[data-testid='brief-budget'], .budget",
        },
        # seller dashboard gig analytics
        "gigs_dashboard_url": "https://www.fiverr.com/seller_dashboard/gigs",
        "metrics_fields": {
            "impressions": "[data-testid='impressions'], .impressions .value",
            "clicks": "[data-testid='clicks'], .clicks .value",
            "orders": "[data-testid='orders'], .orders .value",
            "revenue": "[data-testid='revenue'], .revenue .value",
        },
        # gig creation form (DRAFT only — the handler never clicks publish)
        "gig_new_url": "https://www.fiverr.com/gigs/new",
        "gig_form": {
            "title": "#gig-title, textarea[name='title']",
            "category": "[data-testid='category-select'], select[name='category']",
            "tags": "input[placeholder*='tag' i], input[name='tags']",
            "description": "[data-testid='description-editor'], textarea[name='description']",
            "price": "input[name='price'], input[aria-label*='price' i]",
            "save_draft": "button:has-text('Save as Draft'), button:has-text('Save & Continue')",
            # NEVER selected by the worker — documented tripwire:
            "publish_button_do_not_click": "button:has-text('Publish Gig')",
        },
        # search/category pages for competitor scraping
        "category_url": "https://www.fiverr.com/categories/{category}",
        "search_url": "https://www.fiverr.com/search/gigs?query={query}",
        "gig_card": "[data-testid='gig-card'], .gig-card-layout, li:has(a[href*='/s/'])",
        "gig_card_fields": {
            "title": "h3, [data-testid='gig-title']",
            "price": "[data-testid='price'], .price",
            "seller": "[data-testid='seller-name'], .seller-name",
            "rating": "[data-testid='rating'], .rating-score",
        },
        # custom offer form on a brief (manual-assist by default)
        "offer_form": {
            "message": "textarea[name='message'], [data-testid='offer-message']",
            "price": "input[name='price']",
            "submit_do_not_click": "button:has-text('Send Offer')",
        },
        # READ-ONLY outcome/reply sync (scrape_proposal_status): the seller
        # inbox holds client threads, including responses to briefs. Selectors
        # are best-effort: Fiverr's inbox is a JS app without stable test ids,
        # so the handler falls back to link/text heuristics when these drift.
        "proposals_url": "https://www.fiverr.com/inbox",
        "proposal_card": ("[data-testid='conversation'], .conversation-row, "
                          "li:has(a[href*='/inbox/']), "
                          "tr:has(a[href*='/conversations/'])"),
        "proposal_card_fields": {
            "title": "h3, [data-testid='conversation-title'], .username",
            "status": ("[data-testid='conversation-status'], .status, "
                       "[class*='status'], .badge"),
        },
        # unread-message badge on a thread (client replied)
        "proposal_unread": ("[data-testid='unread-badge'], .unread, "
                            "[class*='unread'], [aria-label*='unread' i]"),
        "challenge_markers": ["text=/please verify/i", "#px-captcha"],
    },
    "upwork": {
        "base_url": "https://www.upwork.com",
        "login_url": "https://www.upwork.com/ab/account-security/login",
        "job_url": "https://www.upwork.com/jobs/{external_id}",
        # proposal submission via the agency Business Manager session
        "proposal_form": {
            "apply_button": "button:has-text('Submit a Proposal'), #submit-proposal-button",
            "agency_selector": "[data-test='agency-dropdown'], select[name*='agency']",
            "member_selector": "[data-test='member-dropdown'], select[name*='member']",
            "cover_letter": "textarea[name='cover_letter'], textarea[aria-label*='cover letter' i]",
            "bid_amount": "input[name='bid'], input[aria-label*='bid' i]",
            "submit": "button:has-text('Submit Proposal'), button[type='submit'].primary",
        },
        "profile_url": "https://www.upwork.com/freelancers/{username}",
        "metrics_fields": {
            "impressions": "[data-test='impressions']",
            "clicks": "[data-test='clicks']",
            "orders": "[data-test='contracts']",
            "revenue": "[data-test='earnings']",
        },
        "search_url": "https://www.upwork.com/nx/search/jobs/?q={query}",
        # READ-ONLY proposal status sync (scrape_proposal_status). The
        # freelancer/agency proposals listing. Selectors are best-effort:
        # Upwork's proposals page is a JS app without stable test ids, so the
        # handler falls back to link/text heuristics when these drift.
        "proposals_url": "https://www.upwork.com/nx/proposals/",
        "proposal_card": ("[data-test='proposal'], .proposal-card, "
                          "li:has(a[href*='/jobs/']), article:has(a[href*='/jobs/'])"),
        "proposal_card_fields": {
            "title": "h3, h4, [data-test='proposal-title']",
            "status": ("[data-test='proposal-status'], .status, "
                       "[class*='status'], .badge"),
        },
        # unread-message badge on a proposal card (client replied)
        "proposal_unread": ("[data-test='unread'], .unread, [class*='unread'], "
                            "[aria-label*='unread' i], .badge:has-text('new')"),
        "challenge_markers": ["#challenge-running", "text=/unusual activity/i"],
    },
    # copy-assist platforms: manual-assist only (fill + screenshot, no submit)
    "peopleperhour": {
        "base_url": "https://www.peopleperhour.com",
        "login_url": "https://www.peopleperhour.com/login",
        "proposal_form": {
            "message": "textarea[name='proposal'], #proposal-text",
            "submit_do_not_click": "button:has-text('Send Proposal')",
        },
        # READ-ONLY outcome/reply sync (scrape_proposal_status): the
        # WorkStream list shows proposal/job state per client thread.
        # Best-effort selectors — PPH ships no stable test ids.
        "proposals_url": "https://www.peopleperhour.com/workstream",
        "proposal_card": (".workstream-item, [class*='workstream'], "
                          "li:has(a[href*='workstream']), "
                          "tr:has(a[href*='workstream'])"),
        "proposal_card_fields": {
            "title": "h3, .title, [class*='title']",
            "status": ".status, [class*='status'], .badge, .label",
        },
        "proposal_unread": (".unread, [class*='unread'], "
                            "[aria-label*='unread' i], .badge:has-text('new')"),
        "challenge_markers": [],
    },
    "guru": {
        "base_url": "https://www.guru.com",
        "login_url": "https://www.guru.com/login/",
        "proposal_form": {
            "message": "textarea[name='quote'], #quote-message",
            "submit_do_not_click": "button:has-text('Submit Quote')",
        },
        # READ-ONLY outcome/reply sync (scrape_proposal_status): the quotes
        # list shows each quote's state (pending/awarded/declined).
        # Best-effort selectors — Guru ships no stable test ids.
        "proposals_url": "https://www.guru.com/d/quotes/",
        "proposal_card": (".quote, [class*='quote'], "
                          "li:has(a[href*='/quotes/']), "
                          "tr:has(a[href*='/quotes/'])"),
        "proposal_card_fields": {
            "title": "h3, .title, [class*='title']",
            "status": ".status, [class*='status'], .badge, .label",
        },
        "proposal_unread": (".unread, [class*='unread'], "
                            "[aria-label*='unread' i], .badge:has-text('new')"),
        "challenge_markers": [],
    },
}


def platform_config(platform: str) -> dict[str, Any]:
    cfg = PLATFORMS.get(platform)
    if cfg is None:
        raise KeyError(f"no platform config for '{platform}' — add it in worker/platforms.py")
    return cfg


def challenge_markers(platform: str) -> list[str]:
    return GENERIC_CHALLENGE_MARKERS + platform_config(platform).get("challenge_markers", [])
