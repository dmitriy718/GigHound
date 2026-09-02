import { useEffect, useRef, useState } from 'react';
import {
  createPortfolioItem,
  createProfileTemplate,
  createRateCardEntry,
  deletePortfolioItem,
  deleteProfileTemplate,
  deleteRateCardEntry,
  getPortfolioItems,
  getProfileTemplates,
  getRateCard,
  updatePortfolioItem,
  updateProfileTemplate,
  updateRateCardEntry,
} from '../api/client';
import type { Platform, PortfolioItem, ProfileTemplate, RateCardEntry } from '../types';
import { PLATFORMS } from '../types';
import { ErrorBanner, Modal, TagInput } from '../components/common';

type Tab = 'templates' | 'portfolio' | 'ratecard';

export default function ProfileManager() {
  const [tab, setTab] = useState<Tab>('templates');
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    },
    [],
  );

  const flash = (msg: string) => {
    setNotice(msg);
    if (noticeTimer.current) window.clearTimeout(noticeTimer.current);
    noticeTimer.current = window.setTimeout(() => setNotice(null), 3000);
  };

  return (
    <div>
      <h1>Profile Manager</h1>
      <p className="page-sub">Platform profile templates, portfolio, and rate card.</p>
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      <div className="tabs">
        <button className={`tab ${tab === 'templates' ? 'active' : ''}`} onClick={() => setTab('templates')}>
          Profile templates
        </button>
        <button className={`tab ${tab === 'portfolio' ? 'active' : ''}`} onClick={() => setTab('portfolio')}>
          Portfolio
        </button>
        <button className={`tab ${tab === 'ratecard' ? 'active' : ''}`} onClick={() => setTab('ratecard')}>
          Rate card
        </button>
      </div>

      {tab === 'templates' && <TemplatesTab setError={setError} flash={flash} />}
      {tab === 'portfolio' && <PortfolioTab setError={setError} flash={flash} />}
      {tab === 'ratecard' && <RateCardTab setError={setError} flash={flash} />}
    </div>
  );
}

interface TabProps {
  setError: (e: string | null) => void;
  flash: (msg: string) => void;
}

