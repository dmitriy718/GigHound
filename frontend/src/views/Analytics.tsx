import { useEffect, useState } from 'react';
import { getAnalyticsTrend, getFunnelAnalytics } from '../api/client';
import type { FunnelAnalytics, TrendAnalytics } from '../types';
import { useReconnectRefetch, type SocketStatus } from '../hooks/useAlertsSocket';
import { ErrorBanner } from '../components/common';

const FUNNEL_STAGES = [
  { key: 'queued', label: 'Queued' },
  { key: 'approved', label: 'Approved' },
  { key: 'submitted', label: 'Submitted' },
  { key: 'replied', label: 'Replied' },
  { key: 'hired', label: 'Hired' },
] as const;

// win_rate is a 0–100 float or null when no outcomes have been recorded yet
const winRateLabel = (rate: number | null) =>
  rate == null ? 'no outcomes yet' : `${Math.round(rate)}%`;

export default function Analytics({ status: socketStatus }: { status: SocketStatus }) {
  const [data, setData] = useState<FunnelAnalytics | null>(null);
  const [trend, setTrend] = useState<TrendAnalytics | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    getFunnelAnalytics()
      .then((res) => {
        setData(res);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
    // trend is additive — a failure (e.g. older backend) just yields the empty state
    getAnalyticsTrend(8)
      .then(setTrend)
      .catch(() => setTrend(null));
  };

  useEffect(load, []);

  // reconnect = events were missed while the socket was down — reload once
  useReconnectRefetch(socketStatus, load);

  const maxStage = data
    ? Math.max(1, ...FUNNEL_STAGES.map((s) => data.funnel[s.key]))
    : 1;
  // Leaderboard: best win rate first; templates with no outcomes (null) sink to the bottom
  const templates = data
    ? [...data.by_template].sort((a, b) => (b.win_rate ?? -1) - (a.win_rate ?? -1))
    : [];
  const weeks = trend?.weeks ?? [];
  const maxSubmitted = Math.max(1, ...weeks.map((w) => w.submitted));

  return (
    <div>
      <h1>Win-Rate Analytics</h1>
      <p className="page-sub">
        Proposal funnel, per-platform and per-template outcomes · the learning loop, quantified
      </p>
      <ErrorBanner error={error} />

      {!data && !error && <p className="muted">Loading analytics…</p>}

      {data && data.funnel.queued === 0 && (
        <div className="panel">
          <h2>No proposals yet</h2>
          <p className="muted" style={{ marginBottom: 0 }}>
            Analytics appear once proposals are queued and outcomes are recorded. Approve proposals
            in the queue, submit them, then mark outcomes (hired / rejected / ghosted) to close the
            loop.
          </p>
        </div>
      )}

      {data && data.funnel.queued > 0 && (
        <>
          <div className="panel">
            <h2>Funnel</h2>
            {FUNNEL_STAGES.map((stage) => (
              <div
                className="score-bar-row"
                style={{ gridTemplateColumns: '110px 1fr 60px' }}
                key={stage.key}
              >
                <span className="label">{stage.label}</span>
                <div className="bar-track">
                  <div
                    className="bar-fill"
                    style={{ width: `${(data.funnel[stage.key] / maxStage) * 100}%` }}
                  />
                </div>
                <span className="muted" style={{ textAlign: 'right', fontSize: 12 }}>
                  {data.funnel[stage.key]}
                </span>
              </div>
            ))}
            <div className="chips" style={{ marginTop: 10 }}>
              <span className="chip flag">rejected · {data.funnel.rejected}</span>
              <span className="chip" style={{ color: 'var(--amber)' }}>
                ghosted · {data.funnel.ghosted}
              </span>
            </div>
          </div>

          <div className="panel">
            <h2>By platform</h2>
            {data.by_platform.length === 0 ? (
              <p className="muted">No platform data yet.</p>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>Platform</th>
                    <th>Queued</th>
                    <th>Approved</th>
                    <th>Submitted</th>
                    <th>Replied</th>
                    <th>Hired</th>
                    <th>Win rate</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_platform.map((row) => (
                    <tr key={row.platform}>
                      <td>{row.platform}</td>
                      <td>{row.queued}</td>
                      <td>{row.approved}</td>
                      <td>{row.submitted}</td>
                      <td>{row.replied}</td>
                      <td>{row.hired}</td>
                      <td style={{ color: row.win_rate != null ? 'var(--green)' : undefined }}>
                        {winRateLabel(row.win_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="panel">
            <h2>Template leaderboard</h2>
            {templates.length === 0 ? (
              <p className="muted">No template outcomes yet.</p>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>Template</th>
                    <th>Platform</th>
                    <th>Uses</th>
                    <th>Wins</th>
                    <th>Losses</th>
                    <th>Win rate</th>
                  </tr>
                </thead>
                <tbody>
                  {templates.map((row) => (
                    <tr key={row.template_id}>
                      <td>{row.title}</td>
                      <td>{row.platform}</td>
                      <td>{row.uses}</td>
                      <td style={{ color: 'var(--green)' }}>{row.wins}</td>
                      <td style={{ color: 'var(--red)' }}>{row.losses}</td>
                      <td style={{ color: row.win_rate != null ? 'var(--green)' : undefined }}>
                        {winRateLabel(row.win_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="panel">
            <h2>Bid-band performance</h2>
            {data.by_bid_band.length === 0 ? (
              <p className="muted">No bid data yet.</p>
            ) : (
              <table className="data">
                <thead>
                  <tr>
                    <th>Bid band</th>
                    <th>Submitted</th>
                    <th>Hired</th>
                    <th>Win rate</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_bid_band.map((row) => (
                    <tr key={row.band}>
                      <td>{row.band}</td>
                      <td>{row.submitted}</td>
                      <td>{row.hired}</td>
                      <td style={{ color: row.win_rate != null ? 'var(--green)' : undefined }}>
                        {winRateLabel(row.win_rate)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>

          <div className="panel">
            <h2>Rejection reasons</h2>
            {data.rejection_reasons.length === 0 ? (
              <p className="muted">No rejections recorded yet.</p>
            ) : (
              <div className="chips">
                {data.rejection_reasons.map((r) => (
                  <span className="chip flag" key={r.reason}>
                    {r.reason.replace(/_/g, ' ')} · {r.count}
                  </span>
                ))}
              </div>
            )}
          </div>
        </>
      )}

      <div className="panel">
        <h2>Weekly trend</h2>
        {weeks.length === 0 ? (
          <p className="muted" style={{ marginBottom: 0 }}>
            No weekly data yet — submit proposals to start the trend.
          </p>
        ) : (
          weeks.map((w) => (
            <div
              className="score-bar-row"
              style={{ gridTemplateColumns: '110px 1fr 240px' }}
              key={w.week}
            >
              <span className="label">{w.week}</span>
              <div className="bar-track">
                <div
                  className="bar-fill"
                  style={{ width: `${(w.submitted / maxSubmitted) * 100}%` }}
                />
              </div>
              <span className="muted" style={{ textAlign: 'right', fontSize: 12 }}>
                {w.submitted} sub · {w.replied} rep · {w.hired} hired
                {w.win_rate != null && (
                  <span style={{ color: 'var(--green)' }}> · {Math.round(w.win_rate)}% won</span>
                )}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
