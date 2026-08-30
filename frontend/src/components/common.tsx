import { Component, useState } from 'react';
import type { ReactNode } from 'react';
import type { ScoreBreakdown } from '../types';

export function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error-banner">{error}</div>;
}

interface ErrorBoundaryProps {
  children: ReactNode;
  label?: string; // panel name shown in the fallback
}

interface ErrorBoundaryState {
  error: Error | null;
}

// Keeps one crashing panel from blanking the whole view.
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  render() {
    const { error } = this.state;
    if (error) {
      return (
        <div className="error-banner">
          {this.props.label ?? 'This panel'} crashed: {error.message}
          <button
            className="btn small secondary"
            style={{ marginLeft: 10 }}
            onClick={() => this.setState({ error: null })}
          >
            Retry
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

interface ModalProps {
  title: string;
  onClose: () => void;
  children: ReactNode;
}

export function Modal({ title, onClose, children }: ModalProps) {
  return (
    <>
      <div className="modal-backdrop" onClick={onClose} />
      <div className="modal" role="dialog" aria-modal="true" aria-label={title}>
        <div className="spread" style={{ marginBottom: 14 }}>
          <h2 style={{ margin: 0 }}>{title}</h2>
          <button className="btn secondary small" onClick={onClose}>
            Close
          </button>
        </div>
        {children}
      </div>
    </>
  );
}

export function scoreClass(score: number): string {
  if (score >= 75) return 'score-high';
  if (score >= 50) return 'score-mid';
  return 'score-low';
}

export function ScoreBadge({ score }: { score: number }) {
  return <span className={`score-badge ${scoreClass(score)}`}>{Math.round(score)}</span>;
}

const BAR_MAX: Record<string, number> = {
  keyword_match: 25,
  budget_realism: 25,
  client_verification: 20,
  description_quality: 20,
  urgency_ratio: 10,
  red_flag_penalty: 60,
};

export function ScoreBars({ breakdown }: { breakdown: ScoreBreakdown }) {
  const entries = Object.entries(breakdown);
  if (entries.length === 0) return <p className="muted">No breakdown available.</p>;
  return (
    <div>
      {entries.map(([key, value]) => {
        const max = BAR_MAX[key] ?? 40;
        const negative = key.includes('penalty') || value < 0;
        const pct = Math.min(100, (Math.abs(value) / max) * 100);
        return (
          <div className="score-bar-row" key={key}>
            <span className="label">{key.replace(/_/g, ' ')}</span>
            <div className="bar-track">
              <div
                className={`bar-fill ${negative ? 'negative' : ''}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="muted" style={{ textAlign: 'right', fontSize: 12 }}>
              {value}
            </span>
          </div>
        );
      })}
    </div>
  );
}

interface TagInputProps {
  tags: string[];
  onChange: (tags: string[]) => void;
  placeholder?: string;
}

export function TagInput({ tags, onChange, placeholder }: TagInputProps) {
  const [draft, setDraft] = useState('');

  const add = () => {
    const t = draft.trim();
    if (t && !tags.includes(t)) onChange([...tags, t]);
    setDraft('');
  };

  return (
    <div className="tag-input">
      {tags.map((tag) => (
        <span className="chip" key={tag}>
          {tag}
          <button type="button" onClick={() => onChange(tags.filter((t) => t !== tag))}>
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder={placeholder ?? 'Add…'}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            add();
          } else if (e.key === 'Backspace' && draft === '' && tags.length > 0) {
            onChange(tags.slice(0, -1));
          }
        }}
        onBlur={add}
      />
    </div>
  );
}

export function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}
