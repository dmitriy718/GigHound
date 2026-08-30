import { useState, type FormEvent } from 'react';
import { ApiError, login, register } from '../api/client';
import type { User } from '../types';

interface Props {
  onAuthed: (token: string, user: User) => void;
}

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail;
    if (d && typeof d === 'object' && 'detail' in d) {
      const detail = (d as { detail: unknown }).detail;
      if (typeof detail === 'string') return detail;
      if (Array.isArray(detail)) {
        return detail
          .map((item) => (item && typeof item === 'object' && 'msg' in item ? String((item as { msg: unknown }).msg) : ''))
          .filter(Boolean)
          .join(' ');
      }
    }
    if (e.status === 0) return e.message;
    return `Request failed (${e.status})`;
  }
  return 'Something went wrong';
}

export default function Login({ onAuthed }: Props) {
  const [mode, setMode] = useState<'signin' | 'register'>('signin');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      const res =
        mode === 'signin'
          ? await login({ email, password })
          : await register({ email, password, display_name: displayName });
      onAuthed(res.access_token, res.user);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-wrap">
      <div className="card auth-card">
        <div className="brand auth-brand">
          <span className="brand-mark">GH</span>
          <div className="brand-word">
            <span className="brand-name">GigHound</span>
            <span className="brand-tag">autonomous gig hunting</span>
          </div>
        </div>

        <div className="tabs auth-tabs">
          <button
            className={`tab ${mode === 'signin' ? 'active' : ''}`}
            onClick={() => { setMode('signin'); setError(null); }}
          >
            Sign in
          </button>
          <button
            className={`tab ${mode === 'register' ? 'active' : ''}`}
            onClick={() => { setMode('register'); setError(null); }}
          >
            Create account
          </button>
        </div>

        {error && <div className="error-banner">{error}</div>}

        <form onSubmit={submit}>
          {mode === 'register' && (
            <div className="field">
              <label htmlFor="auth-name">Display name</label>
              <input
                id="auth-name"
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                autoComplete="name"
              />
            </div>
          )}
          <div className="field">
            <label htmlFor="auth-email">Email</label>
            <input
              id="auth-email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              autoComplete="email"
            />
          </div>
          <div className="field">
            <label htmlFor="auth-password">Password</label>
            <input
              id="auth-password"
              type="password"
              required
              minLength={mode === 'register' ? 8 : undefined}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
            />
          </div>
          <button className="btn" type="submit" disabled={busy}>
            {busy ? 'Working…' : mode === 'signin' ? 'Sign in' : 'Create account'}
          </button>
        </form>

        {import.meta.env.DEV && (
          <p className="muted auth-hint">
            Dev mode: register any account (password ≥ 8 chars), or use the seeded
            dev@gighound.local / dev-noauth.
          </p>
        )}
      </div>
    </div>
  );
}
