"""SQL-backed per-platform automation stops and bounded trial admission.

Missing records mean a new scope; existing tenants are paused by the migration.
Database errors block admission. Redis state never grants dispatch permission.
"""
from contextlib import contextmanager
import logging
import time
from sqlalchemy import update
from sqlalchemy.exc import SQLAlchemyError
from .database import SessionLocal
from .models import AutomationCircuit
from .cache import cache  # retained for callers/tests of the shared cache module

log = logging.getLogger(__name__)
CLOSED, OPEN, HALF_OPEN = 'closed', 'open', 'half_open'
DEFAULT_COOLDOWN_SEC = 1800
TRIAL_TOKEN_TTL = 60
# Compatibility only: clearing these has no effect on durable permission.
_local: dict = {}
_local_trials: dict = {}


def _key(platform, user_id=None):
    return f'circuit:{platform}' if user_id is None else f'circuit:{platform}:{user_id}'


@contextmanager
def _session(db=None):
    if db is not None:
        yield db
    else:
        with SessionLocal() as session:
            yield session
            session.commit()


def _insert(db, values, *, replace=False):
    if db.bind.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    statement = insert(AutomationCircuit).values(**values)
    if replace:
        statement = statement.on_conflict_do_update(index_elements=['key'], set_={**{k:v for k,v in values.items() if k not in ('key','revision')}, 'revision':AutomationCircuit.revision+1})
    else:
        statement = statement.on_conflict_do_nothing(index_elements=['key'])
    db.execute(statement)


def get_state(platform, user_id=None, *, db=None):
    try:
        with _session(db) as session:
            row = session.query(AutomationCircuit).filter_by(key=_key(platform,user_id)).populate_existing().one_or_none()
            if row is None:
                return {'state':CLOSED,'opened_at':None,'reason':'','manual_stop':False,'revision':0}
            return {k:getattr(row,k) for k in ('state','opened_at','reason','manual_stop','revision')}
    except SQLAlchemyError:
        log.exception('Circuit storage unavailable; automation paused')
        return {'state':OPEN,'opened_at':None,'reason':'circuit storage unavailable','manual_stop':True}


def transition(platform, state, reason='', user_id=None, *, manual_stop=False, db=None):
    if state not in (CLOSED, OPEN, HALF_OPEN):
        raise ValueError('invalid circuit state')
    with _session(db) as session:
        _insert(session, dict(key=_key(platform,user_id), user_id=user_id, revision=1, state=state,
            opened_at=time.time() if state == OPEN else None, reason=reason[:2000],
            manual_stop=manual_stop, trial_until=0.0), replace=True)
        session.flush()


def is_closed(platform, user_id=None, *, db=None):
    try:
        with _session(db) as session:
            key = _key(platform,user_id)
            state = get_state(platform,user_id,db=session)
            if state['state'] == CLOSED:
                return True
            if state.get('manual_stop'):
                return False
            now = time.time()
            if state['state'] == OPEN:
                if state.get('opened_at') is None or now-state['opened_at'] <= DEFAULT_COOLDOWN_SEC:
                    return False
                # A concurrent operator stop/reopen must win over stale cooldown.
                changed = session.execute(update(AutomationCircuit).where(
                    AutomationCircuit.key == key, AutomationCircuit.state == OPEN,
                    AutomationCircuit.opened_at == state['opened_at'],
                    AutomationCircuit.manual_stop.is_(False),
                ).values(state=HALF_OPEN,trial_until=0.0,reason='cooldown elapsed',revision=AutomationCircuit.revision+1)).rowcount
                if not changed:
                    return False
            return bool(session.execute(update(AutomationCircuit).where(
                AutomationCircuit.key == key, AutomationCircuit.state == HALF_OPEN,
                AutomationCircuit.manual_stop.is_(False), AutomationCircuit.trial_until <= now,
            ).values(trial_until=now+TRIAL_TOKEN_TTL)).rowcount)
    except SQLAlchemyError:
        log.exception('Circuit admission unavailable; automation paused')
        return False


def open_circuit(platform, reason='', user_id=None, *, db=None):
    transition(platform,OPEN,reason,user_id,db=db)


def close_circuit(platform, reason='', user_id=None, *, db=None):
    transition(platform,CLOSED,reason,user_id,db=db)


def check(platform, user_id=None, *, db=None):
    if not is_closed(platform,db=db):
        return False, f"circuit OPEN for {platform}: {get_state(platform,db=db).get('reason','')}"
    if user_id is not None and not is_closed(platform,user_id,db=db):
        return False, f"circuit OPEN for {platform} (user {user_id}): {get_state(platform,user_id,db=db).get('reason','')}"
    return True, ''
