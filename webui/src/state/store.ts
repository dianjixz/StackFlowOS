/**
 * store.ts — one live snapshot + SSE reducer for the React console.
 *
 * The server is the source of truth.  The store keeps the latest `/api/state`
 * snapshot, folds the event stream into it (chat deltas, event ticker, lastSeq)
 * and refetches the snapshot (debounced) for structural changes.
 *
 * State is immutable: every reducer produces a new object so React's
 * `useSyncExternalStore` sees a new snapshot.
 */

import { useCallback, useRef, useSyncExternalStore } from 'react';
import { api } from '../api/client';

/* ------------------------------------------------------------------ */
/* data shapes (the server sends more fields; these are the ones the   */
/* console reads today — extend here, never inline in a screen)        */
/* ------------------------------------------------------------------ */

export interface ChatMessage {
  id: string;
  role: string;
  text: string;
  ts: number;
  agent_id?: string | null;
  pending?: boolean;
  error?: boolean;
  tool_calls?: ToolCall[];
}

export interface ToolCall {
  id?: string;
  name?: string;
  args?: unknown;
  result?: string;
  status?: string;
}

export interface ProjectCounts {
  todo?: number;
  doing?: number;
  review?: number;
  done?: number;
  blocked?: number;
}

export interface Project {
  id: string;
  name: string;
  goal?: string;
  status: string;
  progress: number;
  counts?: ProjectCounts;
  current_task?: { id?: string; title?: string } | null;
  total_tasks?: number;
  updated_at?: number;
  brief?: string;
  workspace?: string;
  tags?: string[];
  color?: string;
  degraded?: boolean;
  archived_at?: number | null;
  pending_approvals?: number;
  agents?: Agent[];
  leases?: Lease[];
}

export interface Agent {
  id: string;
  role: string;
  project_id?: string | null;
  task_id?: string | null;
  unit_id?: string | null;
  status: string;
  model?: string;
  display_name?: string;
  created_at?: number;
  last_seen?: number;
  stats?: Record<string, number>;
  summary?: string | null;
  error?: string | null;
}

export interface Unit {
  work_id: string;
  kind: string;
  label?: string;
  tier?: string;
  project_id?: string | null;
  pid?: number | null;
  host?: string;
  transport?: string;
  rpc_address?: string;
  events_address?: string;
  status: string;
  abilities?: unknown;
  hwinfo?: Record<string, unknown>;
  started_at?: number;
  last_seen?: number;
  requires?: string[];
  wants?: string[];
  [key: string]: unknown;
}

export interface Lease {
  id: string;
  resource: string;
  holder?: string;
  ttl_s?: number;
  renewable?: boolean;
  preemptible?: boolean;
  priority?: number;
  created_at?: number;
  expires_at?: number;
  status?: string;
  metadata?: Record<string, unknown>;
}

export interface Approval {
  id: string;
  principal: string;
  capability: string;
  scope?: Record<string, unknown>;
  risk: string;
  reason?: string;
  preview?: unknown;
  project_id?: string | null;
  task_id?: string | null;
  status: string;
  mode?: string | null;
  grant_id?: string | null;
  created_at?: number;
  expires_at?: number;
  decided_at?: number | null;
  decided_by?: string | null;
  note?: string;
}

export interface Rule {
  id: string;
  effect: string;
  capability: string;
  principal?: string;
  scope?: Record<string, unknown>;
  risk_max?: string;
  expires_at?: number | null;
  uses_left?: number | null;
  note?: string;
  source?: string;
  created_by?: string;
  created_at?: number;
}

export interface SfoEvent {
  seq: number;
  id: string;
  type: string;
  source?: string;
  ts: number;
  data?: Record<string, any>;
}

export interface SysInfo {
  name?: string;
  version?: string;
  host?: string;
  pid?: number;
  started_at?: number;
  transport?: string;
  sys_rpc?: string;
  sys_events?: string;
  web_port?: number;
  last_seq?: number;
  stats?: Record<string, any>;
}

