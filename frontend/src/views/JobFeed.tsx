import { useEffect, useRef, useState } from 'react';
import { archiveJob, bulkArchiveJobs, getJob, getJobs, unarchiveJob } from '../api/client';
import type { ClientHistory, Job, JobStatus, Platform } from '../types';
import { PLATFORMS } from '../types';
import {
  useNewAlertMessages,
  useReconnectRefetch,
  type AlertMessage,
  type SocketStatus,
} from '../hooks/useAlertsSocket';
import { ErrorBanner, ScoreBadge, ScoreBars, formatDate } from '../components/common';
import OnboardingChecklist from '../components/OnboardingChecklist';
import type { ViewKey } from '../App';

interface Props {
  messages: AlertMessage[];
  status: SocketStatus;
  onNavigate: (view: ViewKey) => void;
}

const PAGE_SIZE = 50;

export default function JobFeed({ messages, status: socketStatus, onNavigate }: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [status, setStatus] = useState<JobStatus | ''>('new');
  const [platform, setPlatform] = useState<Platform | ''>('');
  const [minScore, setMinScore] = useState(0);
  // debounced (~250ms) so dragging the slider fires one query, not one per tick
  const [debouncedMinScore, setDebouncedMinScore] = useState(0);
  const [selected, setSelected] = useState<Job | null>(null);
  // Phase 3: client outcome history — only on the job detail endpoint, so fetched on drawer open
  const [clientHistory, setClientHistory] = useState<ClientHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<Job | null>(null);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // stale-response guards: only the newest load/detail request may land
  const loadSeq = useRef(0);
  const selectedIdRef = useRef<number | null>(null);

  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedMinScore(minScore), 250);
    return () => window.clearTimeout(t);
  }, [minScore]);

  const load = () => {
    const seq = ++loadSeq.current;
    getJobs({
      status: status || undefined,
      platform: platform || undefined,
      min_score: debouncedMinScore > 0 ? debouncedMinScore : undefined,
      limit: PAGE_SIZE,
      offset,
    })
      .then((res) => {
        if (seq !== loadSeq.current) return; // a newer request superseded this one
        setJobs(res.jobs);
        setTotal(res.total);
        setError(null);
      })
      .catch((e: Error) => {
        if (seq !== loadSeq.current) return;
        setError(e.message);
      });
  };

  useEffect(load, [status, platform, debouncedMinScore, offset]);

  // a filter change starts over from the first page
  useEffect(() => setOffset(0), [status, platform, debouncedMinScore]);

  // reconnect = events were missed while the socket was down — reload once
  useReconnectRefetch(socketStatus, load);

  // Live updates from the shared alerts socket — every unseen message is processed,
  // so bursts collapsed by React batching are not dropped
  useNewAlertMessages(messages, (msg) => {
    if (msg.type === 'job_alert' || msg.type === 'hot_job' || msg.type === 'job_ingested') {
      const job = msg.job;
      if (job) {
        // Dedupe by id — job_alert/hot_job/job_ingested may carry the same job
        setJobs((prev) => (prev.some((j) => j.id === job.id) ? prev : [job, ...prev]));
        if (msg.type === 'hot_job') {
          setToast(job);
          window.setTimeout(() => setToast(null), 8000);
        }
      }
    }
  });

  const openJob = (job: Job) => {
    selectedIdRef.current = job.id;
    setSelected(job);
    setClientHistory(null);
    getJob(job.id)
      .then((detail) => {
        // discard if the user opened another job (or closed the drawer) meanwhile
        if (selectedIdRef.current === job.id) {
          setClientHistory(detail.client_history ?? null);
        }
      })
      .catch(() => {
        if (selectedIdRef.current === job.id) setClientHistory(null); // history is cosmetic — ignore failure
      });
  };

  const closeDrawer = () => {
    selectedIdRef.current = null;
    setSelected(null);
    setClientHistory(null);
  };

  const toggleArchive = async (job: Job) => {
    try {
      const updated = job.status === 'archived' ? await unarchiveJob(job.id) : await archiveJob(job.id);
      setJobs((prev) => prev.map((j) => (j.id === updated.id ? updated : j)));
      setSelected(updated);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggleChecked = (id: number) =>
    setChecked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  // Selects every job matching the current filter (i.e. all loaded rows)
  const selectAllVisible = () => setChecked(new Set(jobs.map((j) => j.id)));

  const bulkArchive = async () => {
    setBulkBusy(true);
    try {
      const res = await bulkArchiveJobs([...checked]);
      setNotice(
        `Archived ${res.archived.length} job${res.archived.length === 1 ? '' : 's'}` +
          (res.skipped.length > 0 ? ` · ${res.skipped.length} skipped` : ''),
      );
      window.setTimeout(() => setNotice(null), 5000);
      setChecked(new Set());
      setError(null);
      load();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBulkBusy(false);
    }
  };

  return (
    <div>
      <h1>Job Feed</h1>
      <p className="page-sub">{total} jobs total · live updates via WebSocket</p>
      <OnboardingChecklist onNavigate={onNavigate} />
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      {toast && (
        <div className="toast">
          <strong>Hot job: {toast.title}</strong>
          Score {Math.round(toast.quality_score)} · {toast.platform}
        </div>
      )}

      <div className="filters-bar">
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Status</label>
          <select value={status} onChange={(e) => setStatus(e.target.value as JobStatus | '')}>
            <option value="">All</option>
            <option value="new">New</option>
            <option value="notified">Notified</option>
            <option value="archived">Archived</option>
          </select>
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Platform</label>
          <select value={platform} onChange={(e) => setPlatform(e.target.value as Platform | '')}>
            <option value="">All</option>
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ marginBottom: 0 }}>
          <label>
            Min score: <strong>{minScore}</strong>
          </label>
          <input
            type="range"
            min={0}
            max={100}
            value={minScore}
            onChange={(e) => setMinScore(Number(e.target.value))}
          />
        </div>
        <button className="btn secondary" onClick={load}>
          Refresh
        </button>
        <button className="btn secondary" onClick={selectAllVisible} disabled={jobs.length === 0}>
          Select all
        </button>
        {checked.size > 0 && (
          <>
            <button className="btn danger" disabled={bulkBusy} onClick={bulkArchive}>
              {bulkBusy ? 'Archiving…' : `Archive selected (${checked.size})`}
            </button>
            <button className="btn secondary" onClick={() => setChecked(new Set())}>
              Clear
            </button>
          </>
        )}
      </div>

      <div className="job-list">
        {jobs.length === 0 && !error && <p className="muted">No jobs match these filters.</p>}
        {jobs.map((job) => (
          <div
            className={`job-row ${checked.has(job.id) ? 'checked' : ''}`}
            key={job.id}
            onClick={() => openJob(job)}
          >
            <input
              type="checkbox"
              className="row-check job-check"
              checked={checked.has(job.id)}
              onClick={(e) => e.stopPropagation()}
              onChange={() => toggleChecked(job.id)}
              aria-label="Select for bulk archive"
            />
            <ScoreBadge score={job.quality_score} />
            <div>
              <div className="job-title">{job.title}</div>
              <div className="job-meta">
                {job.platform} · {job.job_type ?? '—'} ·{' '}
                {job.budget_min != null || job.budget_max != null
                  ? `${job.currency} ${job.budget_min ?? '?'}–${job.budget_max ?? '?'}`
                  : 'no budget'}{' '}
                · {formatDate(job.posted_at)}
              </div>
            </div>
            <span className="pill">{job.status}</span>
            {job.red_flags.length > 0 && (
              <span className="pill" style={{ color: 'var(--red)' }}>
                {job.red_flags.length} flag{job.red_flags.length > 1 ? 's' : ''}
              </span>
            )}
          </div>
        ))}
      </div>

      {total > 0 && (
        <div className="filters-bar" style={{ marginTop: 12 }}>
          <button
            className="btn secondary small"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
          >
            ← Prev
          </button>
          <span className="muted" style={{ fontSize: 12, alignSelf: 'center' }}>
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </span>
          <button
            className="btn secondary small"
            disabled={offset + PAGE_SIZE >= total}
            onClick={() => setOffset(offset + PAGE_SIZE)}
          >
            Next →
          </button>
        </div>
      )}

      {selected && (
        <>
          <div className="drawer-backdrop" onClick={closeDrawer} />
          <div className="drawer">
            <button className="btn secondary small drawer-close" onClick={closeDrawer}>
              Close
            </button>
            <h2>{selected.title}</h2>
            <p className="muted">
              <a href={selected.url} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>
                View original posting ↗
              </a>
            </p>
            <div className="spread" style={{ marginBottom: 12 }}>
              <ScoreBadge score={selected.quality_score} />
              {selected.status === 'archived' ? (
                <button className="btn" onClick={() => toggleArchive(selected)}>
                  Unarchive
                </button>
              ) : (
                <button className="btn danger" onClick={() => toggleArchive(selected)}>
                  Archive
                </button>
              )}
            </div>

            <h3>Score breakdown</h3>
            <ScoreBars breakdown={selected.score_breakdown} />

            {selected.red_flags.length > 0 && (
              <>
                <h3>Red flags</h3>
                <div className="chips">
                  {selected.red_flags.map((f) => (
                    <span className="chip flag" key={f}>
                      {f}
                    </span>
                  ))}
                </div>
              </>
            )}

            <h3>Client info</h3>
            <table className="data">
              <tbody>
                <tr>
                  <td className="muted">Payment verified</td>
                  <td>{selected.client_info.payment_verified ? 'Yes' : 'No'}</td>
                </tr>
                <tr>
                  <td className="muted">Hire rate</td>
                  <td>
                    {selected.client_info.hire_rate != null
                      ? `${selected.client_info.hire_rate}%`
                      : '—'}
                  </td>
                </tr>
                <tr>
                  <td className="muted">Total spent</td>
                  <td>
                    {selected.client_info.total_spent != null
                      ? `$${selected.client_info.total_spent.toLocaleString()}`
                      : '—'}
                  </td>
                </tr>
                <tr>
                  <td className="muted">Country</td>
                  <td>{selected.client_info.country ?? '—'}</td>
                </tr>
                <tr>
                  <td className="muted">Rating</td>
                  <td>
                    {selected.client_info.rating != null
                      ? `${selected.client_info.rating} (${selected.client_info.reviews_count ?? 0} reviews)`
                      : '—'}
                  </td>
                </tr>
              </tbody>
            </table>

            {clientHistory && (
              <>
                <h3>Client history</h3>
                <p
                  style={{
                    marginTop: 0,
                    color:
                      clientHistory.hired > 0
                        ? 'var(--green)'
                        : clientHistory.ghosted > clientHistory.hired
                          ? 'var(--amber)'
                          : 'var(--text-dim)',
                  }}
                >
                  Bid {clientHistory.past_proposals}× before — {clientHistory.hired} hired ·{' '}
                  {clientHistory.rejected} rejected · {clientHistory.ghosted} ghosted
                </p>
              </>
            )}

            <h3>Description</h3>
            <p style={{ whiteSpace: 'pre-wrap' }}>{selected.description}</p>

            {selected.skills.length > 0 && (
              <>
                <h3>Skills</h3>
                <div className="chips">
                  {selected.skills.map((s) => (
                    <span className="chip" key={s}>
                      {s}
                    </span>
                  ))}
                </div>
              </>
            )}
          </div>
        </>
      )}
    </div>
  );
}
