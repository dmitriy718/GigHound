import { useEffect, useState } from 'react';
import {
  completeFreelancerOAuth,
  createAccount,
  deleteAccount,
  deleteCredentials,
  enrollCredentials,
  getAccounts,
  getCredentialStatus,
  startFreelancerOAuth,
  updateAccount,
  type PlatformAccountPayload,
} from '../api/client';
import type { AccountMode, CredentialStatus, Platform, PlatformAccount } from '../types';
import { ACCOUNT_MODES, PLATFORMS } from '../types';
import { ErrorBanner, formatDate, Modal } from '../components/common';

// Token-based platforms enroll access_token (+ optional refresh_token);
// stealth platforms enroll a Playwright storage_state JSON or username+password fallback.
// Upwork is in BOTH sets (API tokens OR a browser session — the worker drives it via browser).
const TOKEN_PLATFORMS: Platform[] = ['freelancer', 'upwork'];
const STEALTH_PLATFORMS: Platform[] = ['fiverr', 'peopleperhour', 'guru', 'upwork'];
const ENROLLABLE_PLATFORMS: Platform[] = [...new Set([...TOKEN_PLATFORMS, ...STEALTH_PLATFORMS])];

// FastAPI error bodies are {detail: string} — prefer that over the raw status line
const apiMessage = (e: unknown): string => {
  const detail = (e as { detail?: { detail?: string } })?.detail?.detail;
  return detail ?? (e as Error).message;
};

const emptyPayload: PlatformAccountPayload = {
  platform: 'upwork',
  label: '',
  principal: '',
  mode: 'api',
  enabled: true,
  credential_ref: '',
  settings: {},
};

// Inline row warnings: these settings keys are required before proposals can be submitted
function missingSettingsWarning(a: PlatformAccount): string | null {
  if (a.platform === 'freelancer' && typeof a.settings.bidder_id !== 'number') {
    return 'missing bidder_id';
  }
  if (a.platform === 'upwork' && typeof a.settings.on_behalf_of !== 'string') {
    return 'missing on_behalf_of';
  }
  return null;
}

interface CredentialsPanelProps {
  account: PlatformAccount;
  onChanged: () => void; // credential_ref may be auto-set/cleared — parent reloads accounts
  onClose: () => void;
}

