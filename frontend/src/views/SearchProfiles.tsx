import { useEffect, useRef, useState } from 'react';
import {
  createSearchProfile,
  deleteSearchProfile,
  getFilters,
  getKeywordGroups,
  getSearchProfiles,
  runSearchProfileNow,
  updateSearchProfile,
  validateBooleanQuery,
  type SearchProfilePayload,
} from '../api/client';
import type { KeywordGroup, SearchFilter, SearchProfile } from '../types';
import { ErrorBanner, Modal } from '../components/common';

const emptyPayload: SearchProfilePayload = {
  name: '',
  keyword_group_id: null,
  filter_id: null,
  boolean_query: '',
  auto_queue_proposals: false,
};

const PRESETS: { label: string; name: string; query: string }[] = [
  { label: 'React Full-Stack', name: 'React Full-Stack', query: '(React OR Next.js) AND (NOT WordPress)' },
  { label: 'DevOps/AWS', name: 'DevOps/AWS', query: '(DevOps OR AWS OR Kubernetes) AND (NOT WordPress)' },
  { label: 'AI/ML Python', name: 'AI/ML Python', query: '(Python AND (ML OR "machine learning" OR LLM)) AND (NOT scraping)' },
];

const INSERTS = ['(', ')', ' AND ', ' OR ', ' NOT ', '"'];

type Validation = { state: 'idle' | 'checking' | 'valid' | 'invalid'; error?: string };

