import { useState } from 'react';
import { getJobs, ingestJobs } from '../api/client';
import type { Job, JobIngest } from '../types';
import { SCORING_WEIGHTS } from '../types';
import { ErrorBanner, ScoreBadge, ScoreBars } from '../components/common';

export default function ScoringConfig() {
  const [description, setDescription] = useState('');
  const [budget, setBudget] = useState('');
  const [proposals, setProposals] = useState('');
  const [result, setResult] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const testScorer = async () => {
    setBusy(true);
    setResult(null);
    const externalId = `test-${Date.now()}`;
    const budgetNum = budget === '' ? null : Number(budget);
    const job: JobIngest = {
      external_id: externalId,
      platform: 'upwork',
      title: `Scorer test ${externalId}`,
      description,
      url: 'https://example.com/test-job',
      job_type: 'fixed',
      budget_min: budgetNum,
      budget_max: budgetNum,
      currency: 'USD',
      budget_usd_min: budgetNum,
      budget_usd_max: budgetNum,
      experience_level: null,
      client_info: {},
      proposals_count: proposals === '' ? null : Number(proposals),
      skills: [],
      languages: [],
      work_arrangement: 'remote',
      posted_at: new Date().toISOString(),
      apply_deadline: null,
    };
    try {
      await ingestJobs([job]);
      // Fetch the freshly ingested job back (ingest response has no job payload)
      const res = await getJobs({ platform: 'upwork', limit: 100 });
      const created = res.jobs.find((j) => j.external_id === externalId) ?? null;
      setResult(created);
      setError(created ? null : 'Job ingested but not found in feed (it may have been auto-archived).');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <h1>Scoring Config</h1>
      <p className="page-sub">
        How the server scores jobs (0–100), and a playground to test the scorer.
      </p>
      <ErrorBanner error={error} />

      <div className="panel">
        <h2>Scoring algorithm</h2>
        <p className="muted">
          quality_score = keyword_match + budget_realism + client_verification +
          description_quality + urgency_ratio − red_flag_penalty, clamped to 0–100. A negative
          keyword hit sets the score to 0 and excludes the job immediately.
        </p>
        <table className="data">
          <thead>
            <tr>
              <th>Component</th>
              <th style={{ width: 90 }}>Max points</th>
              <th>What it measures</th>
            </tr>
          </thead>
          <tbody>
            {SCORING_WEIGHTS.map((w) => (
              <tr key={w.component}>
                <td>
                  <code>{w.component}</code>
                </td>
                <td style={{ color: w.points < 0 ? 'var(--red)' : 'var(--green)' }}>
                  {w.points > 0 ? `+${w.points}` : `0 to ${w.points}`}
                </td>
                <td className="muted">{w.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>Test the scorer</h2>
        <div className="field">
          <label>Sample job description</label>
          <textarea
            rows={6}
            value={description}
            placeholder="Paste a job description here…"
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>
        <div className="form-row">
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Budget (USD)</label>
            <input type="number" value={budget} onChange={(e) => setBudget(e.target.value)} />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Proposals count</label>
            <input
              type="number"
              value={proposals}
              onChange={(e) => setProposals(e.target.value)}
            />
          </div>
          <div className="field" style={{ marginBottom: 0, justifyContent: 'flex-end' }}>
            <button
              className="btn"
              onClick={testScorer}
              disabled={busy || description.trim() === ''}
            >
              {busy ? 'Scoring…' : 'Score this job'}
            </button>
          </div>
        </div>

        {result && (
          <div style={{ marginTop: 16 }}>
            <div className="spread" style={{ marginBottom: 12 }}>
              <h2 style={{ margin: 0 }}>Result</h2>
              <ScoreBadge score={result.quality_score} />
            </div>
            <ScoreBars breakdown={result.score_breakdown} />
            {result.red_flags.length > 0 && (
              <>
                <h3>Red flags</h3>
                <div className="chips">
                  {result.red_flags.map((f) => (
                    <span className="chip flag" key={f}>
                      {f}
                    </span>
                  ))}
                </div>
              </>
            )}
            {result.status === 'archived' && (
              <p className="muted" style={{ marginTop: 8 }}>
                This job was auto-archived (below quality threshold or negative keyword hit).
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