function TemplatesTab({ setError, flash }: TabProps) {
  const [platform, setPlatform] = useState<Platform>('upwork');
  const [templates, setTemplates] = useState<ProfileTemplate[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [name, setName] = useState('');
  const [pitch, setPitch] = useState('');
  const [showForm, setShowForm] = useState(false);
  // snapshot taken when the form opens — backdrop close is blocked while dirty
  const initial = useRef({ name: '', pitch: '' });

  const load = (p: Platform) => {
    getProfileTemplates(p)
      .then((t) => {
        setTemplates(t);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(() => {
    load(platform);
    closeForm();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [platform]);

  const select = (t: ProfileTemplate) => {
    setSelectedId(t.id);
    setName(t.name);
    setPitch(t.pitch_template);
    initial.current = { name: t.name, pitch: t.pitch_template };
    setShowForm(true);
  };

  function closeForm() {
    setShowForm(false);
    setSelectedId(null);
    setName('');
    setPitch('');
    initial.current = { name: '', pitch: '' };
  }

  const save = async () => {
    try {
      const body = { platform, name, pitch_template: pitch };
      if (selectedId != null) {
        await updateProfileTemplate(selectedId, body);
      } else {
        await createProfileTemplate(body);
      }
      load(platform);
      closeForm();
      flash('Template saved.');
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async () => {
    if (selectedId == null) return;
    if (!window.confirm(`Delete template "${name}"?`)) return;
    try {
      await deleteProfileTemplate(selectedId);
      load(platform);
      closeForm();
      flash('Template deleted.');
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div>
      <div className="panel">
        <div className="form-row" style={{ alignItems: 'end' }}>
          <div className="field" style={{ marginBottom: 0, minWidth: 200 }}>
            <label>Platform</label>
            <select value={platform} onChange={(e) => setPlatform(e.target.value as Platform)}>
              {PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <button
            className="btn secondary small"
            onClick={() => {
              closeForm();
              setShowForm(true);
            }}
          >
            + New
          </button>
        </div>
        <div className="item-list" style={{ marginTop: 12 }}>
          {templates.length === 0 && <p className="muted">No templates for {platform}.</p>}
          {templates.map((t) => (
            <div
              key={t.id}
              className={`item-row ${selectedId === t.id ? 'active' : ''}`}
              onClick={() => select(t)}
            >
              {t.name}
            </div>
          ))}
        </div>
      </div>

      {showForm && (
        <Modal
          title={selectedId != null ? 'Edit template' : 'New template'}
          onClose={closeForm}
          dirty={name !== initial.current.name || pitch !== initial.current.pitch}
        >
          <div className="field">
            <label>Name</label>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="field">
            <label>Pitch template</label>
            <textarea
              rows={12}
              value={pitch}
              placeholder="Hi {{client_name}}, I noticed you need…"
              onChange={(e) => setPitch(e.target.value)}
            />
          </div>
          <div className="form-row" style={{ marginBottom: 0 }}>
            <button className="btn" onClick={save} disabled={!name.trim()}>
              {selectedId != null ? 'Save changes' : 'Create template'}
            </button>
            <button className="btn secondary" onClick={closeForm}>
              Cancel
            </button>
            {selectedId != null && (
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

function PortfolioTab({ setError, flash }: TabProps) {
  const [items, setItems] = useState<PortfolioItem[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [url, setUrl] = useState('');
  const [tags, setTags] = useState<string[]>([]);
  const [showForm, setShowForm] = useState(false);

  const load = () => {
    getPortfolioItems()
      .then((i) => {
        setItems(i);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const reset = () => {
    setEditingId(null);
    setTitle('');
    setDescription('');
    setUrl('');
    setTags([]);
    setShowForm(false);
  };

  const edit = (item: PortfolioItem) => {
    setEditingId(item.id);
    setTitle(item.title);
    setDescription(item.description);
    setUrl(item.url);
    setTags(item.tags);
    setShowForm(true);
  };

  const save = async () => {
    try {
      const body = { title, description, url, tags };
      if (editingId != null) {
        await updatePortfolioItem(editingId, body);
      } else {
        await createPortfolioItem(body);
      }
      load();
      reset();
      flash('Portfolio item saved.');
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (id: number) => {
    const item = items.find((i) => i.id === id);
    if (!window.confirm(`Delete portfolio item "${item?.title ?? `#${id}`}"?`)) return;
    try {
      await deletePortfolioItem(id);
      load();
      flash('Portfolio item deleted.');
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div>
      <div className="spread" style={{ marginBottom: 12 }}>
        <h2 style={{ margin: 0 }}>Portfolio items</h2>
        <button
          className="btn secondary small"
          onClick={() => {
            reset();
            setShowForm(true);
          }}
        >
          + New item
        </button>
      </div>

      {showForm && (
        <Modal title={editingId != null ? 'Edit item' : 'New item'} onClose={reset}>
          <div className="form-row">
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Title</label>
              <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>URL</label>
              <input type="url" value={url} onChange={(e) => setUrl(e.target.value)} />
            </div>
          </div>
          <div className="field">
            <label>Description</label>
            <textarea rows={4} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div className="field">
            <label>Tags</label>
            <TagInput tags={tags} onChange={setTags} />
          </div>
          <div className="form-row" style={{ marginBottom: 0 }}>
            <button className="btn" onClick={save} disabled={!title.trim()}>
              {editingId != null ? 'Save changes' : 'Create item'}
            </button>
            <button className="btn secondary" onClick={reset}>
              Cancel
            </button>
          </div>
        </Modal>
      )}

      <div className="card-grid">
        {items.length === 0 && <p className="muted">No portfolio items yet.</p>}
        {items.map((item) => (
          <div className="card" key={item.id}>
            <h3>{item.title}</h3>
            <p className="muted" style={{ margin: '0 0 8px' }}>
              {item.description}
            </p>
            {item.url && (
              <p style={{ margin: '0 0 8px' }}>
                <a href={item.url} target="_blank" rel="noreferrer" style={{ color: 'var(--accent)' }}>
                  {item.url}
                </a>
              </p>
            )}
            <div className="chips">
              {item.tags.map((t) => (
                <span className="chip" key={t}>
                  {t}
                </span>
              ))}
            </div>
            <div className="card-actions">
              <button className="btn secondary small" onClick={() => edit(item)}>
                Edit
              </button>
              <button className="btn danger small" onClick={() => remove(item.id)}>
                Delete
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function RateCardTab({ setError, flash }: TabProps) {
  const [entries, setEntries] = useState<RateCardEntry[]>([]);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [category, setCategory] = useState('');
  const [hourly, setHourly] = useState('');
  const [fixedMin, setFixedMin] = useState('');
  const [currency, setCurrency] = useState('USD');
  const [showForm, setShowForm] = useState(false);

  const load = () => {
    getRateCard()
      .then((r) => {
        setEntries(r);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, []);

  const reset = () => {
    setEditingId(null);
    setCategory('');
    setHourly('');
    setFixedMin('');
    setCurrency('USD');
    setShowForm(false);
  };

  const edit = (entry: RateCardEntry) => {
    setEditingId(entry.id);
    setCategory(entry.skill_category);
    setHourly(entry.hourly_rate != null ? String(entry.hourly_rate) : '');
    setFixedMin(entry.fixed_min != null ? String(entry.fixed_min) : '');
    setCurrency(entry.currency);
    setShowForm(true);
  };

  const numOrNull = (v: string) => (v === '' ? null : Number(v));

  const save = async () => {
    try {
      const body = {
        skill_category: category,
        hourly_rate: numOrNull(hourly),
        fixed_min: numOrNull(fixedMin),
        currency,
      };
      if (editingId != null) {
        await updateRateCardEntry(editingId, body);
      } else {
        await createRateCardEntry(body);
      }
      load();
      reset();
      flash('Rate card entry saved.');
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const remove = async (id: number) => {
    const entry = entries.find((e) => e.id === id);
    if (!window.confirm(`Delete rate card entry "${entry?.skill_category ?? `#${id}`}"?`)) return;
    try {
      await deleteRateCardEntry(id);
      load();
      flash('Rate card entry deleted.');
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div>
      <div className="panel">
        <div className="spread">
          <h2 style={{ margin: 0 }}>Rate card</h2>
          <button
            className="btn secondary small"
            onClick={() => {
              reset();
              setShowForm(true);
            }}
          >
            + Add entry
          </button>
        </div>
        {entries.length === 0 ? (
          <p className="muted">No rate card entries yet.</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Skill category</th>
                <th>Hourly rate</th>
                <th>Fixed min</th>
                <th>Currency</th>
                <th style={{ width: 140 }}></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => (
                <tr key={e.id}>
                  <td>{e.skill_category}</td>
                  <td>{e.hourly_rate != null ? e.hourly_rate : '—'}</td>
                  <td>{e.fixed_min != null ? e.fixed_min : '—'}</td>
                  <td>{e.currency}</td>
                  <td>
                    <button className="btn secondary small" onClick={() => edit(e)}>
                      Edit
                    </button>{' '}
                    <button className="btn danger small" onClick={() => remove(e.id)}>
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showForm && (
        <Modal title={editingId != null ? 'Edit entry' : 'Add entry'} onClose={reset}>
          <div className="form-row" style={{ alignItems: 'end', marginBottom: 0 }}>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Skill category</label>
              <input
                type="text"
                value={category}
                placeholder="e.g. React development"
                onChange={(e) => setCategory(e.target.value)}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Hourly rate</label>
              <input type="number" value={hourly} onChange={(e) => setHourly(e.target.value)} />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Fixed min</label>
              <input type="number" value={fixedMin} onChange={(e) => setFixedMin(e.target.value)} />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Currency</label>
              <input
                type="text"
                style={{ width: 80 }}
                value={currency}
                onChange={(e) => setCurrency(e.target.value)}
              />
            </div>
          </div>
          <div className="form-row" style={{ marginTop: 14, marginBottom: 0 }}>
            <button className="btn" onClick={save} disabled={!category.trim()}>
              {editingId != null ? 'Save' : 'Add'}
            </button>
            <button className="btn secondary" onClick={reset}>
              Cancel
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
