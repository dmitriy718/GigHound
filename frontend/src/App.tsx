import { useCallback, useEffect, useRef, useState } from 'react';
import { useAlertsSocket } from './hooks/useAlertsSocket';
import {
  clearToken,
  getMe,
  getToken,
  logout,
  setOnUnauthorized,
  setToken,
} from './api/client';
import type { User } from './types';
import { ChangePasswordModal, DeleteAccountModal } from './components/AccountModals';
import Login from './views/Login';
import JobFeed from './views/JobFeed';
import ProposalQueue from './views/ProposalQueue';
import BuyerRequestInbox from './views/BuyerRequestInbox';
import GigManager from './views/GigManager';
import Analytics from './views/Analytics';
import KeywordIntelligence from './views/KeywordIntelligence';
import SearchFineTuning from './views/SearchFineTuning';
import SearchProfiles from './views/SearchProfiles';
import ScoringConfig from './views/ScoringConfig';
import AlertsPanel from './views/AlertsPanel';
import Accounts from './views/Accounts';
import ProfileManager from './views/ProfileManager';

export type ViewKey =
  | 'jobs'
  | 'proposals'
  | 'buyerRequests'
  | 'gigs'
  | 'analytics'
  | 'keywords'
  | 'filters'
  | 'searchProfiles'
  | 'scoring'
  | 'alerts'
  | 'accounts'
  | 'profiles';

interface NavItem { key: ViewKey; label: string; hint: string }
interface NavCluster { label: string; glyph: string; items: NavItem[] }

const CLUSTERS: NavCluster[] = [
  {
    label: 'Hunt', glyph: '◎',
    items: [
      { key: 'jobs', label: 'Job Feed', hint: 'live stream' },
      { key: 'proposals', label: 'Proposal Queue', hint: 'review & approve' },
      { key: 'buyerRequests', label: 'Buyer Requests', hint: 'fiverr inbox' },
    ],
  },
  {
    label: 'Sell', glyph: '◈',
    items: [
      { key: 'gigs', label: 'Gig Manager', hint: 'templates & analytics' },
      { key: 'analytics', label: 'Win-Rate Analytics', hint: 'am i winning?' },
    ],
  },
  {
    label: 'Tune', glyph: '◇',
    items: [
      { key: 'keywords', label: 'Keyword Intelligence', hint: 'match terms' },
      { key: 'filters', label: 'Search Fine-Tuning', hint: 'filter presets' },
      { key: 'searchProfiles', label: 'Search Profiles', hint: 'boolean queries' },
      { key: 'scoring', label: 'Scoring Config', hint: 'quality engine' },
    ],
  },
  {
    label: 'System', glyph: '◍',
    items: [
      { key: 'alerts', label: 'Alerts', hint: 'realtime & digest' },
      { key: 'accounts', label: 'Platform Accounts', hint: 'connections' },
      { key: 'profiles', label: 'Profiles', hint: 'pitch & portfolio' },
    ],
  },
];

function clusterOf(view: ViewKey): string {
  return CLUSTERS.find((c) => c.items.some((i) => i.key === view))?.label ?? '';
}

