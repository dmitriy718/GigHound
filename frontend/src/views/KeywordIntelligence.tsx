import { useEffect, useRef, useState } from 'react';
import {
  createKeywordGroup,
  deleteKeywordGroup,
  getKeywordGroups,
  suggestSkills,
  updateKeywordGroup,
} from '../api/client';
import type { Keyword, KeywordGroup, KeywordKind, Platform } from '../types';
import { PLATFORMS } from '../types';
import { ErrorBanner, Modal, TagInput } from '../components/common';

interface Draft {
  id: number | null;
  name: string;
  service_type: string;
  keywords: Keyword[];
}

const emptyDraft: Draft = { id: null, name: '', service_type: '', keywords: [] };

export default function KeywordIntelligence() {
  const [groups, setGroups] = useState<KeywordGroup[]>([]);
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [suggestPlatform, setSuggestPlatform] = useState<Platform>('upwork');
  const [suggestQuery, setSuggestQuery] = useState('');
  const [suggestions, setSuggestions] = useState<string[]>([]);
  // snapshot taken when the form opens — backdrop close is blocked while dirty
  const initialDraft = useRef<Draft>(emptyDraft);

  const load = () => {
    getKeywordGroups()
      .then((g) => {
        setGroups(g);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const keywordsOf = (kind: KeywordKind) => draft.keywords.filter((k) => k.kind === kind);

  const setKindTerms = (kind: 'secondary' | 'negative', terms: string[]) => {
    setDraft((d) => ({
      ...d,
      keywords: [
        ...d.keywords.filter((k) => k.kind !== kind),
        ...terms.map((term) => ({ term, kind, weight: 0 })),
      ],
    }));
  };

  const addPrimary = (term: string, weight = 0.8) => {
    const t = term.trim();
    if (!t) return;
    setDraft((d) =>
      d.keywords.some((k) => k.term === t && k.kind === 'primary')
        ? d
        : { ...d, keywords: [...d.keywords, { term: t, kind: 'primary', weight }] },
    );
  };

  const removeKeyword = (idx: number) =>
    setDraft((d) => ({ ...d, keywords: d.keywords.filter((_, i) => i !== idx) }));

  const setWeight = (idx: number, weight: number) =>
    setDraft((d) => ({
      ...d,
      keywords: d.keywords.map((k, i) => (i === idx ? { ...k, weight } : k)),
    }));

  const selectGroup = (g: KeywordGroup) => {
    const next = { id: g.id, name: g.name, service_type: g.service_type, keywords: g.keywords };
    initialDraft.current = next;
    setDraft(next);
    setShowForm(true);
  };

  const newGroup = () => {
    initialDraft.current = emptyDraft;
    setDraft(emptyDraft);
    setShowForm(true);
  };

  const closeForm = () => {
    setShowForm(false);
    setDraft(emptyDraft);
  };

  const save = async () => {
    const body = { name: draft.name, service_type: draft.service_type, keywords: draft.keywords };
    try {
      if (draft.id != null) {
        const updated = await updateKeywordGroup(draft.id, body);
        setGroups((prev) => prev.map((g) => (g.id === updated.id ? updated : g)));
      } else {
        const created = await createKeywordGroup(body);
        setGroups((prev) => [...prev, created]);
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
    if (draft.id == null) return;
    if (!window.confirm(`Delete keyword group "${draft.name}"? Matching and scoring lose these terms.`)) {
      return;
    }
    try {
      await deleteKeywordGroup(draft.id);
      setGroups((prev) => prev.filter((g) => g.id !== draft.id));
      closeForm();
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const suggest = async () => {
    try {
      const res = await suggestSkills(suggestPlatform, suggestQuery);
      setSuggestions(res.suggestions);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const primaryKeywords = keywordsOf('primary');

  return (
    <div>
      <h1>Keyword Intelligence</h1>
      <p className="page-sub">Manage keyword groups used for matching and scoring.</p>
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      <div className="panel">
        <div className="spread">
          <h2>Saved groups</h2>
          <button className="btn secondary small" onClick={newGroup}>
            + New
          </button>
        </div>
        <div className="item-list">
          {groups.length === 0 && <p className="muted">No groups yet.</p>}
          {groups.map((g) => (
            <div
              key={g.id}
              className={`item-row ${draft.id === g.id ? 'active' : ''}`}
              onClick={() => selectGroup(g)}
            >
              <div>
                <div>{g.name}</div>
                <div className="muted" style={{ fontSize: 12 }}>
                  {g.service_type} · {g.keywords.length} keywords
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {showForm && (
        <Modal
          title={draft.id != null ? 'Edit group' : 'New group'}
          onClose={closeForm}
          dirty={JSON.stringify(draft) !== JSON.stringify(initialDraft.current)}
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
              <label>Service type</label>
              <input
                type="text"
                value={draft.service_type}
                placeholder="e.g. web-development"
                onChange={(e) => setDraft({ ...draft, service_type: e.target.value })}
              />
            </div>
          </div>

          <div className="section-title">Primary keywords (term + weight)</div>
          {primaryKeywords.map((k) => {
            const idx = draft.keywords.indexOf(k);
            return (
              <div className="kw-row" key={k.term}>
                <span style={{ width: 180 }}>{k.term}</span>
                <input
                  type="range"
                  min={0}
                  max={1}
                  step={0.05}
                  value={k.weight}
                  style={{ flex: 1 }}
                  onChange={(e) => setWeight(idx, Number(e.target.value))}
                />
                <span className="weight-val">{k.weight.toFixed(2)}</span>
                <button className="btn danger small" onClick={() => removeKeyword(idx)}>
                  ×
                </button>
              </div>
            );
          })}
          <TagInput
            tags={[]}
            placeholder="Add primary keyword, press Enter…"
            onChange={(tags) => {
              const last = tags[tags.length - 1];
              if (last) addPrimary(last);
            }}
          />

          <div className="section-title">Suggest skills</div>
          <div className="form-row">
            <select
              value={suggestPlatform}
              onChange={(e) => setSuggestPlatform(e.target.value as Platform)}
              className="inline-select"
            >
              {PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
            <input
              type="text"
              className="inline-input"
              style={{ flex: 1 }}
              placeholder="Search skill taxonomy, e.g. rea"
              value={suggestQuery}
              onChange={(e) => setSuggestQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && suggest()}
            />
            <button className="btn secondary" onClick={suggest}>
              Suggest skills
            </button>
          </div>
          {suggestions.length > 0 && (
            <div className="suggestions">
              {suggestions.map((s) => (
                <button key={s} className="suggestion" onClick={() => addPrimary(s)}>
                  + {s}
                </button>
              ))}
            </div>
          )}

          <div className="section-title">Secondary keywords</div>
          <TagInput
            tags={keywordsOf('secondary').map((k) => k.term)}
            onChange={(terms) => setKindTerms('secondary', terms)}
          />

          <div className="section-title">Negative keywords (instant exclusion)</div>
          <TagInput
            tags={keywordsOf('negative').map((k) => k.term)}
            onChange={(terms) => setKindTerms('negative', terms)}
          />

          <div className="form-row" style={{ marginTop: 16, marginBottom: 0 }}>
            <button className="btn" onClick={save} disabled={!draft.name.trim()}>
              {draft.id != null ? 'Save changes' : 'Create group'}
            </button>
            <button className="btn secondary" onClick={closeForm}>
              Cancel
            </button>
            {draft.id != null && (
              <button className="btn danger" onClick={remove}>
                Delete group
              </button>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