export interface SfoConfig {
  system?: Record<string, any>;
  transport?: Record<string, any>;
  web?: Record<string, any>;
  security?: Record<string, any>;
  llm?: Record<string, any>;
  agents?: Record<string, any>;
  paths?: Record<string, any>;
  autonomy?: Record<string, any>;
  [key: string]: any;
}

export interface StreamState {
  active: boolean;
  text: string;
  startedAt: number;
  messageId: string | null;
}

export interface SfoState {
  loaded: boolean;
  loading: boolean;
  error: unknown;
  sse: 'connecting' | 'live' | 'reconnecting' | 'offline';
  lastSeq: number;
  sys: SysInfo | null;
  units: Unit[];
  projects: Project[];
  agents: Agent[];
  leases: Lease[];
  approvals: Approval[];
  rules: Rule[];
  events: SfoEvent[];
  chat: ChatMessage[];
  config: SfoConfig | null;
  stats: Record<string, any>;
  stream: StreamState;
  typing: boolean;
}

const initialState: SfoState = {
  loaded: false,
  loading: false,
  error: null,
  sse: 'connecting',
  lastSeq: 0,
  sys: null,
  units: [],
  projects: [],
  agents: [],
  leases: [],
  approvals: [],
  rules: [],
  events: [],
  chat: [],
  config: null,
  stats: {},
  stream: { active: false, text: '', startedAt: 0, messageId: null },
  typing: false,
};

export const MAX_EVENTS = 300;

let state: SfoState = initialState;

const listeners = new Set<() => void>();

export function getState(): SfoState {
  return state;
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function setState(patch: Partial<SfoState>): void {
  state = { ...state, ...patch };
  for (const listener of [...listeners]) {
    try {
      listener();
    } catch (error) {
      console.error('[store] listener failed', error);
    }
  }
}

/* ------------------------------------------------------------------ */
/* selectors                                                           */
/* ------------------------------------------------------------------ */

export function shallowEqual<T>(a: T, b: T): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) return false;
  const aKeys = Object.keys(a as Record<string, unknown>);
  const bKeys = Object.keys(b as Record<string, unknown>);
  if (aKeys.length !== bKeys.length) return false;
  for (const key of aKeys) {
    if (!Object.is((a as Record<string, unknown>)[key], (b as Record<string, unknown>)[key])) return false;
  }
  return true;
}

/** Subscribe a component to a slice of the store. */
export function useStore<T>(selector: (s: SfoState) => T, isEqual: (a: T, b: T) => boolean = Object.is): T {
  const cache = useRef<{ value: T } | null>(null);
  const getSnapshot = useCallback(() => {
    const value = selector(state);
    if (cache.current && isEqual(cache.current.value, value)) return cache.current.value;
    cache.current = { value };
    return value;
  }, [selector, isEqual]);
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/* ------------------------------------------------------------------ */
/* snapshot / refresh                                                  */
/* ------------------------------------------------------------------ */

function sortEvents(events: SfoEvent[]): SfoEvent[] {
  return events
    .filter(Boolean)
    .sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0));
}

function dedupeEvents(events: SfoEvent[]): SfoEvent[] {
  const byKey = new Map<string, SfoEvent>();
  for (const event of events) {
    if (!event) continue;
    const key = event.id ? `id:${event.id}` : `seq:${event.seq}`;
    byKey.set(key, event);
  }
  return sortEvents([...byKey.values()]).slice(-MAX_EVENTS);
}

export function mergeChat(incoming: ChatMessage[] = [], existing: ChatMessage[] = []): ChatMessage[] {
  const byId = new Map<string, ChatMessage>();
  for (const message of incoming) {
    if (message && message.id) byId.set(message.id, message);
  }
  for (const message of existing) {
    if (message && message.id && !byId.has(message.id)) byId.set(message.id, message);
  }
  return [...byId.values()].sort((a, b) => (a.ts || 0) - (b.ts || 0));
}

