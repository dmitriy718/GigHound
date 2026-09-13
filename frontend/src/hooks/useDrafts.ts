import { useEffect } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { ProposalQueueItem } from '../types';

// P3-3: mirror unsaved review edits to sessionStorage so a mid-session 401
// (token cleared, every view unmounted) doesn't destroy reviewer work. Keyed
// by user id so a shared browser never bleeds drafts across tenants; it is
// deliberately NOT cleared on logout/401 — surviving the re-login is the point.

interface StoredDraft<E> {
  edits: E;
  revision: number;
  base: string; // server text the edit started from
}

const readStore = <E,>(key: string): Record<string, StoredDraft<E>> => {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(key) ?? '{}');
    return value && typeof value === 'object' && !Array.isArray(value)
      ? value as Record<string, StoredDraft<E>> : {};
  } catch {
    return {};
  }
};

const writeStore = <E,>(key: string, stored: Record<string, StoredDraft<E>>) => {
  try {
    sessionStorage.setItem(key, JSON.stringify(stored));
  } catch {
    // quota/denied — drafts are best-effort
  }
};

/**
 * Mirror `edits` to sessionStorage and restore drafts with their original revision.
 * Only dirty entries are persisted (isPristine decides), so approving or
 * rejecting an item — which resets its entry to pristine — clears the draft.
 * Stale pending-review drafts remain visible for comparison; callers must require
 * an explicit conflict choice before approval. Restoring never updates the server.
 */
export function useDrafts<E extends { base_revision: number }>(
  userId: number | null | undefined,
  items: ProposalQueueItem[],
  edits: Record<number, E>,
  setEdits: Dispatch<SetStateAction<Record<number, E>>>,
  editsFrom: (item: ProposalQueueItem) => E,
  isPristine: (item: ProposalQueueItem, edits: E) => boolean,
) {
  const key = `gighound:drafts:${userId ?? 'anon'}`;

  // persist on change (entries for items off the current page are kept as-is)
  useEffect(() => {
    const stored = readStore<E>(key);
    for (const [idStr, e] of Object.entries(edits)) {
      const item = items.find((p) => p.id === Number(idStr));
      if (!item) continue;
      if (isPristine(item, e)) delete stored[idStr];
      else {
        const previous = stored[idStr];
        stored[idStr] = {
          edits: e,
          revision: e.base_revision,
          base: previous?.revision === e.base_revision
            ? previous.base : item.humanized_text || item.proposal_text,
        };
      }
    }
    writeStore(key, stored);
  }, [edits, items, key, isPristine]);

  // restore after each load; no-op (same state identity) when nothing applies
  useEffect(() => {
    const stored = readStore<E>(key);
    setEdits((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const item of items) {
        const s = stored[item.id];
        if (!s || next[item.id]) continue;
        if (item.status === "pending_review" && s.edits && typeof s.edits === "object" && Number.isInteger(s.revision) && s.revision >= 1 && s.edits.base_revision === s.revision) {
          // editsFrom defaults keep entries written by another view complete
          next[item.id] = { ...editsFrom(item), ...s.edits };
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [items, key, setEdits, editsFrom]);

  // explicit clear for items leaving the page (approve/reject/bulk) — the
  // persist effect skips off-page items, so their drafts survive otherwise
  const clearDrafts = (ids: number[]) => {
    const stored = readStore<E>(key);
    for (const id of ids) delete stored[id];
    writeStore(key, stored);
  };

  return { clearDrafts };
}
