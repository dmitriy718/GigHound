import { useEffect, useState } from 'react';
import {
  approveProposal,
  bulkApproveProposals,
  getPortfolioItems,
  getProposals,
  markProposalOutcome,
  rejectProposal,
  revertProposal,
  submitProposal,
  suggestProposalTemplates,
} from '../api/client';
import type {
  PortfolioItem,
  ProposalOutcome,
  ProposalQueueItem,
  ProposalStatus,
  RejectionReason,
  Template,
} from '../types';
import { PROPOSAL_STATUSES, REJECTION_REASONS } from '../types';
import type { AlertMessage } from '../hooks/useAlertsSocket';
import { ErrorBanner, ScoreBadge, formatDate, scoreClass } from '../components/common';

interface Props {
  lastMessage: AlertMessage | null;
}

interface DraftEdits {
  proposal_text: string;
  bid_amount: string;
  bid_period_days: string;
}

interface RejectDraft {
  reason: RejectionReason;
  notes: string;
}

const STATUS_COLORS: Record<ProposalStatus, string> = {
  pending_review: 'var(--amber)',
  approved: 'var(--accent)',
  rejected: 'var(--red)',
  submitted: 'var(--green)',
  failed: 'var(--red)',
  generation_failed: 'var(--red)',
};

const OUTCOME_COLORS: Record<ProposalOutcome, string> = {
  pending: 'var(--text-dim)',
  hired: 'var(--green)',
  rejected: 'var(--red)',
  ghosted: 'var(--amber)',
};

const editsFrom = (item: ProposalQueueItem): DraftEdits => ({
  proposal_text: item.humanized_text ?? item.proposal_text,
  bid_amount: item.bid_amount != null ? String(item.bid_amount) : '',
  bid_period_days: item.bid_period_days != null ? String(item.bid_period_days) : '',
});

const truncate = (s: string, n: number) => (s.length > n ? `${s.slice(0, n)}…` : s);

// win_rate may arrive as 0–1 fraction or 0–100 percentage depending on backend
const winRatePct = (t: Template) =>
  Math.round(t.win_rate <= 1 ? t.win_rate * 100 : t.win_rate);

