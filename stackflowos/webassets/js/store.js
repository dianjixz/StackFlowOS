/**
 * store.js — one live snapshot + SSE reducer.
 *
 * The server is the source of truth. The store keeps the latest `/api/state`
 * snapshot, folds the event stream into it (chat deltas, ticker, lastSeq) and
 * refetches the snapshot (debounced) for structural changes.
 */

import { api } from './api.js';

/** @typedef {{id:string,role:string,text:string,ts:number,agent_id?:string}} ChatMessage */

const listeners = new Map();

/**
 * @param {string} type
 * @param {any} [detail]
 */
export function on(type, handler) {
  if (!listeners.has(type)) listeners.set(type, new Set());
  listeners.get(type).add(handler);
  return () => listeners.get(type)?.delete(handler);
}

function emit(type, detail) {
  const bucket = listeners.get(type);
  if (!bucket) return;
  for (const handler of [...bucket]) {
    try {
      handler(detail);
    } catch (error) {
      console.error(`[store] listener for ${type} failed`, error);
    }
  }
}

export const state = {
  loaded: false,
  loading: false,
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
  stream: { active: false, text: '', startedAt: 0 },
  typing: false,
};

const MAX_EVENTS = 300;

function sortEvents(events) {
  return events
    .filter(Boolean)
    .sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0));
}

function dedupeEvents(events) {
  const byKey = new Map();
  for (const event of events) {
    if (!event) continue;
    const key = event.id ? `id:${event.id}` : `seq:${event.seq}`;
    byKey.set(key, event);
  }
  return sortEvents([...byKey.values()]).slice(-MAX_EVENTS);
}

export function mergeChat(incoming = [], existing = []) {
  /** @type {Map<string, ChatMessage>} */
  const byId = new Map();
  for (const message of incoming) {
    if (message && message.id) byId.set(message.id, message);
  }
  for (const message of existing) {
    if (message && message.id && !byId.has(message.id)) byId.set(message.id, message);
  }
  return [...byId.values()].sort((a, b) => (a.ts || 0) - (b.ts || 0));
}

/** Replace local state with a fresh `/api/state` snapshot. */
export function applySnapshot(data) {
  if (!data || typeof data !== 'object') return;
  const wasLoaded = state.loaded;
  state.sys = data.sys || state.sys;
  state.units = Array.isArray(data.units) ? data.units : [];
  state.projects = Array.isArray(data.projects) ? data.projects : [];
  state.agents = Array.isArray(data.agents) ? data.agents : [];
  state.leases = Array.isArray(data.leases) ? data.leases : [];
  state.approvals = Array.isArray(data.approvals) ? data.approvals : [];
  state.rules = Array.isArray(data.rules) ? data.rules : [];
  state.config = data.config || state.config;
  state.stats = data.stats || {};
  state.chat = mergeChat(Array.isArray(data.chat) ? data.chat : [], state.chat);
  state.events = dedupeEvents([
    ...(Array.isArray(data.events) ? data.events : []),
    ...state.events,
  ]);
  state.lastSeq = Math.max(
    Number(state.lastSeq) || 0,
    Number(data.sys?.last_seq) || 0,
    ...state.events.map((event) => Number(event.seq) || 0),
  );
  state.loaded = true;
  state.loading = false;
  emit('state', { first: !wasLoaded });
}

export function applyConfig(config) {
  if (!config) return;
  state.config = config;
  emit('state', { configOnly: true });
}

let refreshTimer = null;
let refreshInFlight = null;
let refreshQueued = false;

/** Fetch `/api/state` and merge it. Concurrent calls share one request. */
export async function loadState({ quiet = false } = {}) {
  if (refreshInFlight) return refreshInFlight;
  state.loading = !state.loaded;
  refreshInFlight = (async () => {
    try {
      const data = await api.getState();
      applySnapshot(data);
      return data;
    } finally {
      refreshInFlight = null;
      if (refreshQueued) {
        refreshQueued = false;
        scheduleRefresh(220);
      }
    }
  })();
  try {
    return await refreshInFlight;
  } catch (error) {
    state.loading = false;
    if (!quiet) emit('state-error', error);
    throw error;
  }
}

