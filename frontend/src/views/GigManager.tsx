import { useEffect, useState } from 'react';
import {
  ApiError,
  createGigFromTemplate,
  createGigTemplate,
  deleteGigTemplate,
  generateFaqs,
  getCompetitors,
  getFiverrTaxonomy,
  getGigMetrics,
  getGigTemplates,
  getGigs,
  registerGig,
  seoTitleScore,
  toggleGigTemplate,
  triggerGigScrape,
  updateGigTemplate,
} from '../api/client';
import type {
  CompetitorSnapshot,
  Gig,
  GigMetric,
  GigPricingTier,
  GigStatus,
  GigTemplate,
  GigTemplateJson,
  Platform,
} from '../types';
import { GIG_STATUSES, PLATFORMS } from '../types';
import { ErrorBanner, TagInput } from '../components/common';

type Tab = 'gigs' | 'templates' | 'competitors';

const STATUS_COLORS: Record<GigStatus, string> = {
  draft: 'var(--text-dim)',
  active: 'var(--green)',
  paused: 'var(--amber)',
};

// Pull {detail: {validation: [...]}} out of a 422 ApiError, if present
function validationErrors(e: unknown): string[] {
  if (e instanceof ApiError && e.status === 422) {
    const detail = e.detail as { detail?: { validation?: unknown } } | undefined;
    const v = detail?.detail?.validation;
    if (Array.isArray(v)) return v.map(String);
  }
  return [];
}

// ---------------------------------------------------------------- Gigs tab

const SERIES: { key: keyof Pick<GigMetric, 'impressions' | 'clicks' | 'orders'>; color: string }[] = [
  { key: 'impressions', color: 'var(--accent)' },
  { key: 'clicks', color: 'var(--amber)' },
  { key: 'orders', color: 'var(--green)' },
];

function MetricsChart({ metrics }: { metrics: GigMetric[] }) {
  const W = 640;
  const H = 200;
  const PAD = 28;
  const max = Math.max(1, ...metrics.flatMap((m) => [m.impressions, m.clicks, m.orders]));
  const x = (i: number) =>
    metrics.length === 1 ? W / 2 : PAD + (i / (metrics.length - 1)) * (W - PAD * 2);
  const y = (v: number) => H - PAD - (v / max) * (H - PAD * 2);

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="metrics-chart" role="img" aria-label="Gig metrics">
        <line x1={PAD} y1={H - PAD} x2={W - PAD} y2={H - PAD} stroke="var(--border)" />
        <line x1={PAD} y1={PAD} x2={PAD} y2={H - PAD} stroke="var(--border)" />
        {SERIES.map(({ key, color }) => (
          <polyline
            key={key}
            fill="none"
            stroke={color}
            strokeWidth={2}
            points={metrics.map((m, i) => `${x(i)},${y(m[key])}`).join(' ')}
          />
        ))}
        {metrics.map((m, i) => (
          <text key={m.week} x={x(i)} y={H - PAD + 14} textAnchor="middle" className="chart-label">
            {m.week}
          </text>
        ))}
      </svg>
      <div className="chips" style={{ marginTop: 6 }}>
        {SERIES.map(({ key, color }) => (
          <span className="chip" key={key} style={{ color }}>
            ● {key}
          </span>
        ))}
        <span className="muted" style={{ fontSize: 12, alignSelf: 'center' }}>
          peak {max}
        </span>
      </div>
    </div>
  );
}