export default function ProposalQueue({ lastMessage }: Props) {
  const [proposals, setProposals] = useState<ProposalQueueItem[]>([]);
  const [portfolio, setPortfolio] = useState<PortfolioItem[]>([]);
  const [statusFilter, setStatusFilter] = useState<ProposalStatus | ''>('pending_review');
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [edits, setEdits] = useState<Record<number, DraftEdits>>({});
  const [rejectDrafts, setRejectDrafts] = useState<Record<number, RejectDraft>>({});
  const [suggestions, setSuggestions] = useState<Record<number, Template[]>>({});
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [reviewer, setReviewer] = useState(() => localStorage.getItem('gh_reviewer') ?? '');
  const [error, setError] = useState<string | null>(null);
  const [rowError, setRowError] = useState<Record<number, string>>({});
  const [busyId, setBusyId] = useState<number | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  const showToast = (text: string) => {
    setToast(text);
    window.setTimeout(() => setToast(null), 8000);
  };

  const load = () => {
    getProposals(statusFilter || undefined)
      .then((items) => {
        setProposals(items);
        setSelected(new Set());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, [statusFilter]);

  useEffect(() => {
    getPortfolioItems()
      .then(setPortfolio)
      .catch(() => setPortfolio([])); // titles are cosmetic — ignore failure
  }, []);

  // Live updates from the shared alerts socket
  useEffect(() => {
    if (!lastMessage) return;
    if (lastMessage.type === 'proposal_queued') {
      // the generic fallback variant also allows type 'proposal_queued' — narrow via `in`
      const proposalId = 'proposal_id' in lastMessage ? lastMessage.proposal_id : 0;
      const job = 'job' in lastMessage ? lastMessage.job : undefined;
      showToast(job ? `New proposal drafted: ${job.title}` : `New proposal #${proposalId} drafted`);
      load();
    } else if (lastMessage.type === 'generation_failed') {
      const err = 'error' in lastMessage && lastMessage.error ? `: ${lastMessage.error}` : '';
      showToast(`Proposal generation failed${err}`);
      load();
    }
  }, [lastMessage]);

  const changeReviewer = (name: string) => {
    setReviewer(name);
    localStorage.setItem('gh_reviewer', name);
  };

  const toggleExpand = (item: ProposalQueueItem) => {
    if (expandedId === item.id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(item.id);
    setEdits((prev) => (prev[item.id] ? prev : { ...prev, [item.id]: editsFrom(item) }));
    // fetch "start from template" suggestions once per item
    if (!suggestions[item.id]) {
      suggestProposalTemplates(item.platform, item.job?.skills ?? [])
        .then((tpls) => setSuggestions((prev) => ({ ...prev, [item.id]: tpls })))
        .catch(() => setSuggestions((prev) => ({ ...prev, [item.id]: [] })));
    }
  };

  const patchEdit = (id: number, patch: Partial<DraftEdits>) =>
    setEdits((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));

  const patchReject = (id: number, patch: Partial<RejectDraft>) =>
    setRejectDrafts((prev) => {
      const base: RejectDraft = prev[id] ?? { reason: 'too_generic', notes: '' };
      return { ...prev, [id]: { ...base, ...patch } };
    });

  const replaceItem = (updated: ProposalQueueItem) => {
    setProposals((prev) =>
      statusFilter && updated.status !== statusFilter
        ? prev.filter((p) => p.id !== updated.id)
        : prev.map((p) => (p.id === updated.id ? updated : p)),
    );
    setEdits((prev) => ({ ...prev, [updated.id]: editsFrom(updated) }));
  };

  const requireReviewer = (id: number): boolean => {
    if (reviewer.trim()) return true;
    setRowError((prev) => ({ ...prev, [id]: 'Enter a reviewer name first.' }));
    return false;
  };

  const run = async (id: number, action: () => Promise<ProposalQueueItem>) => {
    setBusyId(id);
    setRowError((prev) => ({ ...prev, [id]: '' }));
    try {
      replaceItem(await action());
      setError(null);
    } catch (e) {
      // e.g. 501 for platforms without a compliant submission channel
      setRowError((prev) => ({ ...prev, [id]: (e as Error).message }));
    } finally {
      setBusyId(null);
    }
  };

  const approve = (item: ProposalQueueItem) => {
    if (!requireReviewer(item.id)) return;
    const draft = edits[item.id] ?? editsFrom(item);
    void run(item.id, () =>
      approveProposal(item.id, {
        reviewer: reviewer.trim(),
        proposal_text: draft.proposal_text,
        ...(draft.bid_amount !== '' ? { bid_amount: Number(draft.bid_amount) } : {}),
        ...(draft.bid_period_days !== '' ? { bid_period_days: Number(draft.bid_period_days) } : {}),
      }),
    );
  };

  const reject = (item: ProposalQueueItem) => {
    if (!requireReviewer(item.id)) return;
    const rd = rejectDrafts[item.id] ?? { reason: 'too_generic' as RejectionReason, notes: '' };
    void run(item.id, () =>
      rejectProposal(item.id, {
        reviewer: reviewer.trim(),
        reason: rd.reason,
        ...(rd.notes.trim() ? { notes: rd.notes.trim() } : {}),
      }),
    );
  };

  const submit = (item: ProposalQueueItem) => {
    void run(item.id, () => submitProposal(item.id));
  };

  const markOutcome = (item: ProposalQueueItem, outcome: Exclude<ProposalOutcome, 'pending'>) => {
    void run(item.id, () => markProposalOutcome(item.id, outcome));
  };

  const revert = (item: ProposalQueueItem, versionIndex: number) => {
    void run(item.id, () => revertProposal(item.id, versionIndex));
  };

  const applyTemplate = (item: ProposalQueueItem, templateId: string) => {
    const tpl = (suggestions[item.id] ?? []).find((t) => String(t.id) === templateId);
    if (!tpl) return;
    patchEdit(item.id, {
      proposal_text: tpl.text,
      ...(tpl.bid != null ? { bid_amount: String(tpl.bid) } : {}),
    });
  };

  const toggleSelected = (id: number) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const bulkApprove = () => {
    if (!reviewer.trim()) {
      setError('Enter a reviewer name first.');
      return;
    }
    setBulkBusy(true);
    bulkApproveProposals([...selected], reviewer.trim())
      .then((res) => {
        showToast(`Bulk approve: ${res.approved} approved, ${res.skipped} skipped`);
        load();
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setBulkBusy(false));
  };

  const portfolioTitle = (id: number) =>
    portfolio.find((p) => p.id === id)?.title ?? `#${id}`;

  return (
    <div>
      <h1>Proposal Queue</h1>
      <p className="page-sub">Review AI-drafted proposals before they go out · human-in-the-loop</p>
      <ErrorBanner error={error} />

      {toast && (
        <div className="toast">
          <strong>{toast}</strong>
          Queue refreshed
        </div>
      )}

      <div className="filters-bar">
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Status</label>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as ProposalStatus | '')}
          >
            <option value="">All</option>
            {PROPOSAL_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
        </div>
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
        {selected.size > 0 && (
          <button className="btn" disabled={bulkBusy} onClick={bulkApprove}>
            {bulkBusy ? 'Approving…' : `Approve selected (${selected.size})`}
          </button>
        )}
      </div>

      <div className="job-list">
        {proposals.length === 0 && !error && (
          <p className="muted">No proposals with this status.</p>
        )}
        {proposals.map((item) => {
          const draft = edits[item.id];
          const isOpen = expandedId === item.id;
          return (
            <div key={item.id}>
              <div className="job-row proposal-row" onClick={() => toggleExpand(item)}>
                {item.status === 'pending_review' ? (
                  <input
                    type="checkbox"
                    className="row-check"
                    checked={selected.has(item.id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={() => toggleSelected(item.id)}
                    aria-label="Select for bulk approve"
                  />
                ) : (
                  <span />
                )}
                {item.job ? (
                  <ScoreBadge score={item.job.quality_score} />
                ) : (
                  <span className="muted">—</span>
                )}
                <div>
                  <div className="job-title">
                    {item.job?.title ?? `Job #${item.job_id}`}
                    {item.request_type === 'buyer_request' && (
                      <span className="pill" style={{ marginLeft: 8, color: 'var(--accent)' }}>
                        buyer request
                      </span>
                    )}
                  </div>
                  <div className="job-meta">
                    {item.platform} ·{' '}
                    {item.bid_amount != null ? `bid $${item.bid_amount}` : 'no bid'} · drafted{' '}
                    {formatDate(item.created_at)}
                    {item.reviewed_by && ` · reviewed by ${item.reviewed_by}`}
                  </div>
                </div>
                <span className="pill" style={{ color: STATUS_COLORS[item.status] }}>
                  {item.status.replace(/_/g, ' ')}
                </span>
                {item.outcome !== 'pending' && (
                  <span className="pill" style={{ color: OUTCOME_COLORS[item.outcome] }}>
                    {item.outcome}
                  </span>
                )}
                {item.status === 'pending_review' && (
                  <span className="row-actions">
                    <button
                      className="btn small touch-btn"
                      title="Approve with current text"
                      disabled={busyId === item.id}
                      onClick={(e) => {
                        e.stopPropagation();
                        approve(item);
                      }}
                    >
                      ✓
                    </button>
                    <button
                      className="btn small danger touch-btn"
                      title="Open reject panel"
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!isOpen) toggleExpand(item);
                      }}
                    >
                      ✗
                    </button>
                  </span>
                )}
                <span className="muted" style={{ fontSize: 12 }}>
                  {isOpen ? '▲' : '▼'}
                </span>
              </div>

              {isOpen && draft && (
                <div className="panel" style={{ marginTop: 4 }}>
                  {rowError[item.id] && (
                    <div className="error-banner">{rowError[item.id]}</div>
                  )}

                  {item.status === 'generation_failed' && (
                    <div className="error-banner">
                      Generation failed
                      {typeof item.submission_result?.error === 'string' &&
                        `: ${item.submission_result.error}`}
                    </div>
                  )}

                  {item.job && (
                    <>
                      <h3>Job summary</h3>
                      <p className="muted" style={{ marginTop: 0 }}>
                        {item.job.budget_min != null || item.job.budget_max != null
                          ? `${item.job.currency} ${item.job.budget_min ?? '?'}–${item.job.budget_max ?? '?'}`
                          : 'no budget'}{' '}
                        · {item.job.proposals_count ?? '?'} proposals so far ·{' '}
                        <a
                          href={item.job.url}
                          target="_blank"
                          rel="noreferrer"
                          style={{ color: 'var(--accent)' }}
                        >
                          View original posting ↗
                        </a>
                      </p>
                    </>
                  )}

                  {item.analysis && (
                    <>
                      <h3>Analysis</h3>
                      <div className="spread" style={{ justifyContent: 'flex-start', gap: 10 }}>
                        <span className={`score-badge wide ${scoreClass(item.confidence)}`}>
                          {Math.round(item.confidence)}% confident
                        </span>
                        {item.needs_review && (
                          <span className="chip flag">needs review</span>
                        )}
                        {item.analysis.tone && (
                          <span className="muted" style={{ fontSize: 12 }}>
                            tone: {item.analysis.tone}
                          </span>
                        )}
                      </div>
                      {item.analysis.required_skills.length > 0 && (
                        <>
                          <div className="section-title">Required skills</div>
                          <div className="chips">
                            {item.analysis.required_skills.map((s) => (
                              <span className="chip" key={s}>
                                {s}
                              </span>
                            ))}
                          </div>
                        </>
                      )}
                      {(item.analysis.strengths.length > 0 || item.analysis.gaps.length > 0) && (
                        <>
                          <div className="section-title">Skill match</div>
                          <div className="chips">
                            {item.analysis.strengths.map((s) => (
                              <span className="chip" key={s} style={{ color: 'var(--green)' }}>
                                ✓ {s}
                              </span>
                            ))}
                            {item.analysis.gaps.map((g) => (
                              <span className="chip flag" key={g}>
                                {g} (not claimed)
                              </span>
                            ))}
                          </div>
                        </>
                      )}
                      {item.analysis.client_pain_points.length > 0 && (
                        <>
                          <div className="section-title">Client pain points</div>
                          <ul className="muted" style={{ margin: '4px 0', paddingLeft: 18 }}>
                            {item.analysis.client_pain_points.map((p) => (
                              <li key={p}>{p}</li>
                            ))}
                          </ul>
                        </>
                      )}
                      {item.analysis.missing_info.length > 0 && (
                        <>
                          <div className="section-title">Missing info</div>
                          <ul className="muted" style={{ margin: '4px 0', paddingLeft: 18 }}>
                            {item.analysis.missing_info.map((m) => (
                              <li key={m}>{m}</li>
                            ))}
                          </ul>
                        </>
                      )}
                      {item.analysis.red_flags.length > 0 && (
                        <>
                          <div className="section-title">Red flags</div>
                          <div className="chips">
                            {item.analysis.red_flags.map((f) => (
                              <span className="chip flag" key={f}>
                                {f}
                              </span>
                            ))}
                          </div>
                        </>
                      )}
                    </>
                  )}

                  {item.status === 'pending_review' && (
                    <div className="field">
                      <label>Start from template</label>
                      <select
                        value=""
                        onChange={(e) => applyTemplate(item, e.target.value)}
                      >
                        <option value="">
                          {suggestions[item.id]
                            ? suggestions[item.id].length > 0
                              ? 'Pick a template…'
                              : 'No matching templates'
                            : 'Loading templates…'}
                        </option>
                        {(suggestions[item.id] ?? []).map((t) => (
                          <option key={t.id} value={t.id}>
                            {t.title} — win rate {winRatePct(t)}%
                          </option>
                        ))}
                      </select>
                    </div>
                  )}

                  <div className="field">
                    <label>
                      Proposal text
                      {item.humanized_text != null && (
                        <span className="muted"> · humanized</span>
                      )}
                    </label>
                    <textarea
                      rows={8}
                      value={draft.proposal_text}
                      disabled={item.status !== 'pending_review'}
                      onChange={(e) => patchEdit(item.id, { proposal_text: e.target.value })}
                    />
                  </div>
                  <div className="form-row">
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Bid amount</label>
                      <input
                        type="number"
                        value={draft.bid_amount}
                        disabled={item.status !== 'pending_review'}
                        onChange={(e) => patchEdit(item.id, { bid_amount: e.target.value })}
                      />
                      {item.bid_rationale && (
                        <span className="muted" style={{ fontSize: 11 }} title={item.bid_rationale}>
                          {truncate(item.bid_rationale, 80)}
                        </span>
                      )}
                    </div>
                    <div className="field" style={{ marginBottom: 0 }}>
                      <label>Bid period (days)</label>
                      <input
                        type="number"
                        value={draft.bid_period_days}
                        disabled={item.status !== 'pending_review'}
                        onChange={(e) => patchEdit(item.id, { bid_period_days: e.target.value })}
                      />
                    </div>
                  </div>

                  {Object.keys(item.portfolio_match ?? {}).length > 0 && (
                    <>
                      <h3>Portfolio auto-selection</h3>
                      {Object.entries(item.portfolio_match).map(([pid, m]) => (
                        <div className="score-bar-row" key={pid}>
                          <span className="label">{m.title || portfolioTitle(Number(pid))}</span>
                          <div className="bar-track">
                            <div
                              className="bar-fill"
                              style={{ width: `${Math.min(100, m.overlap_pct)}%` }}
                            />
                          </div>
                          <span className="muted" style={{ textAlign: 'right', fontSize: 12 }}>
                            {Math.round(m.overlap_pct)}%
                          </span>
                          {m.matched_skills.length > 0 && (
                            <div
                              className="chips"
                              style={{ gridColumn: '1 / -1', marginTop: -2, marginBottom: 6 }}
                            >
                              {m.matched_skills.map((s) => (
                                <span className="chip" key={s}>
                                  {s}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      ))}
                    </>
                  )}

                  {item.portfolio_item_ids.length > 0 && (
                    <>
                      <h3>Attached portfolio</h3>
                      <div className="chips">
                        {item.portfolio_item_ids.map((pid) => (
                          <span className="chip" key={pid}>
                            {portfolioTitle(pid)}
                          </span>
                        ))}
                      </div>
                    </>
                  )}

                  {item.versions.length > 0 && (
                    <>
                      <h3>Version history</h3>
                      <div className="item-list">
                        {item.versions.map((v, i) => (
                          <div className="item-row" key={`${v.at}-${i}`} style={{ cursor: 'default' }}>
                            <span className="muted" style={{ fontSize: 12 }}>
                              v{i + 1} · {v.by} · {formatDate(v.at)}
                              {v.bid != null && ` · bid $${v.bid}`}
                            </span>
                            {item.status === 'pending_review' && (
                              <button
                                className="btn small secondary"
                                disabled={busyId === item.id}
                                onClick={() => revert(item, i)}
                              >
                                Revert
                              </button>
                            )}
                          </div>
                        ))}
                      </div>
                    </>
                  )}

                  {item.rejection_reason && (
                    <p className="muted" style={{ fontSize: 12 }}>
                      Rejection reason: {item.rejection_reason.replace(/_/g, ' ')}
                    </p>
                  )}

                  {Object.keys(item.submission_result ?? {}).length > 0 && (
                    <>
                      <h3>Submission result</h3>
                      <pre className="muted" style={{ fontSize: 12, whiteSpace: 'pre-wrap' }}>
                        {JSON.stringify(item.submission_result, null, 2)}
                      </pre>
                    </>
                  )}

                  <div className="form-row" style={{ marginTop: 12, marginBottom: 0 }}>
                    {item.status === 'pending_review' && (
                      <>
                        <button
                          className="btn"
                          disabled={busyId === item.id}
                          onClick={() => approve(item)}
                        >
                          Approve
                        </button>
                        <div className="field" style={{ marginBottom: 0 }}>
                          <label>Reject reason</label>
                          <select
                            value={(rejectDrafts[item.id]?.reason ?? 'too_generic') as RejectionReason}
                            onChange={(e) =>
                              patchReject(item.id, { reason: e.target.value as RejectionReason })
                            }
                          >
                            {REJECTION_REASONS.map((r) => (
                              <option key={r} value={r}>
                                {r.replace(/_/g, ' ')}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 160 }}>
                          <label>Notes (optional)</label>
                          <input
                            type="text"
                            value={rejectDrafts[item.id]?.notes ?? ''}
                            placeholder="Why is this rejected?"
                            onChange={(e) => patchReject(item.id, { notes: e.target.value })}
                          />
                        </div>
                        <button
                          className="btn danger"
                          disabled={busyId === item.id}
                          onClick={() => reject(item)}
                        >
                          Reject
                        </button>
                      </>
                    )}
                    {item.status === 'approved' && (
                      <button
                        className="btn"
                        disabled={busyId === item.id}
                        onClick={() => submit(item)}
                      >
                        {busyId === item.id ? 'Submitting…' : 'Submit to platform'}
                      </button>
                    )}
                    {item.status === 'submitted' && (
                      <>
                        <span className="muted" style={{ fontSize: 12, alignSelf: 'center' }}>
                          Outcome:
                        </span>
                        {(['hired', 'rejected', 'ghosted'] as const).map((o) => (
                          <button
                            key={o}
                            className="btn small secondary"
                            disabled={busyId === item.id}
                            onClick={() => markOutcome(item, o)}
                          >
                            {o[0].toUpperCase() + o.slice(1)}
                          </button>
                        ))}
                      </>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