export default function SearchProfiles() {
  const [profiles, setProfiles] = useState<SearchProfile[]>([]);
  const [groups, setGroups] = useState<KeywordGroup[]>([]);
  const [filters, setFilters] = useState<SearchFilter[]>([]);
  const [draft, setDraft] = useState<SearchProfilePayload>(emptyPayload);
  const [draftId, setDraftId] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [validation, setValidation] = useState<Validation>({ state: 'idle' });
  const [runningId, setRunningId] = useState<number | null>(null);
  const queryRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    getSearchProfiles()
      .then(setProfiles)
      .catch((e: Error) => setError(e.message));
    getKeywordGroups()
      .then(setGroups)
      .catch((e: Error) => setError(e.message));
    getFilters()
      .then(setFilters)
      .catch((e: Error) => setError(e.message));
  }, []);

  // Debounced live validation of the boolean query
  useEffect(() => {
    const query = draft.boolean_query.trim();
    if (!query) {
      setValidation({ state: 'idle' });
      return;
    }
    setValidation({ state: 'checking' });
    const timer = window.setTimeout(() => {
      validateBooleanQuery(query)
        .then((res) =>
          setValidation(res.valid ? { state: 'valid' } : { state: 'invalid', error: res.error }),
        )
        .catch((e: Error) => setValidation({ state: 'invalid', error: e.message }));
    }, 400);
    return () => window.clearTimeout(timer);
  }, [draft.boolean_query]);

  const selectProfile = (p: SearchProfile) => {
    const { id, created_at: _created, ...rest } = p;
    setDraft({ ...emptyPayload, ...rest });
    setDraftId(id);
    setShowForm(true);
  };

  const newProfile = () => {
    setDraft(emptyPayload);
    setDraftId(null);
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
        const updated = await updateSearchProfile(draftId, draft);
        setProfiles((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
      } else {
        const created = await createSearchProfile(draft);
        setProfiles((prev) => [...prev, created]);
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
      await deleteSearchProfile(draftId);
      setProfiles((prev) => prev.filter((p) => p.id !== draftId));
      closeForm();
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  // Insert a token at the cursor and restore focus/selection after it
  const insertAtCursor = (token: string) => {
    const el = queryRef.current;
    const query = draft.boolean_query;
    if (!el) {
      setDraft({ ...draft, boolean_query: query + token });
      return;
    }
    const start = el.selectionStart ?? query.length;
    const end = el.selectionEnd ?? query.length;
    const next = query.slice(0, start) + token + query.slice(end);
    setDraft({ ...draft, boolean_query: next });
    requestAnimationFrame(() => {
      el.focus();
      el.setSelectionRange(start + token.length, start + token.length);
    });
  };

  const applyPreset = (preset: (typeof PRESETS)[number]) => {
    setDraft({ ...draft, name: preset.name, boolean_query: preset.query });
  };

  const runNow = async (p: SearchProfile) => {
    setRunningId(p.id);
    try {
      const res = await runSearchProfileNow(p.id);
      setNotice(
        res.queued
          ? `Search "${p.name}" queued for: ${res.platforms.join(', ') || 'no platforms'}`
          : `Search "${p.name}" was not queued.`,
      );
      setError(null);
      window.setTimeout(() => setNotice(null), 5000);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRunningId(null);
    }
  };

  return (
    <div>
      <h1>Search Profiles</h1>
      <p className="page-sub">
        Named boolean queries wired to keyword groups and filters · auto-queue drafts proposals for
        matches
      </p>
      <p className="muted" style={{ fontSize: 12, marginTop: -18, marginBottom: 24 }}>
        How discovery searches: the positive terms of the boolean query become the search keywords
        (falling back to the linked keyword group's primary terms, then the profile name). Saved
        profiles are searched automatically on the discovery schedule (every ~15 minutes).
      </p>
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      <div className="panel">
        <div className="spread">
          <h2>Saved profiles</h2>
          <button className="btn secondary small" onClick={newProfile}>
            + New
          </button>
        </div>
        <div className="item-list">
          {profiles.length === 0 && <p className="muted">No search profiles yet.</p>}
          {profiles.map((p) => (
            <div
              key={p.id}
              className={`item-row ${draftId === p.id ? 'active' : ''}`}
              onClick={() => selectProfile(p)}
            >
              <div>
                <div>{p.name}</div>
                <div className="muted" style={{ fontSize: 12 }}>
                  {p.boolean_query || 'no query'}
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {p.auto_queue_proposals && <span className="pill">auto-queue</span>}
                <button
                  className="btn secondary small"
                  disabled={runningId === p.id}
                  onClick={(e) => {
                    e.stopPropagation();
                    void runNow(p);
                  }}
                >
                  {runningId === p.id ? 'Queueing…' : 'Run search now'}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {showForm && (
        <Modal
          title={draftId != null ? 'Edit profile' : 'New profile'}
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
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Filter</label>
              <select
                value={draft.filter_id ?? ''}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    filter_id: e.target.value === '' ? null : Number(e.target.value),
                  })
                }
              >
                <option value="">None</option>
                {filters.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="section-title">Boolean query</div>
          <div className="chips" style={{ marginBottom: 8 }}>
            {PRESETS.map((preset) => (
              <span
                className="suggestion"
                key={preset.label}
                onClick={() => applyPreset(preset)}
              >
                {preset.label}
              </span>
            ))}
          </div>
          <div className="field">
            <input
              ref={queryRef}
              type="text"
              value={draft.boolean_query}
              placeholder='(React OR Next.js) AND (NOT WordPress)'
              onChange={(e) => setDraft({ ...draft, boolean_query: e.target.value })}
            />
          </div>
          <div className="chips" style={{ marginBottom: 8 }}>
            {INSERTS.map((token) => (
              <button
                type="button"
                className="btn secondary small"
                key={token}
                onClick={() => insertAtCursor(token)}
              >
              {token.trim() || token}
              </button>
            ))}
          </div>
          {validation.state === 'checking' && (
            <p className="muted" style={{ margin: '0 0 8px' }}>
              Validating…
            </p>
          )}
          {validation.state === 'valid' && (
            <p style={{ color: 'var(--green)', margin: '0 0 8px' }}>✓ Query is valid</p>
          )}
          {validation.state === 'invalid' && (
            <p style={{ color: 'var(--red)', margin: '0 0 8px' }}>
              ✗ {validation.error ?? 'Invalid query'}
            </p>
          )}

          <div className="form-row">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={draft.auto_queue_proposals}
                onChange={(e) => setDraft({ ...draft, auto_queue_proposals: e.target.checked })}
              />
              Auto-queue proposals for matching jobs
            </label>
          </div>

          <div className="form-row" style={{ marginBottom: 0 }}>
            <button className="btn" onClick={save} disabled={!draft.name.trim()}>
              {draftId != null ? 'Save changes' : 'Create profile'}
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