/** Replace local state with a fresh `/api/state` snapshot. */
export function applySnapshot(data: Record<string, any>): void {
  if (!data || typeof data !== 'object') return;
  const events = dedupeEvents([
    ...(Array.isArray(data.events) ? data.events : []),
    ...state.events,
  ]);
  const lastSeq = Math.max(
    Number(state.lastSeq) || 0,
    Number(data.sys?.last_seq) || 0,
    ...events.map((event) => Number(event.seq) || 0),
  );
  setState({
    sys: data.sys || state.sys,
    units: Array.isArray(data.units) ? data.units : [],
    projects: Array.isArray(data.projects) ? data.projects : [],
    agents: Array.isArray(data.agents) ? data.agents : [],
    leases: Array.isArray(data.leases) ? data.leases : [],
    approvals: Array.isArray(data.approvals) ? data.approvals : [],
    rules: Array.isArray(data.rules) ? data.rules : [],
    config: data.config || state.config,
    stats: data.stats || {},
    chat: mergeChat(Array.isArray(data.chat) ? data.chat : [], state.chat),
    events,
    lastSeq,
    loaded: true,
    loading: false,
    error: null,
  });
}

export function applyConfig(config: SfoConfig | null): void {
  if (!config) return;
  setState({ config });
}

let refreshTimer: number | null = null;
let refreshInFlight: Promise<unknown> | null = null;
let refreshQueued = false;

/** Fetch `/api/state` and merge it.  Concurrent calls share one request. */
export function loadState({ quiet = false }: { quiet?: boolean } = {}): Promise<unknown> {
  if (refreshInFlight) return refreshInFlight;
  if (!state.loaded) setState({ loading: true });
  refreshInFlight = (async () => {
    try {
      const data = await api.getState();
      applySnapshot(data as Record<string, any>);
      return data;
    } finally {
      refreshInFlight = null;
      if (refreshQueued) {
        refreshQueued = false;
        scheduleRefresh(220);
      }
    }
  })();
  const pending = refreshInFlight;
  return pending.catch((error) => {
    setState({ loading: false, error });
    if (!quiet) throw error;
    return null;
  });
}

/** Debounced snapshot refresh after structural events. */
export function scheduleRefresh(delay = 300): void {
  if (refreshTimer) window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null;
    if (refreshInFlight) {
      refreshQueued = true;
      return;
    }
    loadState({ quiet: true }).catch((error) => {
      console.warn('[store] background state refresh failed', error);
    });
  }, delay);
}

/* ------------------------------------------------------------------ */
/* chat helpers                                                        */
/* ------------------------------------------------------------------ */

export function appendChat(message: ChatMessage): boolean {
  if (!message || !message.id) return false;
  if (state.chat.some((item) => item.id === message.id)) return false;
  const chat = [...state.chat, message].sort((a, b) => (a.ts || 0) - (b.ts || 0));
  setState({ chat });
  return true;
}

export function addLocalChat(message: ChatMessage): boolean {
  return appendChat(message);
}

export function removeChatMessage(id: string): void {
  setState({ chat: state.chat.filter((message) => message.id !== id) });
}

export function resetStream(): void {
  setState({ stream: { active: false, text: '', startedAt: 0, messageId: null }, typing: false });
}

/* ------------------------------------------------------------------ */
/* SSE reducer                                                         */
/* ------------------------------------------------------------------ */

const STRUCTURAL = new Set(['project', 'task', 'agent', 'unit', 'approval', 'rule', 'tool', 'failure', 'sys']);

