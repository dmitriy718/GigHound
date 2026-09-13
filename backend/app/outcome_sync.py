"""Outcome + client-reply sync for Freelancer (Phase 2.4).

Polls the Freelancer adapter for submitted (or browser-queued) proposals:
  * `get_bid_status` → awarded ⇒ hired, rejected ⇒ rejected (via
    `templates.record_outcome`, so template win rates update);
  * `get_threads` → a client message newer than the submission sets
    `client_replied_at` and pushes a `client_replied` WS event.

Per-item errors are logged and skipped — one bad bid never kills the tick.
"""
import logging
import hashlib
import math
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .adapters.base import AdapterError
from .adapters.freelancer import FreelancerAdapter
from .models import AdapterCredential, Job, ProposalQueueItem, User, PlatformAccount
from .adapters.vault import StateStore
from .templates import record_outcome
from .ws_manager import alerts

log = logging.getLogger(__name__)

_BID_OUTCOME_MAP = {"awarded": "hired", "rejected": "rejected"}
_WATCHED_STATUSES = ("submitted",)


def _has_freelancer_credentials(db: Session, user_id: int) -> bool:
    return (db.query(AdapterCredential)
            .filter(AdapterCredential.user_id == user_id,
                    AdapterCredential.platform == "freelancer")
            .first()) is not None


def _bid_id_of(item: ProposalQueueItem) -> int | None:
    result = item.submission_result or {}
    bid_id = (result.get("response") or {}).get("id") or result.get("bid_id")
    try:
        return int(bid_id) if bid_id is not None else None
    except (TypeError, ValueError):
        return None


def _bidder_id_of(item: ProposalQueueItem) -> int | None:
    """Our Freelancer user id for this bid (to tell client messages from ours)."""
    try:
        bidder_id = (item.submission_result or {}).get("bidder_id")
        return int(bidder_id) if bidder_id else None
    except (TypeError, ValueError):
        return None


def _submitted_at(item: ProposalQueueItem) -> datetime:
    ts = item.submitted_at
    if ts is None:
        return datetime.now(timezone.utc)
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _thread_project_id(thread):
    if not isinstance(thread,dict):
        return None
    nested=thread.get('thread')
    if isinstance(nested,dict):
        context=nested.get('context')
        if isinstance(context,dict) and context.get('type')=='project':
            return context.get('id')
        return None
    # Previously normalized/local records remain readable.
    project=thread.get('project') or {}
    return (project.get('id') or thread.get('project_id')) if isinstance(project,dict) else None


def _client_reply(thread: dict, item: ProposalQueueItem, bidder_id: int | None,
                  job: Job) -> tuple[float, str] | None:
    """(timestamp, snippet) when the thread holds a client message newer than
    the submission for this item's project; else None."""
    if not isinstance(thread,dict):
        return None
    project_id = _thread_project_id(thread)
    if str(project_id) != str(job.external_id):
        return None
    candidates = list(thread.get('messages') or []) if isinstance(thread.get('messages'),list) else []
    candidates.append(thread.get('last_message'))
    nested = thread.get('thread')
    if isinstance(nested,dict): candidates.append(nested.get('message'))
    hits = [hit for message in candidates if (hit := _message_reply(message,item,bidder_id,job)) is not None]
    return min(hits,key=lambda hit:hit[0]) if hits else None


def _message_reply(last,item,bidder_id,job):
    if not isinstance(last,dict):
        return None
    sender = last.get('from_user',last.get('from_user_id'))
    if 'from_user' in last and 'from_user_id' in last and str(last['from_user'])!=str(last['from_user_id']):
        return None
    client = job.client_info if isinstance(job.client_info,dict) else {}
    client_id = client.get('client_id')
    # Project relevance alone does not prove that an unknown sender is a client.
    if (bidder_id is None or sender is None or not str(sender).isdigit() or not str(client_id).isdigit() or int(str(sender)) <= 0 or int(str(client_id)) <= 0
            or isinstance(sender,(bool,dict,list))
            or client_id is None or str(sender) != str(client_id)
            or str(sender) == str(bidder_id)):
        return None
    ts = last.get('time_created',last.get('time'))
    if isinstance(ts,bool) or not isinstance(ts,(int,float)) or not math.isfinite(ts):
        return None
    try:
        message_at = datetime.fromtimestamp(ts,timezone.utc)
    except (ValueError,OverflowError,OSError):
        return None
    if message_at <= _submitted_at(item) or message_at > datetime.now(timezone.utc):
        return None
    message = last.get('message')
    if not isinstance(message,str) or not message.strip():
        return None
    return ts,message[:200]


def reply_cursor_key(principal):
    return 'reply_poll:' + hashlib.sha256(principal.encode()).hexdigest()


def _item_account(item, accounts):
    selected = (item.approved_snapshot or {}).get('account_id') or item.platform_account_id
    if selected is not None:
        account = accounts.get(selected)
        return (account.id,account.principal) if account else None
    if len(accounts) == 1:
        account = next(iter(accounts.values()))
        return account.id,account.principal
    if not accounts:
        return None,'default'  # Historical default credentials, with no enrolled accounts.
    return None  # Historical multi-account ownership needs reconciliation.


