import { useCallback, useEffect, useRef, useState } from 'react';
import { useAlertsSocket, type AlertMessage } from './hooks/useAlertsSocket';
import JobFeed from './views/JobFeed';
import ProposalQueue from './views/ProposalQueue';
import BuyerRequestInbox from './views/BuyerRequestInbox';
import GigManager from './views/GigManager';
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
  const [lastMessage, setLastMessage] = useState<AlertMessage | null>(null);
  const navRef = useRef<HTMLDivElement>(null);

  const handleMessage = useCallback((msg: AlertMessage) => {
    setLastMessage(msg);
  }, []);

  const socket = useAlertsSocket({ onMessage: handleMessage });

  useEffect(() => {
    const close = (e: MouseEvent) => {
      if (navRef.current && !navRef.current.contains(e.target as Node)) setOpenCluster(null);
    };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);

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
        </div>
      </header>

      <main className="content">
        {view === 'jobs' && <JobFeed lastMessage={lastMessage} />}
        {view === 'proposals' && <ProposalQueue lastMessage={lastMessage} />}
        {view === 'buyerRequests' && <BuyerRequestInbox />}
        {view === 'gigs' && <GigManager />}
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
