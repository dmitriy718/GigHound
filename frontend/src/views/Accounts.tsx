import { useEffect, useState } from 'react';
import {
  createAccount,
  deleteAccount,
  getAccounts,
  updateAccount,
  type PlatformAccountPayload,
} from '../api/client';
import type { AccountMode, Platform, PlatformAccount } from '../types';
import { ACCOUNT_MODES, PLATFORMS } from '../types';
import { ErrorBanner, formatDate, Modal } from '../components/common';

const emptyPayload: PlatformAccountPayload = {
  platform: 'upwork',
  label: '',
  principal: '',
  mode: 'api',
  enabled: true,
  credential_ref: '',
};

export default function Accounts() {
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [draft, setDraft] = useState<PlatformAccountPayload>(emptyPayload);
  const [draftId, setDraftId] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    getAccounts()
      .then(setAccounts)
      .catch((e: Error) => setError(e.message));
  }, []);

  const selectAccount = (a: PlatformAccount) => {
    const { id, created_at: _created, ...rest } = a;
    setDraft({ ...emptyPayload, ...rest });
    setDraftId(id);
    setShowForm(true);
  };

  const newAccount = () => {
    setDraft(emptyPayload);
    setDraftId(null);
    setShowForm(true);
  };

  const closeForm = () => {
    setShowForm(false);
    setDraftId(null);
    setDraft(emptyPayload);
  };

  const save = async () => {
    try {
      if (draftId != null) {
        const updated = await updateAccount(draftId, draft);
        setAccounts((prev) => prev.map((a) => (a.id === updated.id ? updated : a)));
      } else {
        const created = await createAccount(draft);
        setAccounts((prev) => [...prev, created]);
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
      await deleteAccount(draftId);
      setAccounts((prev) => prev.filter((a) => a.id !== draftId));
      setDraft(emptyPayload);
      setDraftId(null);
      setShowForm(false);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div>
      <h1>Platform Accounts</h1>
      <p className="page-sub">
        Marketplace identities the orchestrator acts through. Secrets live in the credential vault —
        <code>credential_ref</code> only points at a vault entry, never store credentials here.
      </p>
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      <div className="panel">
        <div className="spread">
          <h2>Accounts</h2>
          <button className="btn secondary small" onClick={newAccount}>
            + Add account
          </button>
        </div>
        {accounts.length === 0 ? (
          <p className="muted">No platform accounts configured.</p>
        ) : (
          <table className="data">
            <thead>
              <tr>
                <th>Platform</th>
                <th>Label</th>
                <th>Principal</th>
                <th>Mode</th>
                <th>Enabled</th>
                <th>Credential ref</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => (
                <tr
                  key={a.id}
                  onClick={() => selectAccount(a)}
                  style={{
                    cursor: 'pointer',
                    background:
                      draftId === a.id ? 'rgba(139, 92, 246, 0.12)' : undefined,
                  }}
                >
                  <td>{a.platform}</td>
                  <td>{a.label}</td>
                  <td className="muted">{a.principal}</td>
                  <td>{a.mode}</td>
                  <td style={{ color: a.enabled ? 'var(--green)' : 'var(--red)' }}>
                    {a.enabled ? 'yes' : 'no'}
                  </td>
                  <td className="muted">{a.credential_ref || '—'}</td>
                  <td className="muted">{formatDate(a.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {showForm && (
        <Modal
          title={draftId != null ? 'Edit account' : 'New account'}
          onClose={closeForm}
        >
          <div className="form-row">
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Platform</label>
              <select
                value={draft.platform}
                onChange={(e) => setDraft({ ...draft, platform: e.target.value as Platform })}
              >
                {PLATFORMS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Label</label>
              <input
                type="text"
                value={draft.label}
                placeholder="e.g. Main Upwork agency"
                onChange={(e) => setDraft({ ...draft, label: e.target.value })}
              />
            </div>
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Principal</label>
              <input
                type="text"
                value={draft.principal}
                placeholder="e.g. agency name or account owner"
                onChange={(e) => setDraft({ ...draft, principal: e.target.value })}
              />
            </div>
          </div>
          <div className="form-row">
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Mode</label>
              <select
                value={draft.mode}
                onChange={(e) => setDraft({ ...draft, mode: e.target.value as AccountMode })}
              >
                {ACCOUNT_MODES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Credential ref (vault pointer)</label>
              <input
                type="text"
                value={draft.credential_ref}
                placeholder="e.g. vault://upwork/main"
                onChange={(e) => setDraft({ ...draft, credential_ref: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0, justifyContent: 'flex-end' }}>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={draft.enabled}
                  onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })}
                />
                Enabled
              </label>
            </div>
          </div>
          <div className="form-row" style={{ marginBottom: 0 }}>
            <button className="btn" onClick={save} disabled={!draft.label.trim()}>
              {draftId != null ? 'Save changes' : 'Create account'}
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