async def _sync_account_threads(db,user,adapter,principal,accounts):
    """Advance a durable thread cursor and match each page against all proposals.

    The independent bid cursor must not limit reply matching: otherwise two
    rotating cursors could repeatedly miss a project in the opposite batch.
    """
    state = StateStore(db,user.id)
    key = reply_cursor_key(principal)
    previous = state.get('freelancer',key,{})
    previous = previous if isinstance(previous,dict) else {}
    offset = previous.get('offset',0)
    if type(offset) is not int or offset < 0:
        offset = 0
    started = datetime.now(timezone.utc).isoformat()
    replies = 0
    pages = 0
    try:
        for _ in range(5):
            threads = await adapter.get_threads(limit=50,offset=offset)
            if not isinstance(threads,list):
                raise ValueError('invalid thread page')
            project_ids=set()
            for thread in threads:
                if not isinstance(thread,dict): continue
                project_id=_thread_project_id(thread)
                if isinstance(project_id,(str,int)) and not isinstance(project_id,bool):
                    project_ids.add(str(project_id))
            candidates = db.query(ProposalQueueItem,Job).join(Job,Job.id==ProposalQueueItem.job_id).filter(
                ProposalQueueItem.user_id==user.id,Job.user_id==user.id,
                ProposalQueueItem.platform=='freelancer',Job.platform=='freelancer',
                ProposalQueueItem.status=='submitted',ProposalQueueItem.client_replied_at.is_(None),
                Job.external_id.in_(project_ids)).all() if project_ids else []
            for item,job in candidates:
                binding=_item_account(item,accounts)
                if binding is None or binding[1]!=principal:
                    continue
                for thread in threads:
                    hit=_client_reply(thread,item,_bidder_id_of(item),job)
                    if not hit: continue
                    from sqlalchemy import update
                    ts,snippet=hit
                    changed=db.execute(update(ProposalQueueItem).where(
                        ProposalQueueItem.id==item.id,ProposalQueueItem.user_id==user.id,
                        ProposalQueueItem.status=='submitted',ProposalQueueItem.client_replied_at.is_(None)
                    ).values(client_replied_at=datetime.fromtimestamp(ts,timezone.utc))).rowcount
                    db.commit()
                    if changed:
                        replies+=1
                        await alerts.broadcast(user.id,{'type':'client_replied','proposal_id':item.id,'job_id':job.id,'snippet':snippet})
                    break
            pages+=1
            finished=len(threads)<50
            offset=0 if finished else offset+50
            progress={**previous,'offset':offset,'last_attempt_at':started,
                      'last_success_at':datetime.now(timezone.utc).isoformat(),'last_error':None}
            if finished:
                progress['last_complete_scan_at']=progress['last_success_at']
            state.set('freelancer',key,progress)
            previous=progress
            if finished: break
    except Exception:
        db.rollback()
        state.set('freelancer',key,{**previous,'offset':offset,'last_attempt_at':started,
                                  'last_error':'Reply polling failed; the saved page will be retried.'})
        log.exception('reply polling failed for user %d',user.id)
    return replies,pages



async def sync_user_outcomes(db: Session, user: User) -> dict:
    """Poll bid statuses + message threads for one tenant's open proposals."""
    from .auth import platform_enabled
    if not user.is_active or not platform_enabled(db,user.id,'freelancer'):
        return {"checked":0,"outcomes":0,"replies":0}
    state = StateStore(db, user.id)
    saved_cursor = state.get("freelancer", "outcome_cursor", {})
    cursor = saved_cursor.get("id",0) if isinstance(saved_cursor,dict) else 0
    if type(cursor) is not int or cursor < 0:
        cursor = 0
    query = (db.query(ProposalQueueItem)
             .filter(ProposalQueueItem.user_id == user.id,
                     ProposalQueueItem.platform == "freelancer",
                     ProposalQueueItem.status.in_(_WATCHED_STATUSES)))
    items = query.filter(ProposalQueueItem.id > cursor).order_by(ProposalQueueItem.id).limit(200).all()
    if not items and cursor:
        items = query.order_by(ProposalQueueItem.id).limit(200).all()
    if not items or not _has_freelancer_credentials(db, user.id):
        return {"checked": 0, "outcomes": 0, "replies": 0}

    adapters = {}
    accounts = {a.id:a for a in db.query(PlatformAccount).filter(
        PlatformAccount.user_id==user.id,PlatformAccount.platform=="freelancer",
        PlatformAccount.enabled.is_(True),PlatformAccount.mode!="disabled").all()}
    outcomes = replies = 0
    try:
        threads: list[dict] = []
        for item in items:
            try:
                job = db.get(Job, item.job_id)
                if job is None:
                    continue
                binding = _item_account(item,accounts)
                if binding is None:
                    continue
                account_id,principal = binding
                if principal not in adapters:
                    adapters[principal] = FreelancerAdapter(db, user.id, principal=principal)
                adapter = adapters[principal]
                bid_id = _bid_id_of(item)
                if bid_id and item.outcome == "pending":
                    status = await adapter.get_bid_status(bid_id)
                    outcome = _BID_OUTCOME_MAP.get((status.get("award_status") or status.get("status") or "").lower())
                    if outcome:
                        outcomes += int(record_outcome(db, item, outcome))
                        log.info("outcome sync: proposal %d → %s", item.id, outcome)

            except Exception as exc:  # noqa: BLE001 — per-item isolation
                log.warning("outcome sync: item %d failed (%s); continuing",
                            item.id, exc)
                db.rollback()
        for principal,adapter in adapters.items():
            found,_ = await _sync_account_threads(db,user,adapter,principal,accounts)
            replies += found
    except AdapterError as exc:
        log.warning("outcome sync: adapter failed for user %d: %s", user.id, exc)
    finally:
        for adapter in adapters.values():
            await adapter.close()
    state.set("freelancer", "outcome_cursor", {"id": items[-1].id})
    return {"checked": len(items), "outcomes": outcomes, "replies": replies}