export default function App() {
  const [view, setView] = useState<ViewKey>('jobs');
  const [openCluster, setOpenCluster] = useState<string | null>(null);
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const [user, setUser] = useState<User | null>(null);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [accountModal, setAccountModal] = useState<'password' | 'delete' | null>(null);
  const navRef = useRef<HTMLDivElement>(null);
  const userMenuRef = useRef<HTMLDivElement>(null);

  const socket = useAlertsSocket({ token });

  // Any 401 from the API client drops the session back to Login.
  useEffect(() => {
    setOnUnauthorized(() => {
      setTokenState(null);
      setUser(null);
    });
    return () => setOnUnauthorized(null);
  }, []);

  // Hydrate the current user when a token exists (page reload).
  useEffect(() => {
    if (!token) return;
    getMe().then(setUser).catch(() => {}); // 401 is handled via onUnauthorized
  }, [token]);

  const handleAuthed = useCallback((newToken: string, authedUser: User) => {
    setToken(newToken);
    setUser(authedUser);
    setTokenState(newToken);
  }, []);

  const handleLogout = useCallback(() => {
    logout().catch(() => {}); // best-effort; JWT is discarded client-side
    clearToken();
    setTokenState(null);
    setUser(null);
    setUserMenuOpen(false);
  }, []);

  const handleDeleted = useCallback(() => {
    clearToken();
    setTokenState(null);
    setUser(null);
    setAccountModal(null);
    setUserMenuOpen(false);
  }, []);

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (navRef.current && !navRef.current.contains(e.target as Node)) setOpenCluster(null);
      if (userMenuRef.current && !userMenuRef.current.contains(e.target as Node)) setUserMenuOpen(false);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

  if (!token) {
    return (
      <div className="app">
        <div className="atmosphere" aria-hidden="true">
          <div className="smoke smoke-a" />
          <div className="smoke smoke-b" />
          <div className="smoke smoke-c" />
          <div className="grain" />
        </div>
        <Login onAuthed={handleAuthed} />
      </div>
    );
  }

  return (
    <div className="app">
      <div className="atmosphere" aria-hidden="true">
        <div className="smoke smoke-a" />
        <div className="smoke smoke-b" />
        <div className="smoke smoke-c" />
        <div className="grain" />
      </div>

      <header className="topbar">
        <div className="brand">
          <span className="brand-mark">GH</span>
          <div className="brand-word">
            <span className="brand-name">GigHound</span>
            <span className="brand-tag">autonomous gig hunting</span>
          </div>
        </div>

        <nav className="clusters" ref={navRef}>
          {CLUSTERS.map((cluster) => (
            <div
              key={cluster.label}
              className={`cluster ${clusterOf(view) === cluster.label ? 'active' : ''} ${
                openCluster === cluster.label ? 'open' : ''
              }`}
            >
              <button
                className="cluster-btn"
                onClick={() => setOpenCluster(openCluster === cluster.label ? null : cluster.label)}
              >
                <span className="cluster-glyph">{cluster.glyph}</span>
                {cluster.label}
                <span className="cluster-caret">▾</span>
              </button>
              <div className="cluster-menu">
                {cluster.items.map((item) => (
                  <button
                    key={item.key}
                    className={`menu-item ${view === item.key ? 'active' : ''}`}
                    onClick={() => { setView(item.key); setOpenCluster(null); }}
                  >
                    <span>{item.label}</span>
                    <span className="menu-hint">{item.hint}</span>
                  </button>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="topbar-right">
          <div className={`live-dot socket-${socket.status}`}>
            <span className="dot" />
            {socket.status === 'open' ? 'LIVE' : socket.status.toUpperCase()}
          </div>
          <div className={`cluster user-menu ${userMenuOpen ? 'open' : ''}`} ref={userMenuRef}>
            <button className="cluster-btn" onClick={() => setUserMenuOpen(!userMenuOpen)}>
              <span className="cluster-glyph">◉</span>
              {user?.display_name || user?.email || 'Account'}
              <span className="cluster-caret">▾</span>
            </button>
            <div className="cluster-menu">
              {user && (
                <div className="menu-item user-identity">
                  <span>{user.display_name || user.email}</span>
                  <span className="menu-hint">{user.email}</span>
                </div>
              )}
              <button
                className="menu-item"
                onClick={() => { setAccountModal('password'); setUserMenuOpen(false); }}
              >
                <span>Change password</span>
                <span className="menu-hint">update credentials</span>
              </button>
              <button className="menu-item" onClick={handleLogout}>
                <span>Log out</span>
                <span className="menu-hint">discard session token</span>
              </button>
              <button
                className="menu-item"
                onClick={() => { setAccountModal('delete'); setUserMenuOpen(false); }}
              >
                <span>Delete account</span>
                <span className="menu-hint">permanent — erases all data</span>
              </button>
            </div>
          </div>
        </div>
      </header>

      {accountModal === 'password' && (
        <ChangePasswordModal onClose={() => setAccountModal(null)} />
      )}
      {accountModal === 'delete' && user && (
        <DeleteAccountModal
          email={user.email}
          onDeleted={handleDeleted}
          onClose={() => setAccountModal(null)}
        />
      )}

      <main className="content">
        {view === 'jobs' && <JobFeed messages={socket.messages} onNavigate={setView} />}
        {view === 'proposals' && <ProposalQueue messages={socket.messages} user={user} />}
        {view === 'buyerRequests' && <BuyerRequestInbox messages={socket.messages} />}
        {view === 'gigs' && <GigManager />}
        {view === 'analytics' && <Analytics />}
        {view === 'keywords' && <KeywordIntelligence />}
        {view === 'filters' && <SearchFineTuning />}
        {view === 'searchProfiles' && <SearchProfiles />}
        {view === 'scoring' && <ScoringConfig />}
        {view === 'alerts' && <AlertsPanel messages={socket.messages} status={socket.status} />}
        {view === 'accounts' && <Accounts />}
        {view === 'profiles' && <ProfileManager />}
      </main>
    </div>
  );
}
