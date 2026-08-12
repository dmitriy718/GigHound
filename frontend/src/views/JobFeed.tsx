import { useEffect, useState } from 'react';
import { archiveJob, getJobs, unarchiveJob } from '../api/client';
import type { Job, JobStatus, Platform } from '../types';
import { PLATFORMS } from '../types';
import type { AlertMessage } from '../hooks/useAlertsSocket';
import { ErrorBanner, ScoreBadge, ScoreBars, formatDate } from '../components/common';

interface Props {
  lastMessage: AlertMessage | null;
}

export default function JobFeed({ lastMessage }: Props) {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState<JobStatus | ''>('new');
  const [platform, setPlatform] = useState<Platform | ''>('');
  const [minScore, setMinScore] = useState(0);
  const [selected, setSelected] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<Job | null>(null);

  const load = () => {
    getJobs({
      status: status || undefined,
      platform: platform || undefined,
      min_score: minScore > 0 ? minScore : undefined,
      limit: 50,
    })
      .then((res) => {
        setJobs(res.jobs);
        setTotal(res.total);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, [status, platform, minScore]);

  // Live updates from the shared alerts socket
  useEffect(() => {
    if (!lastMessage) return;
    if (
      lastMessage.type === 'job_alert' ||
      lastMessage.type === 'hot_job' ||
      lastMessage.type === 'job_ingested'
    ) {
      const job = lastMessage.job;
      if (job) {
        // Dedupe by id — job_alert/hot_job/job_ingested may carry the same job
        setJobs((prev) => (prev.some((j) => j.id === job.id) ? prev : [job, ...prev]));
        if (lastMessage.type === 'hot_job') {
          setToast(job);
          window.setTimeout(() => setToast(null), 8000);
        }
      }
    }
  }, [lastMessage]);

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

  return (
    <div>
      <h1>Job Feed</h1>
      <p className="page-sub">{total} jobs total · live updates via WebSocket</p>
      <ErrorBanner error={error} />

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
      </div>

      <div className="job-list">
        {jobs.length === 0 && !error && <p className="muted">No jobs match these filters.</p>}
        {jobs.map((job) => (
          <div className="job-row" key={job.id} onClick={() => setSelected(job)}>
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

      {selected && (
        <>
          <div className="drawer-backdrop" onClick={() => setSelected(null)} />
          <div className="drawer">
            <button className="btn secondary small drawer-close" onClick={() => setSelected(null)}>
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
