import { useEffect, useState } from 'react';
import {
  getAccounts,
  getCredentialStatus,
  request,
  getSearchProfiles,
  runSearchProfileNow,
} from '../api/client';
import type { SearchProfile } from '../types';
import type { ViewKey } from '../App';

const DISMISS_KEY = 'gh_onboarding_dismissed_at';
const REVEAL_AFTER_MS = 24 * 60 * 60 * 1000; // re-show a dismissed strip after 24h if incomplete

interface ChecklistState {
  accountEnrolled: boolean;
  autoQueueProfile: SearchProfile | null;
  firstProfile: SearchProfile | null;
  pendingDrafts: number;
  unansweredReplies: number;
}

interface Props {
  onNavigate: (view: ViewKey) => void;
}

// Dismissible onboarding / attention strip — setup steps (account, profile, discovery)
// plus live attention counts (drafts awaiting review, unanswered client replies).
export default function OnboardingChecklist({ onNavigate }: Props) {
  const [state, setState] = useState<ChecklistState | null>(null);
  const [discoveryRan, setDiscoveryRan] = useState(false);
  const [runBusy, setRunBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [userId,setUserId] = useState<number|null>(null);
  const [updatedAt,setUpdatedAt] = useState<string|null>(null);

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      try {
        const [accounts, profiles, attention] = await Promise.all([
          getAccounts(),
          getSearchProfiles(),
          request<{user_id:number;pending_drafts:number;open_proposals_with_replies:number;last_successful_discovery:string|null;updated_at:string}>('/api/workbench/attention'),
        ]);
        const statuses = await Promise.all(
          accounts.filter(a=>a.enabled && a.mode!=='disabled').map((a) => getCredentialStatus(a.id).catch(() => null)),
        );
        if (cancelled) return;
        setUserId(attention.user_id);
        setUpdatedAt(attention.updated_at);
        setDiscoveryRan(Boolean(attention.last_successful_discovery));
        setState({
          accountEnrolled: statuses.some((s) => s?.enrolled),
          autoQueueProfile: profiles.find((p) => p.auto_queue_proposals) ?? null,
          firstProfile: profiles[0] ?? null,
          pendingDrafts: attention.pending_drafts,
          unansweredReplies: attention.open_proposals_with_replies,
        });
      } catch {
        // the strip is advisory — stay hidden rather than noisily erroring
        if (!cancelled) setState(null);
      }
    };
    void refresh();
    const timer = window.setInterval(refresh,30000);
    window.addEventListener('focus',refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus',refresh);
      cancelled = true;
    };
  }, []);

  if (!state || dismissed) return null;

  const steps = [
    { done: state.accountEnrolled },
    { done: state.autoQueueProfile != null },
    { done: discoveryRan },
    { done: state.pendingDrafts === 0 },
    { done: state.unansweredReplies === 0 },
  ];
  const allDone = steps.every((s) => s.done);

  // Nothing to show once setup is complete and nothing needs attention.
  // Dismissal holds for 24h — after that the strip re-appears while still incomplete.
  if (allDone) return null;
  const dismissedAt = Number(localStorage.getItem(`${DISMISS_KEY}:${userId}`) ?? 0);
  if (dismissedAt && Date.now() - dismissedAt < REVEAL_AFTER_MS) return null;

  const dismiss = () => {
    localStorage.setItem(`${DISMISS_KEY}:${userId}`, String(Date.now()));
    setDismissed(true);
  };

  const runDiscovery = () => {
    if (!state.firstProfile) return;
    setRunBusy(true);
    runSearchProfileNow(state.firstProfile.id)
      .then((res) => {
        if (res.platforms.length > 0) setDiscoveryRan(true);
      })
      .catch(() => {})
      .finally(() => setRunBusy(false));
  };

  const check = <span style={{ color: 'var(--green)' }}>✓</span>;
  const todo = <span style={{ color: 'var(--amber)' }}>○</span>;
  const rowText = { fontSize: 13 } as const;

  return (
    <div className="panel">
      <div className="spread">
        <div><h2 style={{ margin: 0 }}>Getting the most out of GigHound</h2>{updatedAt&&<span className="muted">Updated {new Date(updatedAt).toLocaleTimeString()}</span>}</div>
        <button className="btn secondary small" onClick={dismiss}>
          Dismiss
        </button>
      </div>
      <div className="item-list" style={{ marginTop: 10 }}>
        <div className="item-row" style={{ cursor: 'default' }}>
          <span style={rowText}>
            {state.accountEnrolled ? check : todo} Enroll credentials for an enabled platform account
          </span>
          {!state.accountEnrolled && (
            <button className="btn secondary small" onClick={() => onNavigate('accounts')}>
              Set up
            </button>
          )}
        </div>
        <div className="item-row" style={{ cursor: 'default' }}>
          <span style={rowText}>
            {state.autoQueueProfile ? check : todo} Create a search profile with auto-queue on
          </span>
          {!state.autoQueueProfile && (
            <button className="btn secondary small" onClick={() => onNavigate('searchProfiles')}>
              Create
            </button>
          )}
        </div>
        <div className="item-row" style={{ cursor: 'default' }}>
          <span style={rowText}>{discoveryRan ? check : todo} Run discovery</span>
          {!discoveryRan && (
            <button
              className="btn secondary small"
              disabled={runBusy || !state.firstProfile}
              title={state.firstProfile ? `Runs "${state.firstProfile.name}" now` : 'Create a search profile first'}
              onClick={runDiscovery}
            >
              {runBusy ? 'Queueing…' : 'Run now'}
            </button>
          )}
        </div>
        <div className="item-row" style={{ cursor: 'default' }}>
          <span style={rowText}>
            {state.pendingDrafts === 0
              ? <>{check} No drafts awaiting review</>
              : <>{todo} {state.pendingDrafts} draft{state.pendingDrafts === 1 ? '' : 's'} awaiting review</>}
          </span>
          {state.pendingDrafts > 0 && (
            <button className="btn secondary small" onClick={() => onNavigate('proposals')}>
              Review
            </button>
          )}
        </div>
        <div className="item-row" style={{ cursor: 'default' }}>
          <span style={rowText}>
            {state.unansweredReplies === 0
              ? <>{check} No open proposals with client replies</>
              : <>{todo} {state.unansweredReplies} open proposal{state.unansweredReplies === 1 ? '' : 's'} with client replies</>}
          </span>
          {state.unansweredReplies > 0 && (
            <button className="btn secondary small" onClick={() => onNavigate('proposals')}>
              Answer
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
