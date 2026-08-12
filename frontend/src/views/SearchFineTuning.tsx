import { useEffect, useState } from 'react';
import {
  createFilter,
  deleteFilter,
  getFilters,
  getKeywordGroups,
  previewFilter,
  updateFilter,
  type SearchFilterPayload,
} from '../api/client';
import type { Job, KeywordGroup, PlatformBudget, SearchFilter } from '../types';
import { EXPERIENCE_LEVELS, JOB_TYPES, PLATFORMS, WORK_ARRANGEMENTS } from '../types';
import { ErrorBanner, Modal, ScoreBadge, TagInput } from '../components/common';

const emptyPayload: SearchFilterPayload = {
  name: '',
  keyword_group_id: null,
  platforms: [],
  job_types: [],
  budgets: [],
  experience_levels: [],
  client_filters: {},
  posted_within_hours: null,
  apply_deadline_within_hours: null,
  work_arrangements: [],
  languages: [],
  max_proposals: null,
  quality_threshold: 50,
};

export default function SearchFineTuning() {
  const [filters, setFilters] = useState<SearchFilter[]>([]);
  const [groups, setGroups] = useState<KeywordGroup[]>([]);
  const [draft, setDraft] = useState<SearchFilterPayload>(emptyPayload);
  const [draftId, setDraftId] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [preview, setPreview] = useState<{ matched: Job[]; excluded_count: number } | null>(null);

  useEffect(() => {
    getFilters()
      .then(setFilters)
      .catch((e: Error) => setError(e.message));
    getKeywordGroups()
      .then(setGroups)
      .catch((e: Error) => setError(e.message));
  }, []);

  const toggleIn = <T,>(arr: T[], val: T): T[] =>
    arr.includes(val) ? arr.filter((v) => v !== val) : [...arr, val];

  const selectFilter = (f: SearchFilter) => {
    const { id, created_at: _created, ...rest } = f;
    setDraft({ ...emptyPayload, ...rest });
    setDraftId(id);
    setPreview(null);
    setShowForm(true);
  };

  const newFilter = () => {
    setDraft(emptyPayload);
    setDraftId(null);
    setPreview(null);
    setShowForm(true);
  };

  const closeForm = () => {
    setShowForm(false);
    setDraft(emptyPayload);
    setDraftId(null);
  };

  const save = async () => {
    try {
      if (draftId != null) {
        const updated = await updateFilter(draftId, draft);
        setFilters((prev) => prev.map((f) => (f.id === updated.id ? updated : f)));
      } else {
        const created = await createFilter(draft);
        setFilters((prev) => [...prev, created]);
      }
      closeForm();
      setNotice('Saved.');
      setError(null);
      window.setTimeout(() => setNotice(null), 3000);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async () => {
    if (draftId == null) return;
    try {
      await deleteFilter(draftId);
      setFilters((prev) => prev.filter((f) => f.id !== draftId));
      closeForm();
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const runPreview = async () => {
    if (draftId == null) {
      setError('Save the filter first — preview runs against a saved filter.');
      return;
    }
    try {
      setPreview(await previewFilter(draftId));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const budgetFor = (p: (typeof PLATFORMS)[number]): PlatformBudget =>
    draft.budgets.find((b) => b.platform === p) ?? { platform: p, min: null, max: null, currency: 'USD' };

  const setBudget = (updated: PlatformBudget) => {
    setDraft((d) => ({
      ...d,
      budgets: [...d.budgets.filter((b) => b.platform !== updated.platform), updated],
    }));
  };

  const cf = draft.client_filters;
  const setCf = (patch: Partial<typeof cf>) =>
    setDraft((d) => ({ ...d, client_filters: { ...d.client_filters, ...patch } }));

  const numOrNull = (v: string) => (v === '' ? null : Number(v));

  return (
    <div>
      <h1>Search Fine-Tuning</h1>
      <p className="page-sub">Manage search filter presets and preview what they match.</p>
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      <div className="panel">
        <div className="spread">
          <h2>Saved filters</h2>
          <button className="btn secondary small" onClick={newFilter}>
            + New
          </button>
        </div>
        <div className="item-list">
          {filters.length === 0 && <p className="muted">No filters yet.</p>}
          {filters.map((f) => (
            <div
              key={f.id}
              className={`item-row ${draftId === f.id ? 'active' : ''}`}
              onClick={() => selectFilter(f)}
            >
              <div>
                <div>{f.name}</div>
                <div className="muted" style={{ fontSize: 12 }}>
                  {f.platforms.join(', ') || 'all platforms'} · threshold {f.quality_threshold}
                </div>
              </div>
            </div>
          ))}
        </div>

        {preview && (
          <>
            <div className="section-title">
              Preview: {preview.matched.length} matched · {preview.excluded_count} excluded
            </div>
            <div className="item-list">
              {preview.matched.map((j) => (
                <div key={j.id} className="item-row" style={{ cursor: 'default' }}>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{j.title}</span>
                  <ScoreBadge score={j.quality_score} />
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      {showForm && (
        <Modal
          title={draftId != null ? 'Edit filter' : 'New filter'}
          onClose={closeForm}
        >

          <div className="form-row">
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Name</label>
              <input
                type="text"
                value={draft.name}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              />
            </div>
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Keyword group</label>
              <select
                value={draft.keyword_group_id ?? ''}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    keyword_group_id: e.target.value === '' ? null : Number(e.target.value),
                  })
                }
              >
                <option value="">None</option>
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="section-title">Platforms</div>
          <div className="checks">
            {PLATFORMS.map((p) => (
              <label className="checkbox-row" key={p}>
                <input
                  type="checkbox"
                  checked={draft.platforms.includes(p)}
                  onChange={() => setDraft({ ...draft, platforms: toggleIn(draft.platforms, p) })}
                />
                {p}
              </label>
            ))}
          </div>

          {draft.platforms.length > 0 && (
            <>
              <div className="section-title">Budget per platform</div>
              {draft.platforms.map((p) => {
                const b = budgetFor(p);
                return (
                  <div className="form-row" key={p} style={{ alignItems: 'center' }}>
                    <span style={{ width: 110 }}>{p}</span>
                    <input
                      type="number"
                      className="inline-input"
                      style={{ width: 90 }}
                      placeholder="min"
                      value={b.min ?? ''}
                      onChange={(e) => setBudget({ ...b, min: numOrNull(e.target.value) })}
                    />
                    <input
                      type="number"
                      className="inline-input"
                      style={{ width: 90 }}
                      placeholder="max"
                      value={b.max ?? ''}
                      onChange={(e) => setBudget({ ...b, max: numOrNull(e.target.value) })}
                    />
                    <input
                      type="text"
                      className="inline-input"
                      style={{ width: 70 }}
                      value={b.currency}
                      onChange={(e) => setBudget({ ...b, currency: e.target.value })}
                    />
                  </div>
                );
              })}
            </>
          )}

          <div className="section-title">Job types</div>
          <div className="checks">
            {JOB_TYPES.map((t) => (
              <label className="checkbox-row" key={t}>
                <input
                  type="checkbox"
                  checked={draft.job_types.includes(t)}
                  onChange={() => setDraft({ ...draft, job_types: toggleIn(draft.job_types, t) })}
                />
                {t}
              </label>
            ))}
          </div>

          <div className="section-title">Experience levels</div>
          <div className="checks">
            {EXPERIENCE_LEVELS.map((l) => (
              <label className="checkbox-row" key={l}>
                <input
                  type="checkbox"
                  checked={draft.experience_levels.includes(l)}
                  onChange={() =>
                    setDraft({ ...draft, experience_levels: toggleIn(draft.experience_levels, l) })
                  }
                />
                {l}
              </label>
            ))}
          </div>

          <div className="section-title">Client filters</div>
          <div className="form-row">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={cf.payment_verified === true}
                onChange={(e) => setCf({ payment_verified: e.target.checked ? true : null })}
              />
              Payment verified only
            </label>
          </div>
          <div className="form-row">
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Min hire rate (%)</label>
              <input
                type="number"
                value={cf.min_hire_rate ?? ''}
                onChange={(e) => setCf({ min_hire_rate: numOrNull(e.target.value) })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Min total spent (USD)</label>
              <input
                type="number"
                value={cf.min_total_spent ?? ''}
                onChange={(e) => setCf({ min_total_spent: numOrNull(e.target.value) })}
              />
            </div>
          </div>
          <div className="field">
            <label>Countries (ISO codes)</label>
            <TagInput
              tags={cf.countries ?? []}
              placeholder="e.g. US, GB…"
              onChange={(countries) => setCf({ countries })}
            />
          </div>

          <div className="section-title">Timing & saturation</div>
          <div className="form-row">
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Posted within (hours)</label>
              <input
                type="number"
                value={draft.posted_within_hours ?? ''}
                onChange={(e) => setDraft({ ...draft, posted_within_hours: numOrNull(e.target.value) })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Apply deadline within (hours)</label>
              <input
                type="number"
                value={draft.apply_deadline_within_hours ?? ''}
                onChange={(e) =>
                  setDraft({ ...draft, apply_deadline_within_hours: numOrNull(e.target.value) })
                }
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Max proposals</label>
              <input
                type="number"
                value={draft.max_proposals ?? ''}
                onChange={(e) => setDraft({ ...draft, max_proposals: numOrNull(e.target.value) })}
              />
            </div>
          </div>

          <div className="section-title">Work arrangements</div>
          <div className="checks">
            {WORK_ARRANGEMENTS.map((w) => (
              <label className="checkbox-row" key={w}>
                <input
                  type="checkbox"
                  checked={draft.work_arrangements.includes(w)}
                  onChange={() =>
                    setDraft({ ...draft, work_arrangements: toggleIn(draft.work_arrangements, w) })
                  }
                />
                {w}
              </label>
            ))}
          </div>

          <div className="field" style={{ marginTop: 12 }}>
            <label>Languages</label>
            <TagInput
              tags={draft.languages}
              placeholder="e.g. English…"
              onChange={(languages) => setDraft({ ...draft, languages })}
            />
          </div>

          <div className="field">
            <label>
              Quality threshold (auto-archive below): <strong>{draft.quality_threshold}</strong>
            </label>
            <input
              type="range"
              min={0}
              max={100}
              value={draft.quality_threshold}
              onChange={(e) => setDraft({ ...draft, quality_threshold: Number(e.target.value) })}
            />
          </div>

          <div className="form-row" style={{ marginBottom: 0 }}>
            <button className="btn" onClick={save} disabled={!draft.name.trim()}>
              {draftId != null ? 'Save changes' : 'Create filter'}
            </button>
            <button className="btn secondary" onClick={runPreview} disabled={draftId == null}>
              Preview
            </button>
            <button className="btn secondary" onClick={closeForm}>
              Cancel
            </button>
            {draftId != null && (
              <button className="btn danger" onClick={remove}>
                Delete
              </button>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
