import { useState, type FormEvent } from 'react';
import { ApiError, changePassword, deleteMyAccount } from '../api/client';
import { ErrorBanner, Modal } from './common';

function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail;
    if (d && typeof d === 'object' && 'detail' in d) {
      const detail = (d as { detail: unknown }).detail;
      if (typeof detail === 'string') return detail;
    }
    if (e.status === 0) return e.message;
    return `Request failed (${e.status})`;
  }
  return 'Something went wrong';
}

export function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next.length < 8) {
      setError('New password must be at least 8 characters.');
      return;
    }
    if (next !== confirm) {
      setError('New passwords do not match.');
      return;
    }
    setError(null);
    setBusy(true);
    try {
      await changePassword({ current_password: current, new_password: next });
      setDone(true);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Change password" onClose={onClose}>
      {done ? (
        <div>
          <p className="muted">
            Password updated. Your current session stays signed in; use the new password next time
            you log in.
          </p>
          <button className="btn" onClick={onClose}>
            Done
          </button>
        </div>
      ) : (
        <form onSubmit={submit}>
          <ErrorBanner error={error} />
          <div className="field">
            <label>Current password</label>
            <input
              type="password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              autoComplete="current-password"
              required
            />
          </div>
          <div className="field">
            <label>New password</label>
            <input
              type="password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              autoComplete="new-password"
              placeholder="at least 8 characters"
              required
            />
          </div>
          <div className="field">
            <label>Confirm new password</label>
            <input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
              required
            />
          </div>
          <button className="btn" type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Update password'}
          </button>
        </form>
      )}
    </Modal>
  );
}

interface DeleteAccountModalProps {
  email: string;
  onDeleted: () => void;
  onClose: () => void;
}

export function DeleteAccountModal({ email, onDeleted, onClose }: DeleteAccountModalProps) {
  const [password, setPassword] = useState('');
  const [confirmEmail, setConfirmEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await deleteMyAccount({ password });
      onDeleted();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <Modal title="Delete account" onClose={onClose}>
      <form onSubmit={submit}>
        <ErrorBanner error={error} />
        <p className="muted">
          This permanently deletes your account and all of its data — jobs, proposals, gigs,
          templates, credentials. There is no undo.
        </p>
        <div className="field">
          <label>Type your account email ({email}) to confirm</label>
          <input
            type="text"
            value={confirmEmail}
            onChange={(e) => setConfirmEmail(e.target.value)}
            placeholder={email}
            autoComplete="off"
            required
          />
        </div>
        <div className="field">
          <label>Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </div>
        <button
          className="btn danger"
          type="submit"
          disabled={busy || confirmEmail.trim().toLowerCase() !== email.toLowerCase() || !password}
        >
          {busy ? 'Deleting…' : 'Delete my account'}
        </button>
      </form>
    </Modal>
  );
}
