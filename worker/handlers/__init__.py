"""Task-kind → handler registry. Canonical kinds mirror backend/app/stealth.py;
legacy aliases keep older queued rows executable."""
from .buyer_requests import handle_fetch_buyer_requests
from .gig_draft import handle_create_gig_draft
from .manual_assist import (handle_submit_fiverr_offer,
                            handle_submit_proposal)
from .proposal_status import handle_scrape_proposal_status
from .scrape import handle_scrape_competitors, handle_scrape_gig_metrics
from .upwork_proposal import handle_submit_upwork_proposal

FETCH_BUYER_REQUESTS = "fetch_buyer_requests"
SCRAPE_GIG_METRICS = "scrape_gig_metrics"
SCRAPE_COMPETITORS = "scrape_competitors"
CREATE_GIG_DRAFT = "create_gig_draft"
SUBMIT_UPWORK_PROPOSAL = "submit_upwork_proposal"
SUBMIT_FIVERR_OFFER = "submit_fiverr_offer"
SUBMIT_PROPOSAL = "submit_proposal"
SCRAPE_PROPOSAL_STATUS = "scrape_proposal_status"

# legacy task_type → canonical kind (must match backend app.stealth.LEGACY_ALIASES).
# No backend code emits these anymore — kept so older queued rows stay executable.
LEGACY_ALIASES = {
    "fiverr_fetch_buyer_requests": FETCH_BUYER_REQUESTS,
    "gig_scrape_metrics": SCRAPE_GIG_METRICS,
    "competitor_scrape": SCRAPE_COMPETITORS,
    "fiverr_create_gig": CREATE_GIG_DRAFT,
    "upwork_catalog_upsert": CREATE_GIG_DRAFT,
    "fiverr_send_offer": SUBMIT_FIVERR_OFFER,
}

HANDLERS = {
    FETCH_BUYER_REQUESTS: handle_fetch_buyer_requests,
    SCRAPE_GIG_METRICS: handle_scrape_gig_metrics,
    SCRAPE_COMPETITORS: handle_scrape_competitors,
    CREATE_GIG_DRAFT: handle_create_gig_draft,
    SUBMIT_UPWORK_PROPOSAL: handle_submit_upwork_proposal,
    SUBMIT_FIVERR_OFFER: handle_submit_fiverr_offer,
    SUBMIT_PROPOSAL: handle_submit_proposal,
    SCRAPE_PROPOSAL_STATUS: handle_scrape_proposal_status,
}


def get_handler(task_type: str):
    """Resolve a task type (canonical or legacy) to its handler, or None."""
    kind = LEGACY_ALIASES.get(task_type, task_type)
    return HANDLERS.get(kind)