/** Debounced snapshot refresh after structural events. */
export function scheduleRefresh(delay = 300) {
  if (refreshTimer) window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => {
    refreshTimer = null;
    if (refreshInFlight) {
      refreshQueued = true;
      return;
    }
    loadState({ quiet: true }).catch((error) => {
      console.warn('[store] background state refresh failed', error);
      emit('state-error', error);
    });
  }, delay);
}

/* ------------------------------------------------------------------ */
/* chat helpers                                                        */
/* ------------------------------------------------------------------ */

export function appendChat(message) {
  if (!message || !message.id) return false;
  if (state.chat.some((item) => item.id === message.id)) return false;
  state.chat.push(message);
  state.chat.sort((a, b) => (a.ts || 0) - (b.ts || 0));
  return true;
}

export function addLocalChat(message) {
  const added = appendChat(message);
  if (added) emit('chat', { kind: 'message', message });
  return added;
}

export function removeChatMessage(id) {
  state.chat = state.chat.filter((message) => message.id !== id);
}

export function resetStream() {
  state.stream.active = false;
  state.stream.text = '';
  state.stream.startedAt = 0;
  state.typing = false;
}

/* ------------------------------------------------------------------ */
/* SSE reducer                                                         */
/* ------------------------------------------------------------------ */

const STRUCTURAL = new Set(['project', 'task', 'agent', 'unit', 'approval', 'rule', 'tool', 'failure', 'sys']);

/** Fold one EventRecord into local state. */
export function applyEvent(event) {
  if (!event || typeof event !== 'object') return;
  const seq = Number(event.seq) || 0;
  if (seq && seq <= state.lastSeq) return; // reconnect/replay dedupe
  if (seq) state.lastSeq = seq;

  state.events = dedupeEvents([...state.events, event]);

  const type = String(event.type || '');
  const data = event.data || {};
  const [head] = type.split('.');

  if (type === 'chat.message') {
    const message = data;
    if (message && message.id) {
      const added = appendChat(message);
      if (message.role === 'assistant') {
        resetStream();
        emit('chat', { kind: 'final', message });
      } else if (added) {
        emit('chat', { kind: 'message', message });
      }
    }
    emit('event', event);
    return;
  }

  if (type === 'chat.delta') {
    if (data.typing) {
      state.typing = true;
      emit('chat', { kind: 'typing' });
    } else if (data.delta) {
      state.typing = false;
      if (!state.stream.active) {
        state.stream.active = true;
        state.stream.startedAt = Date.now();
        state.stream.text = '';
      }
      state.stream.text += String(data.delta);
      emit('chat', { kind: 'stream' });
    }
    emit('event', event);
    return;
  }

  if (type === 'chat.typing') {
    state.typing = Boolean(data.typing ?? true);
    emit('chat', { kind: 'typing' });
    emit('event', event);
    return;
  }

  emit('event', event);
  if (STRUCTURAL.has(head)) scheduleRefresh(head === 'sys' ? 400 : 300);
}

/* ------------------------------------------------------------------ */
/* SSE client                                                          */
/* ------------------------------------------------------------------ */

let source = null;
let reconnectTimer = null;
let attempts = 0;
let everConnected = false;

function setSse(status) {
  if (state.sse === status) return;
  state.sse = status;
  emit('sse', status);
}

function scheduleReconnect() {
  if (reconnectTimer) window.clearTimeout(reconnectTimer);
  const delay = Math.min(900 * 2 ** attempts, 8000);
  attempts += 1;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connectSSE();
  }, delay);
}

/** Open the SSE stream from the current lastSeq. Safe to call repeatedly. */
export function connectSSE() {
  if (source) {
    source.close();
    source = null;
  }
  setSse(attempts > 0 ? 'reconnecting' : 'connecting');
  const url = `/api/events/stream?since=${encodeURIComponent(String(state.lastSeq || 0))}`;
  let stream;
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
    if (reconnected) scheduleRefresh(60); // catch up after downtime
  });

  stream.addEventListener('message', (messageEvent) => {
    if (source !== stream) return;
    if (!messageEvent.data) return;
    let event;
    try {
      event = JSON.parse(messageEvent.data);
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

export function disconnectSSE() {
  if (source) source.close();
  source = null;
  if (reconnectTimer) window.clearTimeout(reconnectTimer);
  reconnectTimer = null;
  setSse('offline');
}
