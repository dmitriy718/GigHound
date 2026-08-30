import { useEffect, useState } from 'react';
import {
  approveProposal,
  getBuyerRequests,
  getProposals,
  rejectProposal,
} from '../api/client';
import type { ProposalQueueItem, RejectionReason } from '../types';
import { REJECTION_REASONS } from '../types';
import { useNewAlertMessages, type AlertMessage } from '../hooks/useAlertsSocket';
import { ErrorBanner, formatDate } from '../components/common';

interface Props {
  messages: AlertMessage[];
}

interface OfferEdits {
  proposal_text: string;
  bid_amount: string;
}

const editsFrom = (item: ProposalQueueItem): OfferEdits => ({
  proposal_text: item.humanized_text ?? item.proposal_text,
  bid_amount: item.bid_amount != null ? String(item.bid_amount) : '',
});

export default function BuyerRequestInbox({ messages }: Props) {
  const [offers, setOffers] = useState<{ offers_remaining_today: number; daily_limit: number } | null>(null);
  const [items, setItems] = useState<ProposalQueueItem[]>([]);
  const [edits, setEdits] = useState<Record<number, OfferEdits>>({});
  const [rejectReasons, setRejectReasons] = useState<Record<number, RejectionReason>>({});
  const [reviewer, setReviewer] = useState(() => localStorage.getItem('gh_reviewer') ?? '');
  const [error, setError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<Record<number, string>>({});
  const [busyId, setBusyId] = useState<number | null>(null);

  const load = () => {
    // only actionable (pending) offers — the API filters by status server-side
    getProposals({ status: 'pending_review', limit: 200 })
      .then((page) => {
        // buyer requests only, deduped by id
        const seen = new Set<number>();
        const brs = page.items.filter((p) => {
          if (p.request_type !== 'buyer_request' || seen.has(p.id)) return false;
          seen.add(p.id);
          return true;
        });
        setItems(brs);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  useEffect(() => {
    getBuyerRequests()
      .then(setOffers)
      .catch(() => setOffers(null)); // quota banner is cosmetic — ignore failure
  }, []);

  // Live updates from the shared alerts socket — new buyer-request offers appear live
  useNewAlertMessages(messages, (msg) => {
    if (msg.type === 'proposal_queued' || msg.type === 'generation_failed') load();
  });

  const changeReviewer = (name: string) => {
    setReviewer(name);
    localStorage.setItem('gh_reviewer', name);
  };

  const patchEdit = (id: number, patch: Partial<OfferEdits>) =>
    setEdits((prev) => ({ ...prev, [id]: { ...editsFrom(items.find((i) => i.id === id)!), ...prev[id], ...patch } }));

  const requireReviewer = (id: number): boolean => {
    if (reviewer.trim()) return true;
    setRowError((prev) => ({ ...prev, [id]: 'Enter a reviewer name first.' }));
    return false;
  };

  const approve = (item: ProposalQueueItem) => {
    if (!requireReviewer(item.id)) return;
    const draft = edits[item.id] ?? editsFrom(item);
    setBusyId(item.id);
    setRowError((prev) => ({ ...prev, [item.id]: '' }));
    approveProposal(item.id, {
      reviewer: reviewer.trim(),
      proposal_text: draft.proposal_text,
      ...(draft.bid_amount !== '' ? { bid_amount: Number(draft.bid_amount) } : {}),
    })
      .then(() => {
        setItems((prev) => prev.filter((p) => p.id !== item.id));
        setError(null);
      })
      .catch((e: Error) => setRowError((prev) => ({ ...prev, [item.id]: e.message })))
      .finally(() => setBusyId(null));
  };

  const reject = (item: ProposalQueueItem) => {
    if (!requireReviewer(item.id)) return;
    setBusyId(item.id);
    setRowError((prev) => ({ ...prev, [item.id]: '' }));
    rejectProposal(item.id, {
      reviewer: reviewer.trim(),
      reason: rejectReasons[item.id] ?? 'too_generic',
    })
      .then(() => {
        setItems((prev) => prev.filter((p) => p.id !== item.id));
        setError(null);
      })
      .catch((e: Error) => setRowError((prev) => ({ ...prev, [item.id]: e.message })))
      .finally(() => setBusyId(null));
  };

  return (
    <div>
      <h1>Buyer Requests</h1>
      <p className="page-sub">
        Fiverr-style buyer requests with auto-drafted offers · always human-reviewed
      </p>
      <ErrorBanner error={error} />

      <div className="filters-bar">
        {offers && (
          <span className="pill" style={{ alignSelf: 'center', fontSize: 13, padding: '6px 12px' }}>
            {offers.offers_remaining_today}/{offers.daily_limit} offers remaining
          </span>
        )}
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Reviewer name</label>
          <input
            type="text"
            value={reviewer}
            placeholder="Who is reviewing?"
            onChange={(e) => changeReviewer(e.target.value)}
          />
        </div>
        <button className="btn secondary" onClick={load}>
          Refresh
        </button>
      </div>

      {items.length === 0 && !error && (
        <p className="muted">No buyer-request offers in the queue.</p>
      )}

      <div className="job-list">
        {items.map((item) => {
          const draft = edits[item.id] ?? editsFrom(item);
          return (
            <div className="panel" key={item.id} style={{ marginBottom: 0 }}>
              {rowError[item.id] && <div className="error-banner">{rowError[item.id]}</div>}
              <div className="spread">
                <div>
                  <div className="job-title">{item.job?.title ?? `Request #${item.job_id}`}</div>
                  <div className="job-meta">
                    {item.platform}
                    {item.job && (item.job.budget_min != null || item.job.budget_max != null) && (
                      <>
                        {' '}
                        · budget {item.job.currency} {item.job.budget_min ?? '?'}–
                        {item.job.budget_max ?? '?'}
                      </>
                    )}{' '}
                    · drafted {formatDate(item.created_at)}
                  </div>
                </div>
                <span className="pill" style={{ color: 'var(--amber)' }}>
                  {item.status.replace(/_/g, ' ')}
                </span>
              </div>

              <div className="field" style={{ marginTop: 10 }}>
                <label>Offer text</label>
                <textarea
                  rows={6}
                  value={draft.proposal_text}
                  disabled={item.status !== 'pending_review'}
                  onChange={(e) => patchEdit(item.id, { proposal_text: e.target.value })}
                />
              </div>
              <div className="form-row" style={{ marginBottom: 0 }}>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Bid amount</label>
                  <input
                    type="number"
                    value={draft.bid_amount}
                    disabled={item.status !== 'pending_review'}
                    onChange={(e) => patchEdit(item.id, { bid_amount: e.target.value })}
                  />
                </div>
                {item.status === 'pending_review' && (
                  <>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Reject reason</label>
                      <select
                        value={rejectReasons[item.id] ?? 'too_generic'}
                        onChange={(e) =>
                          setRejectReasons((prev) => ({
                            ...prev,
                            [item.id]: e.target.value as RejectionReason,
                          }))
                        }
                      >
                        {REJECTION_REASONS.map((r) => (
                          <option key={r} value={r}>
                            {r.replace(/_/g, ' ')}
                          </option>
                        ))}
                      </select>
                    </div>
                    <button
                      className="btn danger touch-btn"
                      disabled={busyId === item.id}
                      onClick={() => reject(item)}
                    >
                      {busyId === item.id ? 'Rejecting…' : 'Reject'}
                    </button>
                    <button
                      className="btn touch-btn"
                      disabled={busyId === item.id}
                      onClick={() => approve(item)}
                    >
                      {busyId === item.id ? 'Approving…' : 'Approve offer'}
                    </button>
                  </>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
