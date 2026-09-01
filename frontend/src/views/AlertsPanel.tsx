import { useEffect, useState } from 'react';
import { getAlertSettings, getDigestPreview, sendDigest, updateAlertSettings } from '../api/client';
import type { AlertMessage, SocketStatus } from '../hooks/useAlertsSocket';
import type { AlertSettings, Job } from '../types';
import { ErrorBanner, ScoreBadge } from '../components/common';

interface Props {
  messages: AlertMessage[];
  status: SocketStatus;
}

export default function AlertsPanel({ messages, status }: Props) {
  const [settings, setSettings] = useState<AlertSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [digest, setDigest] = useState<Job[] | null>(null);
  const [digestResult, setDigestResult] = useState<{ jobs_in_digest: number; emailed: boolean } | null>(null);
  const [sendingDigest, setSendingDigest] = useState(false);

  useEffect(() => {
    getAlertSettings()
      .then((s) => {
        setSettings(s);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const patch = (p: Partial<AlertSettings>) =>
    setSettings((s) => (s ? { ...s, ...p } : s));

  const save = async () => {
    if (!settings) return;
    try {
      setSettings(await updateAlertSettings(settings));
      setNotice('Settings saved.');
      setError(null);
      window.setTimeout(() => setNotice(null), 3000);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const loadDigest = async () => {
    try {
      const res = await getDigestPreview();
      setDigest(res.jobs);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const sendDigestNow = async () => {
    setSendingDigest(true);
    setDigestResult(null);
    try {
      const res = await sendDigest();
      setDigestResult(res);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSendingDigest(false);
    }
  };

  return (
    <div>
      <h1>Alerts</h1>
      <p className="page-sub">
        Realtime and digest notification settings · socket{' '}
        <span className={`socket-status socket-${status}`}>{status}</span>
      </p>
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      {settings ? (
        <div className="panel">
          <h2>Alert settings</h2>
          <div className="form-row">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={settings.realtime_enabled}
                onChange={(e) => patch({ realtime_enabled: e.target.checked })}
              />
              Realtime alerts enabled
            </label>
          </div>
          <div className="field" style={{ maxWidth: 320 }}>
            <label>
              Min score to alert: <strong>{settings.min_score_alert}</strong>
            </label>
            <input
              type="range"
              min={0}
              max={100}
              value={settings.min_score_alert}
              onChange={(e) => patch({ min_score_alert: Number(e.target.value) })}
            />
          </div>

          <div className="section-title">Digest mode</div>
          <div className="checks">
            {(['off', 'hourly', 'daily'] as const).map((mode) => (
              <label className="checkbox-row" key={mode}>
                <input
                  type="radio"
                  name="digest_mode"
                  checked={settings.digest_mode === mode}
                  onChange={() => patch({ digest_mode: mode })}
                />
                {mode}
              </label>
            ))}
          </div>

          <div className="section-title">Hot job alerts</div>
          <div className="form-row">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={settings.hot_job_enabled}
                onChange={(e) => patch({ hot_job_enabled: e.target.checked })}
              />
              Hot job alerts enabled
            </label>
          </div>
          {settings.hot_job_enabled && (
            <>
              <div className="field" style={{ maxWidth: 320 }}>
                <label>
                  Hot job min score: <strong>{settings.hot_job_min_score}</strong>
                </label>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={settings.hot_job_min_score}
                  onChange={(e) => patch({ hot_job_min_score: Number(e.target.value) })}
                />
              </div>
              <div className="form-row">
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Max proposals</label>
                  <input
                    type="number"
                    value={settings.hot_job_max_proposals}
                    onChange={(e) => patch({ hot_job_max_proposals: Number(e.target.value) })}
                  />
                </div>
                <div className="field" style={{ marginBottom: 0 }}>
                  <label>Posted within (hours)</label>
                  <input
                    type="number"
                    value={settings.hot_job_posted_hours}
                    onChange={(e) => patch({ hot_job_posted_hours: Number(e.target.value) })}
                  />
                </div>
              </div>
            </>
          )}

          <div className="form-row" style={{ marginTop: 16, marginBottom: 0 }}>
            <button className="btn" onClick={save}>
              Save settings
            </button>
            <button className="btn secondary" onClick={loadDigest}>
              Preview next digest
            </button>
            <button className="btn secondary" onClick={sendDigestNow} disabled={sendingDigest}>
              {sendingDigest ? 'Sending…' : 'Send digest now'}
            </button>
          </div>

          {digestResult && (
            <div className="info-banner" style={{ marginTop: 12, marginBottom: 0 }}>
              Digest sent: {digestResult.jobs_in_digest} job{digestResult.jobs_in_digest === 1 ? '' : 's'} in
              digest · {digestResult.emailed ? 'emailed' : 'not emailed (SMTP not configured)'}
            </div>
          )}

          {digest && (
            <>
              <div className="section-title">Digest preview ({digest.length} jobs)</div>
              {digest.length === 0 ? (
                <p className="muted">The next digest would be empty.</p>
              ) : (
                <div className="item-list">
                  {digest.map((j) => (
                    <div key={j.id} className="item-row" style={{ cursor: 'default' }}>
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                        {j.title}
                      </span>
                      <ScoreBadge score={j.quality_score} />
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      ) : (
        !error && <p className="muted">Loading settings…</p>
      )}

      <div className="panel">
        <h2>Live alert log</h2>
        {messages.length === 0 ? (
          <p className="muted">No messages received yet on /ws/alerts.</p>
        ) : (
          <div className="log">
            {messages.slice(0, 20).map((m, i) => (
              <div className="log-entry" key={i}>
                <span className="muted">
                  {new Date(m.receivedAt).toLocaleTimeString()}
                </span>{' '}
                <span
                  className={`log-type ${m.type === 'hot_job' || m.type === 'client_replied' ? 'hot' : ''}`}
                >
                  {m.type}
                </span>{' '}
                {m.type === 'client_replied' && 'proposal_id' in m
                  ? `proposal #${m.proposal_id} — ${'snippet' in m && m.snippet ? m.snippet : '(no snippet)'}`
                  : 'job' in m && m.job
                    ? (m.job as Job).title
                    : JSON.stringify(m)}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