function CredentialsPanel({ account, onChanged, onClose }: CredentialsPanelProps) {
  const [status, setStatus] = useState<CredentialStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // token platforms
  const [accessToken, setAccessToken] = useState('');
  const [refreshToken, setRefreshToken] = useState('');
  // stealth platforms
  const [credForm, setCredForm] = useState<'token' | 'storage_state' | 'userpass'>('storage_state');
  const [storageJson, setStorageJson] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  // freelancer OAuth
  const [oauthStarted, setOauthStarted] = useState(false);
  const [oauthCode, setOauthCode] = useState('');

  const isTokenPlatform = TOKEN_PLATFORMS.includes(account.platform);
  const isStealthPlatform = STEALTH_PLATFORMS.includes(account.platform);
  const isDualPlatform = isTokenPlatform && isStealthPlatform;
  const showTokenForm = isTokenPlatform && (!isDualPlatform || credForm === 'token');

  const loadStatus = () => {
    getCredentialStatus(account.id)
      .then((s) => {
        setStatus(s);
        setError(null);
      })
      .catch((e) => setError(apiMessage(e)));
  };

  useEffect(loadStatus, [account.id]);

  const flash = (msg: string) => {
    setNotice(msg);
    window.setTimeout(() => setNotice(null), 4000);
  };

  const storageJsonError = (): string | null => {
    if (!storageJson.trim()) return 'Paste the Playwright storage_state JSON first.';
    try {
      const parsed: unknown = JSON.parse(storageJson);
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return 'storage_state must be a JSON object.';
      }
      return null;
    } catch {
      return 'Not valid JSON.';
    }
  };

  const enroll = async () => {
    let secrets: Record<string, string>;
    if (showTokenForm) {
      if (!accessToken.trim()) {
        setError('access_token is required.');
        return;
      }
      secrets = { access_token: accessToken.trim() };
      if (refreshToken.trim()) secrets.refresh_token = refreshToken.trim();
    } else if (credForm === 'storage_state') {
      const jsonErr = storageJsonError();
      if (jsonErr) {
        setError(jsonErr);
        return;
      }
      secrets = { storage_state_json: storageJson.trim() };
    } else {
      if (!username.trim() || !password) {
        setError('Both username and password are required.');
        return;
      }
      secrets = { username: username.trim(), password };
    }
    setBusy(true);
    try {
      await enrollCredentials(account.id, secrets);
      setAccessToken('');
      setRefreshToken('');
      setStorageJson('');
      setUsername('');
      setPassword('');
      setError(null);
      flash('Credentials enrolled — stored in the vault, never shown again.');
      loadStatus();
      onChanged();
    } catch (e) {
      setError(apiMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm(`Delete enrolled credentials for "${account.label}"? The account will stop working until re-enrolled.`)) {
      return;
    }
    setBusy(true);
    try {
      await deleteCredentials(account.id);
      setError(null);
      flash('Credentials deleted.');
      loadStatus();
      onChanged();
    } catch (e) {
      setError(apiMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const oauthStart = async () => {
    setBusy(true);
    try {
      const { authorize_url } = await startFreelancerOAuth(account.id);
      window.open(authorize_url, '_blank', 'noopener');
      setOauthStarted(true);
      setError(null);
    } catch (e) {
      // 501 when OAuth is not configured on the deployment — say so plainly
      setError(apiMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const oauthComplete = async () => {
    if (!oauthCode.trim()) {
      setError('Paste the authorization code first.');
      return;
    }
    setBusy(true);
    try {
      await completeFreelancerOAuth(account.id, oauthCode.trim());
      setOauthCode('');
      setOauthStarted(false);
      setError(null);
      flash('OAuth tokens enrolled.');
      loadStatus();
      onChanged();
    } catch (e) {
      setError(apiMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="spread">
        <h2>
          Credentials — {account.label} <span className="muted">({account.platform})</span>
        </h2>
        <button className="btn secondary small" onClick={onClose}>
          Close
        </button>
      </div>
      <ErrorBanner error={error} />
      {notice && <div className="info-banner">{notice}</div>}

      <p style={{ marginTop: 0 }}>
        {status === null ? (
          <span className="muted">Checking status…</span>
        ) : status.enrolled ? (
          <span className="pill" style={{ color: 'var(--green)' }}>
            Enrolled — keys: {status.keys.join(', ')}
            {status.updated_at ? ` · updated ${formatDate(status.updated_at)}` : ''}
          </span>
        ) : (
          <span className="pill" style={{ color: 'var(--amber)' }}>
            Not enrolled
          </span>
        )}
      </p>

      {!ENROLLABLE_PLATFORMS.includes(account.platform) ? (
        <p className="muted" style={{ fontSize: 12 }}>
          Credential enrollment is not supported for {account.platform} — no worker serves this
          platform.
        </p>
      ) : (
        <>
          {isDualPlatform && (
            <div className="form-row" style={{ marginBottom: 8 }}>
              <label className="checkbox-row">
                <input
                  type="radio"
                  checked={credForm === 'token'}
                  onChange={() => setCredForm('token')}
                />
                Access token (API)
              </label>
              <label className="checkbox-row">
                <input
                  type="radio"
                  checked={credForm === 'storage_state'}
                  onChange={() => setCredForm('storage_state')}
                />
                Browser session (storage_state JSON)
              </label>
              <label className="checkbox-row">
                <input
                  type="radio"
                  checked={credForm === 'userpass'}
                  onChange={() => setCredForm('userpass')}
                />
                Username + password
              </label>
            </div>
          )}
          {showTokenForm ? (
        <>
          <div className="form-row">
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Access token</label>
              <input
                type="password"
                value={accessToken}
                placeholder="required"
                autoComplete="off"
                onChange={(e) => setAccessToken(e.target.value)}
              />
            </div>
            <div className="field" style={{ flex: 1, marginBottom: 0 }}>
              <label>Refresh token (optional)</label>
              <input
                type="password"
                value={refreshToken}
                autoComplete="off"
                onChange={(e) => setRefreshToken(e.target.value)}
              />
            </div>
          </div>
          {account.platform === 'freelancer' && (
            <>
              <div className="form-row" style={{ marginBottom: 0 }}>
                <button className="btn secondary small" disabled={busy} onClick={oauthStart}>
                  Connect with OAuth
                </button>
              </div>
              {oauthStarted && (
                <div className="form-row" style={{ marginTop: 8, marginBottom: 0 }}>
                  <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                    <label>Authorization code</label>
                    <input
                      type="text"
                      value={oauthCode}
                      placeholder="Paste the code from the Freelancer redirect"
                      onChange={(e) => setOauthCode(e.target.value)}
                    />
                  </div>
                  <button
                    className="btn small"
                    style={{ alignSelf: 'flex-end' }}
                    disabled={busy}
                    onClick={oauthComplete}
                  >
                    Complete OAuth
                  </button>
                </div>
              )}
            </>
          )}
        </>
      ) : (
        <>
          {!isDualPlatform && (
            <div className="form-row" style={{ marginBottom: 8 }}>
              <label className="checkbox-row">
                <input
                  type="radio"
                  checked={credForm === 'storage_state'}
                  onChange={() => setCredForm('storage_state')}
                />
                Browser session (storage_state JSON)
              </label>
              <label className="checkbox-row">
                <input
                  type="radio"
                  checked={credForm === 'userpass'}
                  onChange={() => setCredForm('userpass')}
                />
                Username + password
              </label>
            </div>
          )}
          {credForm === 'storage_state' ? (
            <div className="field">
              <label>Playwright storage_state JSON</label>
              <textarea
                rows={5}
                value={storageJson}
                placeholder='{"cookies": [...], "origins": [...]}'
                onChange={(e) => setStorageJson(e.target.value)}
              />
              <span className="muted" style={{ fontSize: 11 }}>
                Export from a logged-in browser session (or the worker login flow).
              </span>
            </div>
          ) : (
            <>
              <div className="form-row" style={{ marginBottom: 8 }}>
                <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                  <label>Username</label>
                  <input
                    type="text"
                    value={username}
                    autoComplete="off"
                    onChange={(e) => setUsername(e.target.value)}
                  />
                </div>
                <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                  <label>Password</label>
                  <input
                    type="password"
                    value={password}
                    autoComplete="new-password"
                    onChange={(e) => setPassword(e.target.value)}
                  />
                </div>
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
                Fallback only — password login is challenge-prone (CAPTCHA/2FA) and may escalate
                to a human. Prefer a storage_state session when you have one.
              </p>
            </>
          )}
        </>
      )}
        </>
      )}

      <div className="form-row" style={{ marginBottom: 0 }}>
        {ENROLLABLE_PLATFORMS.includes(account.platform) && (
          <button className="btn" disabled={busy} onClick={enroll}>
            {busy ? 'Working…' : 'Enroll credentials'}
          </button>
        )}
        {status?.enrolled && (
          <button className="btn danger" disabled={busy} onClick={remove}>
            Delete credentials
          </button>
        )}
      </div>
    </div>
  );
}

export default function Accounts() {
  const [accounts, setAccounts] = useState<PlatformAccount[]>([]);
  const [draft, setDraft] = useState<PlatformAccountPayload>(emptyPayload);
  const [draftId, setDraftId] = useState<number | null>(null);
  const [showForm, setShowForm] = useState(false);
  const [credAccountId, setCredAccountId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reloadAccounts = () => {
    getAccounts()
      .then(setAccounts)
      .catch((e: Error) => setError(e.message));
  };

  useEffect(reloadAccounts, []);

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

  const patchSettings = (key: string, value: unknown) => {
    const settings = { ...draft.settings };
    if (value === undefined) delete settings[key];
    else settings[key] = value;
    setDraft({ ...draft, settings });
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
        Marketplace identities the orchestrator acts through. Enroll secrets via the Credentials
        panel — they go straight to the vault; <code>credential_ref</code> only points at the vault
        entry.
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
                <th></th>
              </tr>
            </thead>
            <tbody>
              {accounts.map((a) => {
                const warning = missingSettingsWarning(a);
                return (
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
                    <td>
                      {a.label}
                      {warning && (
                        <span
                          className="pill"
                          style={{ marginLeft: 8, color: 'var(--amber)' }}
                          title="Required to submit proposals"
                        >
                          {warning}
                        </span>
                      )}
                    </td>
                    <td className="muted">{a.principal}</td>
                    <td>{a.mode}</td>
                    <td style={{ color: a.enabled ? 'var(--green)' : 'var(--red)' }}>
                      {a.enabled ? 'yes' : 'no'}
                    </td>
                    <td className="muted">{a.credential_ref || '—'}</td>
                    <td className="muted">{formatDate(a.created_at)}</td>
                    <td>
                      <button
                        className="btn secondary small"
                        onClick={(e) => {
                          e.stopPropagation();
                          setCredAccountId((prev) => (prev === a.id ? null : a.id));
                        }}
                      >
                        {credAccountId === a.id ? 'Hide' : 'Credentials'}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {credAccountId != null &&
        (() => {
          const credAccount = accounts.find((a) => a.id === credAccountId);
          return credAccount ? (
            <CredentialsPanel
              account={credAccount}
              onChanged={reloadAccounts}
              onClose={() => setCredAccountId(null)}
            />
          ) : null;
        })()}

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
          {(draft.platform === 'freelancer' || draft.platform === 'upwork') && (
            <>
              <div className="form-row" style={{ marginBottom: 0 }}>
                {draft.platform === 'freelancer' && (
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Bidder ID</label>
                    <input
                      type="number"
                      value={
                        typeof draft.settings.bidder_id === 'number' ? draft.settings.bidder_id : ''
                      }
                      placeholder="Freelancer user/bidder id"
                      onChange={(e) =>
                        patchSettings(
                          'bidder_id',
                          e.target.value === '' ? undefined : Number(e.target.value),
                        )
                      }
                    />
                  </div>
                )}
                {draft.platform === 'upwork' && (
                  <div className="field" style={{ flex: 1, marginBottom: 0 }}>
                    <label>On behalf of</label>
                    <input
                      type="text"
                      value={
                        typeof draft.settings.on_behalf_of === 'string'
                          ? draft.settings.on_behalf_of
                          : ''
                      }
                      placeholder="Upwork agency member UID"
                      onChange={(e) =>
                        patchSettings(
                          'on_behalf_of',
                          e.target.value.trim() === '' ? undefined : e.target.value,
                        )
                      }
                    />
                  </div>
                )}
              </div>
              <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                Required before proposals can be submitted on {draft.platform}.
              </p>
            </>
          )}
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