/** Fold one EventRecord into local state. */
export function applyEvent(event: SfoEvent): void {
  if (!event || typeof event !== 'object') return;
  const seq = Number(event.seq) || 0;
  if (seq && seq <= state.lastSeq) return; // reconnect/replay dedupe

  const type = String(event.type || '');
  const data = event.data || {};
  const [head] = type.split('.');

  if (type === 'chat.message') {
    const message = data as ChatMessage;
    if (message && message.id) {
      const added = appendChat(message);
      if (message.role === 'assistant') {
        resetStream();
      } else if (!added) {
        /* already present */
      }
    }
    setState({ events: dedupeEvents([...state.events, event]), lastSeq: seq || state.lastSeq });
    return;
  }

  if (type === 'chat.delta') {
    if (data.typing) {
      setState({
        typing: true,
        events: dedupeEvents([...state.events, event]),
        lastSeq: seq || state.lastSeq,
      });
      return;
    }
    if (data.delta) {
      const active = state.stream.active;
      setState({
        typing: false,
        stream: {
          active: true,
          startedAt: active ? state.stream.startedAt : Date.now(),
          text: (active ? state.stream.text : '') + String(data.delta),
          messageId: active ? state.stream.messageId : ((data.message_id || data.id || null) as string | null),
        },
        events: dedupeEvents([...state.events, event]),
        lastSeq: seq || state.lastSeq,
      });
      return;
    }
    setState({ events: dedupeEvents([...state.events, event]), lastSeq: seq || state.lastSeq });
    return;
  }

  if (type === 'chat.typing') {
    setState({
      typing: Boolean(data.typing ?? true),
      events: dedupeEvents([...state.events, event]),
      lastSeq: seq || state.lastSeq,
    });
    return;
  }

  setState({ events: dedupeEvents([...state.events, event]), lastSeq: seq || state.lastSeq });
  if (STRUCTURAL.has(head)) scheduleRefresh(head === 'sys' ? 400 : 300);
}

/* ------------------------------------------------------------------ */
/* SSE client                                                          */
/* ------------------------------------------------------------------ */

let source: EventSource | null = null;
let reconnectTimer: number | null = null;
let attempts = 0;
let everConnected = false;

function setSse(status: SfoState['sse']): void {
  if (state.sse === status) return;
  setState({ sse: status });
}

function scheduleReconnect(): void {
  if (reconnectTimer) window.clearTimeout(reconnectTimer);
  const delay = Math.min(900 * 2 ** attempts, 8000);
  attempts += 1;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connectSSE();
  }, delay);
}

/** Open the SSE stream from the current lastSeq.  Safe to call repeatedly. */
export function connectSSE(): void {
  if (source) {
    source.close();
    source = null;
  }
  setSse(attempts > 0 ? 'reconnecting' : 'connecting');
  // Same-origin EventSource: the browser attaches the HttpOnly `sfo_session`
  // cookie automatically.  Never append the launch token to this URL.
  const url = `/api/events/stream?since=${encodeURIComponent(String(state.lastSeq || 0))}`;
  let stream: EventSource;
  try {
    stream = new EventSource(url);
  } catch (error) {
    console.warn('[store] EventSource unavailable', error);
    setSse('offline');
    scheduleReconnect();
    return;
  }
  source = stream;

  stream.addEventListener('open', () => {
    if (source !== stream) return;
    const reconnected = everConnected;
    everConnected = true;
    attempts = 0;
    setSse('live');
    if (reconnected) {
      // Ephemeral deltas are not replayed.  A draft left over from the old
      // connection can never complete, so drop it and let the next snapshot
      // (or the final chat.message) render the durable message exactly once.
      if (state.stream.active) resetStream();
      scheduleRefresh(60);
    }
  });

  stream.addEventListener('message', (messageEvent) => {
    if (source !== stream) return;
    if (!messageEvent.data) return;
    let event: SfoEvent;
    try {
      event = JSON.parse(messageEvent.data) as SfoEvent;
    } catch (error) {
      console.warn('[store] bad SSE frame', error);
      return;
    }
    applyEvent(event);
  });

  stream.addEventListener('error', () => {
    if (source !== stream) return;
    stream.close();
    source = null;
    setSse('reconnecting');
    scheduleReconnect();
  });
}

export function disconnectSSE(): void {
  if (source) source.close();
  source = null;
  if (reconnectTimer) window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  setSse('offline');
}

/** Boot the console: snapshot first, then the live stream. */
export function bootStore(): Promise<unknown> {
  const ready = loadState().catch(() => null);
  connectSSE();
  return ready;
}
