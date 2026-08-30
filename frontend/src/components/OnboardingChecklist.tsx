import { useEffect, useState } from 'react';
import {
  getAccounts,
  getCredentialStatus,
  getProposals,
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

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [accounts, profiles, pendingPage, submittedPage] = await Promise.all([
          getAccounts(),
          getSearchProfiles(),
          getProposals({ status: 'pending_review', limit: 1 }),
          getProposals({ status: 'submitted', limit: 200 }),
        ]);
        const statuses = await Promise.all(
          accounts.map((a) => getCredentialStatus(a.id).catch(() => null)),
        );
        if (cancelled) return;
        setState({
          accountEnrolled: statuses.some((s) => s?.enrolled),
          autoQueueProfile: profiles.find((p) => p.auto_queue_proposals) ?? null,
          firstProfile: profiles[0] ?? null,
          pendingDrafts: pendingPage.total,
          unansweredReplies: submittedPage.items.filter(
            (p) => p.client_replied_at != null && p.outcome === 'pending',
          ).length,
        });
      } catch {
        // the strip is advisory — stay hidden rather than noisily erroring
        if (!cancelled) setState(null);
      }
    })();
    return () => {
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
  const dismissedAt = Number(localStorage.getItem(DISMISS_KEY) ?? 0);
  if (dismissedAt && Date.now() - dismissedAt < REVEAL_AFTER_MS) return null;

  const dismiss = () => {
    localStorage.setItem(DISMISS_KEY, String(Date.now()));
    setDismissed(true);
  };

  const runDiscovery = () => {
    if (!state.firstProfile) return;
    setRunBusy(true);
    runSearchProfileNow(state.firstProfile.id)
      .then((res) => {
        if (res.queued) setDiscoveryRan(true);
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
        <h2 style={{ margin: 0 }}>Getting the most out of GigHound</h2>
        <button className="btn secondary small" onClick={dismiss}>
          Dismiss
        </button>
      </div>
      <div className="item-list" style={{ marginTop: 10 }}>
        <div className="item-row" style={{ cursor: 'default' }}>
          <span style={rowText}>
            {state.accountEnrolled ? check : todo} Connect a platform account with credentials
            enrolled
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
              ? <>{check} No unanswered client replies</>
              : <>{todo} {state.unansweredReplies} client repl{state.unansweredReplies === 1 ? 'y' : 'ies'} unanswered</>}
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