function GigsTab() {
  const [gigs, setGigs] = useState<Gig[]>([]);
  const [platform, setPlatform] = useState<Platform | ''>('');
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [selected, setSelected] = useState<Gig | null>(null);
  const [metrics, setMetrics] = useState<GigMetric[]>([]);
  const [metricsError, setMetricsError] = useState<string | null>(null);
  const [form, setForm] = useState({
    platform: 'fiverr' as Platform,
    title: '',
    url: '',
    price_min: '',
    status: 'draft' as GigStatus,
  });

  const load = () => {
    getGigs(platform || undefined)
      .then((g) => {
        setGigs(g);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, [platform]);

  const openMetrics = (gig: Gig) => {
    setSelected(gig);
    setMetrics([]);
    setMetricsError(null);
    getGigMetrics(gig.id)
      .then(setMetrics)
      .catch((e: Error) => setMetricsError(e.message));
  };

  const scrape = () => {
    triggerGigScrape()
      .then((res) => setInfo(`Weekly scrape enqueued${res.enqueued != null ? ` (${res.enqueued} gigs)` : ''}.`))
      .catch((e: Error) => setError(e.message));
  };

  const register = () => {
    registerGig({
      platform: form.platform,
      title: form.title.trim(),
      external_id: null,
      url: form.url.trim(),
      status: form.status,
      price_min: form.price_min !== '' ? Number(form.price_min) : null,
      template_id: null,
    })
      .then(() => {
        setForm({ ...form, title: '', url: '', price_min: '' });
        setError(null);
        load();
      })
      .catch((e: Error) => setError(e.message));
  };

  const latest = metrics.length > 0 ? metrics[metrics.length - 1] : null;

  return (
    <div>
      <ErrorBanner error={error} />
      {info && <div className="info-banner">{info}</div>}

      <div className="filters-bar">
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
        <button className="btn secondary" onClick={load}>
          Refresh
        </button>
        <button className="btn secondary" onClick={scrape}>
          Trigger weekly scrape
        </button>
      </div>

      <div className="panel">
        <table className="data">
          <thead>
            <tr>
              <th>Title</th>
              <th>Platform</th>
              <th>Status</th>
              <th>Price min</th>
              <th>URL</th>
            </tr>
          </thead>
          <tbody>
            {gigs.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No gigs tracked yet.
                </td>
              </tr>
            )}
            {gigs.map((g) => (
              <tr
                key={g.id}
                onClick={() => openMetrics(g)}
                style={{ cursor: 'pointer', background: selected?.id === g.id ? 'var(--bg-elevated)' : undefined }}
              >
                <td>{g.title}</td>
                <td>{g.platform}</td>
                <td>
                  <span className="pill" style={{ color: STATUS_COLORS[g.status] }}>
                    {g.status}
                  </span>
                </td>
                <td>{g.price_min != null ? `$${g.price_min}` : '—'}</td>
                <td>
                  {g.url ? (
                    <a
                      href={g.url}
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: 'var(--accent)' }}
                      onClick={(e) => e.stopPropagation()}
                    >
                      open ↗
                    </a>
                  ) : (
                    '—'
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Metrics — {selected.title}</h2>
          <ErrorBanner error={metricsError} />
          {metrics.length === 0 && !metricsError && (
            <p className="muted">No metrics yet — trigger a weekly scrape.</p>
          )}
          {metrics.length > 0 && <MetricsChart metrics={metrics} />}
          {latest && latest.suggestions.length > 0 && (
            <>
              <h3>Suggestions (latest week)</h3>
              <ul className="muted" style={{ margin: '4px 0', paddingLeft: 18 }}>
                {latest.suggestions.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Register gig</h2>
        <div className="form-row">
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Platform</label>
            <select
              value={form.platform}
              onChange={(e) => setForm({ ...form, platform: e.target.value as Platform })}
            >
              {PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
            <label>Title</label>
            <input
              type="text"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
            />
          </div>
          <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 200 }}>
            <label>URL</label>
            <input
              type="url"
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
            />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Price min</label>
            <input
              type="number"
              value={form.price_min}
              onChange={(e) => setForm({ ...form, price_min: e.target.value })}
            />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Status</label>
            <select
              value={form.status}
              onChange={(e) => setForm({ ...form, status: e.target.value as GigStatus })}
            >
              {GIG_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <button
            className="btn"
            disabled={!form.title.trim() || !form.url.trim()}
            onClick={register}
          >
            Register
          </button>
        </div>
      </div>
    </div>
  );
}

// -------------------------------------------------------- Template Builder

type TierName = 'basic' | 'standard' | 'premium';
const TIERS: TierName[] = ['basic', 'standard', 'premium'];

interface EditorState {
  id: number | null;
  platform: Platform;
  name: string;
  auto_publish: boolean;
  title: string;
  category: string;
  subcategory: string;
  tags: string[];
  pricing: Record<TierName, GigPricingTier>;
  description: { hook: string; what_you_get: string; why_me: string; cta: string };
  faqs: { question: string; answer: string }[];
}

const emptyTier = (): GigPricingTier => ({ price: null, delivery_days: null, revisions: null });

const emptyEditor = (): EditorState => ({
  id: null,
  platform: 'fiverr',
  name: '',
  auto_publish: false,
  title: '',
  category: '',
  subcategory: '',
  tags: [],
  pricing: { basic: emptyTier(), standard: emptyTier(), premium: emptyTier() },
  description: { hook: '', what_you_get: '', why_me: '', cta: '' },
  faqs: [],
});

const editorFromTemplate = (t: GigTemplate): EditorState => {
  const j = t.template_json;
  return {
    id: t.id,
    platform: t.platform,
    name: t.name,
    auto_publish: t.auto_publish,
    title: j.title ?? '',
    category: j.category ?? '',
    subcategory: j.subcategory ?? '',
    tags: j.tags ?? [],
    pricing: {
      basic: j.pricing?.basic ?? emptyTier(),
      standard: j.pricing?.standard ?? emptyTier(),
      premium: j.pricing?.premium ?? emptyTier(),
    },
    description: {
      hook: j.description?.hook ?? '',
      what_you_get: j.description?.what_you_get ?? '',
      why_me: j.description?.why_me ?? '',
      cta: j.description?.cta ?? '',
    },
    faqs: j.faqs ?? [],
  };
};

function TemplatesTab() {
  const [templates, setTemplates] = useState<GigTemplate[]>([]);
  const [platformFilter, setPlatformFilter] = useState<Platform | ''>('');
  const [taxonomy, setTaxonomy] = useState<Record<string, string[]>>({});
  const [editor, setEditor] = useState<EditorState>(emptyEditor());
  const [seo, setSeo] = useState<{ score: number; issues: string[] } | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [createResult, setCreateResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => {
    getGigTemplates(platformFilter || undefined)
      .then((t) => {
        setTemplates(t);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(load, [platformFilter]);

  useEffect(() => {
    getFiverrTaxonomy()
      .then((t) => setTaxonomy(t.categories))
      .catch(() => setTaxonomy({}));
  }, []);

  const patch = (p: Partial<EditorState>) => setEditor((prev) => ({ ...prev, ...p }));
  const patchTier = (tier: TierName, p: Partial<GigPricingTier>) =>
    setEditor((prev) => ({ ...prev, pricing: { ...prev.pricing, [tier]: { ...prev.pricing[tier], ...p } } }));
  const patchDesc = (key: keyof EditorState['description'], value: string) =>
    setEditor((prev) => ({ ...prev, description: { ...prev.description, [key]: value } }));

  const checkSeo = () => {
    seoTitleScore(editor.title, editor.tags)
      .then(setSeo)
      .catch((e: Error) => setError(e.message));
  };

  const genFaqs = () => {
    generateFaqs(editor.category || 'gig', editor.title, 3)
      .then((res) => patch({ faqs: res.faqs }))
      .catch((e: Error) => setError(e.message));
  };

  const save = () => {
    setBusy(true);
    setErrors([]);
    setCreateResult(null);
    const template_json: Partial<GigTemplateJson> = {
      title: editor.title,
      category: editor.category,
      subcategory: editor.subcategory,
      tags: editor.tags,
      pricing: editor.pricing,
      description: editor.description,
      faqs: editor.faqs,
    };
    const body = {
      platform: editor.platform,
      name: editor.name.trim(),
      template_json,
      auto_publish: editor.auto_publish,
    };
    const req = editor.id != null ? updateGigTemplate(editor.id, body) : createGigTemplate(body);
    req
      .then((saved) => {
        setEditor(editorFromTemplate(saved));
        setError(null);
        load();
      })
      .catch((e: unknown) => {
        const v = validationErrors(e);
        if (v.length > 0) setErrors(v);
        else setError((e as Error).message);
      })
      .finally(() => setBusy(false));
  };

  const remove = (t: GigTemplate) => {
    deleteGigTemplate(t.id)
      .then(() => {
        if (editor.id === t.id) setEditor(emptyEditor());
        load();
      })
      .catch((e: Error) => setError(e.message));
  };

  const toggle = (t: GigTemplate) => {
    toggleGigTemplate(t.id)
      .then(() => load())
      .catch((e: Error) => setError(e.message));
  };

  const createGig = () => {
    if (editor.id == null) return;
    setCreateResult(null);
    setErrors([]);
    createGigFromTemplate(editor.id)
      .then((res) => setCreateResult(`Stealth task #${res.stealth_task_id} queued — ${res.note}`))
      .catch((e: unknown) => {
        // 429 when circuit open or rate-limited (1 draft/hour/platform) — show server text
        setCreateResult(null);
        setError((e as Error).message);
      });
  };

  const subcats = taxonomy[editor.category] ?? [];

  return (
    <div className="grid-2" style={{ gridTemplateColumns: 'minmax(220px, 1fr) 2fr' }}>
      <div>
        <div className="field">
          <label>Platform</label>
          <select
            value={platformFilter}
            onChange={(e) => setPlatformFilter(e.target.value as Platform | '')}
          >
            <option value="">All</option>
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
        <div className="item-list">
          {templates.map((t) => (
            <div
              key={t.id}
              className={`item-row ${editor.id === t.id ? 'active' : ''}`}
              onClick={() => {
                setEditor(editorFromTemplate(t));
                setSeo(null);
                setErrors([]);
                setCreateResult(null);
              }}
            >
              <span>
                {t.name}
                <span className="muted" style={{ fontSize: 12 }}>
                  {' '}
                  · {t.platform}
                </span>
              </span>
              <span style={{ display: 'flex', gap: 6 }} onClick={(e) => e.stopPropagation()}>
                <button className="btn small secondary" onClick={() => toggle(t)}>
                  {t.active ? 'Deactivate' : 'Activate'}
                </button>
                <button className="btn small danger" onClick={() => remove(t)}>
                  Delete
                </button>
              </span>
            </div>
          ))}
          {templates.length === 0 && <p className="muted">No templates yet.</p>}
        </div>
        <button
          className="btn secondary"
          style={{ marginTop: 10 }}
          onClick={() => {
            setEditor(emptyEditor());
            setSeo(null);
            setErrors([]);
            setCreateResult(null);
          }}
        >
          New template
        </button>
      </div>

      <div className="panel" style={{ marginBottom: 0 }}>
        <h2 style={{ marginTop: 0 }}>
          {editor.id != null ? `Edit template #${editor.id}` : 'New template'}
        </h2>
        <ErrorBanner error={error} />
        {errors.length > 0 && (
          <div className="error-banner">
            <strong>Validation failed</strong>
            <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
              {errors.map((v) => (
                <li key={v}>{v}</li>
              ))}
            </ul>
          </div>
        )}
        {createResult && <div className="info-banner">{createResult}</div>}

        <div className="form-row">
          <div className="field" style={{ marginBottom: 0, flex: 1, minWidth: 180 }}>
            <label>Template name</label>
            <input type="text" value={editor.name} onChange={(e) => patch({ name: e.target.value })} />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Platform</label>
            <select value={editor.platform} onChange={(e) => patch({ platform: e.target.value as Platform })}>
              {PLATFORMS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="field">
          <label>
            Gig title{' '}
            <span style={{ color: editor.title.length > 80 ? 'var(--red)' : undefined }}>
              {editor.title.length}/80
            </span>
          </label>
          <input type="text" value={editor.title} onChange={(e) => patch({ title: e.target.value })} />
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 4 }}>
            <button
              className="btn small secondary"
              disabled={!editor.title.trim()}
              onClick={checkSeo}
            >
              Check SEO
            </button>
            {seo && (
              <span
                className="muted"
                style={{ fontSize: 12, color: seo.score >= 70 ? 'var(--green)' : seo.score >= 40 ? 'var(--amber)' : 'var(--red)' }}
              >
                SEO score {seo.score}/100
              </span>
            )}
          </div>
          {seo && seo.issues.length > 0 && (
            <ul className="muted" style={{ margin: '4px 0 0', paddingLeft: 18, fontSize: 12 }}>
              {seo.issues.map((i) => (
                <li key={i}>{i}</li>
              ))}
            </ul>
          )}
        </div>

        <div className="form-row">
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Category</label>
            <select
              value={editor.category}
              onChange={(e) => patch({ category: e.target.value, subcategory: '' })}
            >
              <option value="">—</option>
              {Object.keys(taxonomy).map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Subcategory</label>
            <select
              value={editor.subcategory}
              disabled={subcats.length === 0}
              onChange={(e) => patch({ subcategory: e.target.value })}
            >
              <option value="">—</option>
              {subcats.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="field">
          <label>Search tags (max 5)</label>
          <TagInput
            tags={editor.tags}
            onChange={(tags) => patch({ tags: tags.slice(0, 5) })}
            placeholder="Add tag…"
          />
        </div>

        <h3>Pricing tiers</h3>
        <div className="form-row">
          {TIERS.map((tier) => (
            <div key={tier} style={{ flex: 1, minWidth: 150 }}>
              <div className="section-title" style={{ margin: '0 0 6px' }}>
                {tier}
              </div>
              <div className="field" style={{ marginBottom: 6 }}>
                <label>Price</label>
                <input
                  type="number"
                  value={editor.pricing[tier].price ?? ''}
                  onChange={(e) =>
                    patchTier(tier, { price: e.target.value === '' ? null : Number(e.target.value) })
                  }
                />
              </div>
              <div className="field" style={{ marginBottom: 6 }}>
                <label>Delivery days</label>
                <input
                  type="number"
                  value={editor.pricing[tier].delivery_days ?? ''}
                  onChange={(e) =>
                    patchTier(tier, {
                      delivery_days: e.target.value === '' ? null : Number(e.target.value),
                    })
                  }
                />
              </div>
              <div className="field" style={{ marginBottom: 0 }}>
                <label>Revisions</label>
                <input
                  type="number"
                  value={editor.pricing[tier].revisions ?? ''}
                  onChange={(e) =>
                    patchTier(tier, {
                      revisions: e.target.value === '' ? null : Number(e.target.value),
                    })
                  }
                />
              </div>
            </div>
          ))}
        </div>

        <h3>Description</h3>
        {(
          [
            ['hook', 'Hook'],
            ['what_you_get', 'What you get'],
            ['why_me', 'Why me'],
            ['cta', 'Call to action'],
          ] as const
        ).map(([key, label]) => (
          <div className="field" key={key}>
            <label>{label}</label>
            <textarea
              rows={3}
              value={editor.description[key]}
              onChange={(e) => patchDesc(key, e.target.value)}
            />
          </div>
        ))}

        <div className="spread">
          <h3 style={{ margin: 0 }}>FAQs</h3>
          <button
            className="btn small secondary"
            disabled={!editor.title.trim()}
            onClick={genFaqs}
          >
            Generate FAQs
          </button>
        </div>
        {editor.faqs.length > 0 && (
          <div className="item-list" style={{ marginTop: 8 }}>
            {editor.faqs.map((f, i) => (
              <div className="item-row" key={`${f.question}-${i}`} style={{ cursor: 'default', alignItems: 'flex-start' }}>
                <span style={{ fontSize: 13 }}>
                  <strong>Q: {f.question}</strong>
                  <br />
                  <span className="muted">A: {f.answer}</span>
                </span>
                <button
                  className="btn small danger"
                  onClick={() => patch({ faqs: editor.faqs.filter((_, j) => j !== i) })}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}

        <label className="checkbox-row" style={{ marginTop: 12 }}>
          <input
            type="checkbox"
            checked={editor.auto_publish}
            onChange={(e) => patch({ auto_publish: e.target.checked })}
          />
          Auto-publish after creation
          <span className="muted" style={{ fontSize: 12 }}>
            (Upwork only — Fiverr always saves as draft)
          </span>
        </label>

        <div className="form-row" style={{ marginTop: 12, marginBottom: 0 }}>
          <button className="btn" disabled={busy || !editor.name.trim()} onClick={save}>
            {busy ? 'Saving…' : editor.id != null ? 'Save changes' : 'Create template'}
          </button>
          {editor.id != null && (
            <button className="btn secondary" onClick={createGig}>
              Create Gig from Template
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------ Competitor Intel

function CompetitorsTab() {
  const [platform, setPlatform] = useState<Platform>('fiverr');
  const [category, setCategory] = useState('');
  const [snapshots, setSnapshots] = useState<CompetitorSnapshot[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = () => {
    setLoaded(false);
    getCompetitors(platform, category.trim())
      .then((s) => {
        setSnapshots(s);
        setLoaded(true);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  return (
    <div>
      <ErrorBanner error={error} />
      <div className="filters-bar">
        <div className="field" style={{ marginBottom: 0 }}>
          <label>Platform</label>
          <select value={platform} onChange={(e) => setPlatform(e.target.value as Platform)}>
            {PLATFORMS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>
        <div className="field" style={{ marginBottom: 0, minWidth: 220 }}>
          <label>Category</label>
          <input
            type="text"
            value={category}
            placeholder="e.g. Web Development"
            onChange={(e) => setCategory(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && category.trim()) load();
            }}
          />
        </div>
        <button className="btn" disabled={!category.trim()} onClick={load}>
          Load snapshots
        </button>
      </div>

      {loaded && snapshots.length === 0 && (
        <p className="muted">No competitor snapshots for this platform/category yet.</p>
      )}
      <div className="card-grid">
        {snapshots.map((s) => (
          <div className="card" key={s.date}>
            <h3>{s.date}</h3>
            {s.gigs.length > 0 && (
              <table className="data">
                <thead>
                  <tr>
                    <th>Top gig</th>
                    <th>Price</th>
                  </tr>
                </thead>
                <tbody>
                  {s.gigs.map((g, i) => (
                    <tr key={`${g.title}-${i}`}>
                      <td>{g.title}</td>
                      <td>{g.price != null ? `$${g.price}` : '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {s.insights.length > 0 && (
              <>
                <div className="section-title">Insights</div>
                <ul className="muted" style={{ margin: '4px 0 0', paddingLeft: 18, fontSize: 13 }}>
                  {s.insights.map((ins) => (
                    <li key={ins}>{ins}</li>
                  ))}
                </ul>
              </>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- View

export default function GigManager() {
  const [tab, setTab] = useState<Tab>('gigs');

  return (
    <div>
      <h1>Gig Manager</h1>
      <p className="page-sub">Seller mode — tracked gigs, Fiverr template builder, competitor intel</p>
      <div className="tabs">
        {(
          [
            ['gigs', 'Gigs'],
            ['templates', 'Template Builder'],
            ['competitors', 'Competitor Intel'],
          ] as const
        ).map(([key, label]) => (
          <button
            key={key}
            className={`tab ${tab === key ? 'active' : ''}`}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === 'gigs' && <GigsTab />}
      {tab === 'templates' && <TemplatesTab />}
      {tab === 'competitors' && <CompetitorsTab />}
    </div>
  );
}
