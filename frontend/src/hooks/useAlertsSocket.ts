import { useEffect, useRef, useState } from 'react';
import { wsUrl } from '../api/client';
import type { Job } from '../types';

export type AlertMessage =
  | { type: 'job_alert'; job: Job; receivedAt: number }
  | { type: 'hot_job'; job: Job; receivedAt: number }
  | { type: 'job_ingested'; job: Job; receivedAt: number }
  | { type: 'proposal_queued'; proposal_id: number; job?: Job; receivedAt: number }
  | { type: 'generation_failed'; proposal_id: number; error?: string; job_id?: number; receivedAt: number }
  | { type: 'client_replied'; proposal_id: number; job_id: number; snippet: string; receivedAt: number }
  | { type: 'digest'; jobs: Job[]; receivedAt: number }
  | { type: string; receivedAt: number; job?: undefined; jobs?: undefined };

export type SocketStatus = 'connecting' | 'open' | 'closed';

interface Options {
  onMessage?: (msg: AlertMessage) => void;
  token?: string | null; // JWT appended as ?token= — reconnects when it changes; no connect without one
}

/**
 * WebSocket hook for WS /ws/alerts.
 * Auto-reconnects with exponential backoff (1s → 30s max) and sends a `ping`
 * text message every 30s while open.
 */
export function useAlertsSocket({ onMessage, token }: Options = {}) {
  const [status, setStatus] = useState<SocketStatus>('connecting');
  const [messages, setMessages] = useState<AlertMessage[]>([]);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useEffect(() => {
    if (!token) {
      setStatus('closed');
      return;
    }
    let ws: WebSocket | null = null;
    let pingTimer: number | undefined;
    let retryTimer: number | undefined;
    let attempts = 0;
    let disposed = false;

    const connect = () => {
      if (disposed) return;
      setStatus('connecting');
      ws = new WebSocket(`${wsUrl('/ws/alerts')}?token=${encodeURIComponent(token)}`);

      ws.onopen = () => {
        attempts = 0;
        setStatus('open');
        pingTimer = window.setInterval(() => {
          if (ws && ws.readyState === WebSocket.OPEN) ws.send('ping');
        }, 30_000);
      };

      ws.onmessage = (ev) => {
        let data: {
          type?: string;
          job?: Job;
          jobs?: Job[];
          proposal_id?: number;
          job_id?: number;
          error?: string;
          snippet?: string;
        };
        try {
          data = JSON.parse(ev.data as string);
        } catch {
          return; // ignore non-JSON (e.g. pong frames as text)
        }
        if (!data || typeof data.type !== 'string') return;
        const msg: AlertMessage =
          (data.type === 'job_alert' || data.type === 'hot_job' || data.type === 'job_ingested') &&
          data.job
            ? { type: data.type, job: data.job, receivedAt: Date.now() }
            : data.type === 'proposal_queued'
              ? {
                  type: 'proposal_queued',
                  proposal_id: data.proposal_id ?? 0,
                  job: data.job,
                  receivedAt: Date.now(),
                }
              : data.type === 'generation_failed'
                ? {
                    type: 'generation_failed',
                    proposal_id: data.proposal_id ?? 0,
                    error: data.error,
                    job_id: data.job_id,
                    receivedAt: Date.now(),
                  }
              : data.type === 'client_replied'
                ? {
                    type: 'client_replied',
                    proposal_id: data.proposal_id ?? 0,
                    job_id: data.job_id ?? 0,
                    snippet: data.snippet ?? '',
                    receivedAt: Date.now(),
                  }
              : data.type === 'digest' && Array.isArray(data.jobs)
                ? { type: 'digest', jobs: data.jobs, receivedAt: Date.now() }
                : { type: data.type, receivedAt: Date.now() };
        setMessages((prev) => [msg, ...prev].slice(0, 50));
        onMessageRef.current?.(msg);
      };

      const scheduleReconnect = () => {
        if (pingTimer !== undefined) window.clearInterval(pingTimer);
        setStatus('closed');
        if (disposed) return;
        const delay = Math.min(1000 * 2 ** attempts, 30_000);
        attempts += 1;
        retryTimer = window.setTimeout(connect, delay);
      };

      ws.onclose = scheduleReconnect;
      ws.onerror = () => {
        ws?.close();
      };
    };

    connect();

    return () => {
      disposed = true;
      if (pingTimer !== undefined) window.clearInterval(pingTimer);
      if (retryTimer !== undefined) window.clearTimeout(retryTimer);
      ws?.close();
    };
  }, [token]);

  return { status, messages };
}

/**
 * Fires `handler` once for every message the consumer hasn't seen yet, oldest first.
 * Tracks the last consumed message by identity, so a burst of events collapsed into a
 * single render by React 18 batching is still delivered in full (unlike a single
 * `lastMessage` state, which drops all but the newest event in the batch).
 */
export function useNewAlertMessages(
  messages: AlertMessage[],
  handler: (msg: AlertMessage) => void,
) {
  const lastSeenRef = useRef<AlertMessage | null>(null);
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    if (messages.length === 0) return;
    const prev = lastSeenRef.current;
    const idx = prev ? messages.indexOf(prev) : 0;
    // idx === -1: the last-seen message rolled out of the 50-entry cap — catch up from
    // the newest message only rather than replaying history.
    const fresh = idx === -1 ? [messages[0]] : messages.slice(0, idx).reverse();
    lastSeenRef.current = messages[0];
    for (const msg of fresh) handlerRef.current(msg);
  }, [messages]);
}
