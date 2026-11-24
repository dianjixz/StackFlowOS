/**
 * session.ts — identity, browser sessions and the 401 login gate.
 *
 * The console is a single-operator control plane backed by an HttpOnly cookie.
 * This module owns:
 *   * the current identity (user + role) used by the top bar and the write gate;
 *   * the session list rendered by Settings → 安全;
 *   * a blocking "session expired" screen fed by the `sfo:unauthorized` event
 *     that api/client.ts dispatches on every HTTP 401.
 */

import { useSyncExternalStore } from 'react';
import { api, isForbidden, isUnsupported } from '../api/client';

export interface SessionRow {
  id?: string;
  session_id?: string;
  user?: string;
  username?: string;
  role?: string;
  created_at?: number;
  last_seen?: number;
  current?: boolean;
  current_session?: boolean;
  ua?: string;
  ip?: string;
  [key: string]: unknown;
}

export interface AuthState {
  user: string;
  role: string;
  known: boolean;
  unavailable: boolean;
  forbidden: boolean;
  loading: boolean;
  error: string;
  sessions: SessionRow[] | null;
  currentId: string;
}

const initial: AuthState = {
  user: '',
  role: '',
  known: false,
  unavailable: false,
  forbidden: false,
  loading: false,
  error: '',
  sessions: null,
  currentId: '',
};

let state: AuthState = initial;
const listeners = new Set<() => void>();

function setState(patch: Partial<AuthState>): void {
  state = { ...state, ...patch };
  for (const listener of [...listeners]) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function useAuth(): AuthState {
  return useSyncExternalStore(subscribe, () => state, () => state);
}

/** Whether the current role may perform write operations (server stays the authority). */
export function canWrite(): boolean {
  return state.role !== 'viewer';
}

export function useCanWrite(): boolean {
  useAuth();
  return canWrite();
}

function lastSeen(row: SessionRow): number {
  return Number(row?.last_seen) || 0;
}

function unwrapSessionList(data: unknown): { rows: SessionRow[]; current: SessionRow | null; payload: Record<string, any> } {
  const payload = (data && typeof data === 'object' ? data : {}) as Record<string, any>;
  const rows: SessionRow[] = Array.isArray(payload.sessions) ? payload.sessions
    : Array.isArray(payload.items) ? payload.items
      : Array.isArray(data) ? (data as SessionRow[]) : [];
  // The backend does not tag the caller's row.  An explicit marker wins; a
  // single-row answer is unambiguous (viewers only ever see their own); the
  // remaining case is a multi-row operator view, where the most recently used
  // session is the best available approximation for the identity chip.
  let current: SessionRow | null = payload.current && typeof payload.current === 'object' ? payload.current
    : rows.find((row) => row && (row.current || row.current_session)) || null;
  if (!current && rows.length === 1) current = rows[0];
  if (!current && rows.length > 1) current = [...rows].sort((a, b) => lastSeen(b) - lastSeen(a))[0] || null;
  return { rows, current, payload };
}

/** Fetch `/api/sessions`; quiet mode never toasts (used for the top-bar chip). */
export async function loadSessions({ quiet = true, force = false }: { quiet?: boolean; force?: boolean } = {}): Promise<SessionRow[] | null> {
  if (state.loading) return state.sessions;
  if (!force && state.sessions && !quiet) return state.sessions;
  setState({ loading: true, error: '' });
  try {
    const data = await api.getSessions();
    const { rows, current, payload } = unwrapSessionList(data);
    setState({
      sessions: rows,
      currentId: String(current?.id || current?.session_id || payload.current_id || ''),
      user: String(current?.user || current?.username || payload.user || payload.username || ''),
      role: String(current?.role || payload.role || ''),
      known: true,
      unavailable: false,
      forbidden: false,
      loading: false,
    });
    return rows;
  } catch (error) {
    setState({
      sessions: null,
      currentId: '',
      known: false,
      unavailable: isUnsupported(error),
      forbidden: isForbidden(error),
      error: error instanceof Error ? error.message : String(error),
      loading: false,
    });
    return null;
  }
}

/* ------------------------------------------------------------------ */
/* 401 gate                                                            */
/* ------------------------------------------------------------------ */

let blocked = false;
const gateListeners = new Set<() => void>();

export function isBlocked(): boolean {
  return blocked;
}

export function setBlocked(next: boolean): void {
  if (blocked === next) return;
  blocked = next;
  for (const listener of [...gateListeners]) listener();
}

function subscribeGate(listener: () => void): () => void {
  gateListeners.add(listener);
  return () => {
    gateListeners.delete(listener);
  };
}

export function useBlocked(): boolean {
  return useSyncExternalStore(subscribeGate, isBlocked, isBlocked);
}

/** Install the single 401 listener (api/client.ts dispatches the event). */
export function initAuthGate(): () => void {
  const onUnauthorized = (): void => setBlocked(true);
  document.addEventListener('sfo:unauthorized', onUnauthorized);
  return () => document.removeEventListener('sfo:unauthorized', onUnauthorized);
}

export async function signOut(): Promise<void> {
  await api.logout();
  setBlocked(true);
}
