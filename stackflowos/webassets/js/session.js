/**
 * session.js — identity, browser sessions and the 401 login gate.
 *
 * The console is a single-operator control plane backed by an HttpOnly cookie.
 * This module owns:
 *   * the topbar identity chip (user + role, hidden when /api/sessions is not
 *     available so an older backend degrades instead of showing wrong data);
 *   * the settings → security session panel (list, revoke, rotate, sign out);
 *   * the viewer-role write gate used by unit actions / git rollback;
 *   * a blocking "session expired" screen fed by the `sfo:unauthorized` event
 *     that api.js dispatches on every HTTP 401.
 *
 * All server strings are rendered with textContent.
 */

import { api, isForbidden, isUnsupported } from './api.js';
import { notify, on } from './store.js';
import {
  ago,
  el,
  fmtDateTime,
  icon,
  loadingInline,
  statusPill,
  toast,
  toastError,
  withBusy,
} from './ui.js';
import { t } from './i18n.js';

/** @typedef {{id?:string,user?:string,role?:string,created_at?:number,last_seen?:number,current?:boolean}} SessionRow */

const view = {
  /** @type {SessionRow[]|null} */
  sessions: null,
  /** @type {{user:string,role:string,known:boolean}} */
  auth: { user: '', role: '', known: false },
  loading: false,
  error: '',
  unavailable: false,
  forbidden: false,
  currentId: '',
  gate: null,
};

/** Read the current identity (safe before the first /api/sessions answer). */
export function getAuth() {
  return { ...view.auth };
}

/**
 * Whether the current role may perform write operations.  Unknown identity
 * defaults to true: the server remains the authority and answers 403, which
 * every write path treats as a friendly refusal.  A known viewer is hidden.
 */
export function canWrite() {
  return view.auth.role !== 'viewer';
}

export function onSessionChange(handler) {
  return on('session', handler);
}

function lastSeen(row) {
  return Number(row?.last_seen) || 0;
}

function unwrapSessionList(data) {
  const payload = data && typeof data === 'object' ? data : {};
  const rows = Array.isArray(payload.sessions) ? payload.sessions
    : Array.isArray(payload.items) ? payload.items
      : Array.isArray(data) ? data : [];
  // The backend does not tag the caller's row.  An explicit marker wins; a
  // single-row answer is unambiguous (viewers only ever see their own); the
  // remaining case is a multi-row operator view, where the most recently used
  // session is the best available approximation for the identity chip.
  let current = payload.current && typeof payload.current === 'object' ? payload.current
    : rows.find((row) => row && (row.current || row.current_session)) || null;
  if (!current && rows.length === 1) current = rows[0];
  if (!current && rows.length > 1) {
    current = [...rows].sort((a, b) => lastSeen(b) - lastSeen(a))[0] || null;
  }
  return { rows, current, payload };
}

/** Fetch `/api/sessions`; quiet mode never toasts (used for the topbar chip). */
export async function loadSessions({ quiet = true, force = false } = {}) {
  if (view.loading) return view.sessions;
  if (!force && view.sessions && !quiet) return view.sessions;
  view.loading = true;
  view.error = '';
  view.unavailable = false;
  view.forbidden = false;
  try {
    const data = await api.getSessions();
    const { rows, current, payload } = unwrapSessionList(data);
    view.sessions = rows;
    view.currentId = String(current?.id || current?.session_id || payload.current_id || '');
    const user = current?.user || current?.username || payload.user || payload.username || '';
    const role = current?.role || payload.role || '';
    view.auth = { user: String(user || ''), role: String(role || ''), known: true };
    applyChip();
    notify('session', { auth: getAuth(), sessions: rows });
    return rows;
  } catch (error) {
    view.sessions = null;
    view.currentId = '';
    view.auth = { ...view.auth, known: false };
    view.unavailable = isUnsupported(error);
    view.forbidden = isForbidden(error);
    view.error = error?.message || String(error);
    applyChip();
    if (!quiet) {
      if (view.unavailable || view.forbidden) notify('session', { auth: getAuth(), sessions: null });
      else toastError(error);
    }
    return null;
  } finally {
    view.loading = false;
  }
}

/* ------------------------------------------------------------------ */
/* topbar chip                                                         */
/* ------------------------------------------------------------------ */

function chipNodes() {
  return {
    chip: document.getElementById('session-chip'),
    user: document.getElementById('session-user'),
    role: document.getElementById('session-role'),
  };
}

function applyChip() {
  const { chip, user, role } = chipNodes();
  if (!chip) return;
  if (!view.auth.known) {
    chip.hidden = true;
    return;
  }
  chip.hidden = false;
  if (user) user.textContent = view.auth.user || t('session.unknown');
  if (role) role.textContent = view.auth.role || '—';
  chip.dataset.role = view.auth.role || '';
  chip.title = `${t('session.current')}: ${view.auth.user || t('session.unknown')}`
    + (view.auth.role ? ` · ${view.auth.role}` : '');
}

/** Boot the session chip, the session bus and the 401 gate. */
export function initSession() {
  const { chip } = chipNodes();
  chip?.addEventListener('click', () => {
    notify('open-settings-tab', { tab: 'security' });
  });
  document.addEventListener('sfo:unauthorized', () => showLoginGate());
  on('lang', () => {
    applyChip();
    if (view.gate) renderGate(view.gate);
  });
  applyChip();
  loadSessions({ quiet: true }).catch(() => {});
}

/* ------------------------------------------------------------------ */
/* settings → security panel                                           */
/* ------------------------------------------------------------------ */

/**
 * Build the session/identity panel used inside settings → security.
 * The returned node owns its own refresh cycle.
 */
export function renderSessionPanel() {
  const root = el('section', { class: 'session-panel', 'aria-live': 'polite' });
  const head = el('div', { class: 'session-head' },
    el('div', {},
      el('h3', {}, t('session.title')),
      el('p', { class: 'session-hint' }, t('session.sub')),
    ),
  );
  const actions = el('div', { class: 'session-actions' });
  const body = el('div', { class: 'session-body' });
  root.append(head, actions, body);

  const refreshBtn = el('button', { class: 'btn btn-sm btn-ghost', type: 'button' },
    icon('refresh', 'ico ico-sm'), t('session.refresh'));
  const rotateBtn = el('button', { class: 'btn btn-sm', type: 'button' },
    icon('refresh', 'ico ico-sm'), t('session.rotate'));
  const logoutBtn = el('button', { class: 'btn btn-sm btn-danger', type: 'button' },
    icon('close', 'ico ico-sm'), t('session.logout'));
  actions.append(refreshBtn, rotateBtn, logoutBtn);

  const render = () => {
    const writes = canWrite() && !view.unavailable && !view.forbidden;
    rotateBtn.hidden = !writes;
    logoutBtn.hidden = !writes;
    body.replaceChildren();

    if (view.auth.known) {
      body.append(el('div', { class: 'session-identity' },
        el('span', { class: 'si-label' }, t('session.current')),
        el('span', { class: 'si-user mono' }, view.auth.user || t('session.unknown')),
        statusPill(view.auth.role || 'unknown'),
      ));
    }
    if (!writes) body.append(el('p', { class: 'field-hint session-viewer' }, t('session.viewer_hint')));

    if (view.loading && !view.sessions) {
      body.append(loadingInline(t('session.loading')));
      return;
    }
    if (view.unavailable) {
      body.append(note(t('session.unavailable')));
      return;
    }
    if (view.forbidden) {
      body.append(note(t('session.forbidden')));
      return;
    }
    if (view.error && !view.sessions) {
      body.append(note(view.error, true));
      return;
    }
    if (!view.sessions || !view.sessions.length) {
      body.append(el('p', { class: 'field-hint' }, t('session.empty')));
      return;
    }

    const table = el('table', { class: 'session-table' },
      el('thead', {}, el('tr', {},
        ['user', 'role', 'created', 'last_seen', ''].map((key) => el('th', { scope: 'col' },
          key === 'user' ? t('session.user')
            : key === 'role' ? t('session.role')
              : key === 'created' ? t('session.created')
                : key === 'last_seen' ? t('session.last_seen') : '')),
      )),
    );
    const tbody = el('tbody');
    for (const row of view.sessions) {
      const isCurrent = Boolean(row && (row.current || row.current_session
        || (row.id && row.id === view.currentId)));
      const revokeBtn = el('button', {
        class: 'btn btn-xs btn-danger',
        type: 'button',
        disabled: !writes || isCurrent,
        title: isCurrent ? t('session.this') : t('session.revoke'),
      }, t('session.revoke'));
      revokeBtn.addEventListener('click', () => {
        const id = row.id || row.session_id || '';
        if (!id || !canWrite()) return;
        if (!window.confirm(t('session.confirm_revoke', { id }))) return;
        withBusy(revokeBtn, async () => {
          try {
            await api.revokeSession(id);
            toast(t('session.revoked'), 'success');
            await loadSessions({ quiet: false, force: true });
            render();
          } catch (error) {
            toastError(error);
          }
        });
      });
      tbody.append(el('tr', { dataset: { current: String(isCurrent) } },
        el('td', {},
          el('span', { class: 'mono' }, row.user || row.username || '—'),
          isCurrent ? el('span', { class: 'tag session-current-tag' }, t('session.this')) : null),
        el('td', {}, statusPill(row.role || 'unknown')),
        el('td', { class: 'mono session-time', title: fmtDateTime(row.created ?? row.created_at) },
          ago(row.created ?? row.created_at)),
        el('td', { class: 'mono session-time', title: fmtDateTime(row.last_seen) }, ago(row.last_seen)),
        el('td', { class: 'session-cell-actions' }, revokeBtn),
      ));
    }
    table.append(tbody);
    body.append(table);
  };

  refreshBtn.addEventListener('click', () => withBusy(refreshBtn, async () => {
    await loadSessions({ quiet: false, force: true });
    render();
  }));
  rotateBtn.addEventListener('click', () => {
    if (!canWrite()) return;
    if (!window.confirm(t('session.confirm_rotate'))) return;
    withBusy(rotateBtn, async () => {
      try {
        await api.rotateSessions();
        toast(t('session.rotated'), 'success');
        await loadSessions({ quiet: false, force: true });
        render();
      } catch (error) {
        toastError(error);
      }
    });
  });
  logoutBtn.addEventListener('click', () => {
    if (!canWrite()) return;
    if (!window.confirm(t('session.confirm_logout'))) return;
    withBusy(logoutBtn, async () => {
      try {
        await api.logout();
        toast(t('session.logged_out'), 'success');
        showLoginGate();
      } catch (error) {
        toastError(error);
      }
    });
  });

  render();
  loadSessions({ quiet: false, force: true })
    .then(() => render())
    .catch(() => render());
  return root;
}

function note(text, warn = false) {
  return el('p', { class: `session-note${warn ? ' is-warn' : ''}` }, text);
}

/* ------------------------------------------------------------------ */
/* 401 / login gate                                                    */
/* ------------------------------------------------------------------ */

/** Show the blocking re-login screen (idempotent). */
export function showLoginGate() {
  if (view.gate && view.gate.isConnected) {
    renderGate(view.gate);
    return view.gate;
  }
  const gate = el('div', {
    class: 'auth-gate',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-labelledby': 'auth-gate-title',
  });
  view.gate = gate;
  renderGate(gate);
  document.body.append(gate);
  gate.querySelector('.auth-gate-primary')?.focus();
  return gate;
}

export function hideLoginGate() {
  view.gate?.remove();
  view.gate = null;
}

function renderGate(gate) {
  const reload = el('button', { class: 'btn btn-primary auth-gate-primary', type: 'button' },
    icon('refresh'), t('session.expired.reload'));
  reload.addEventListener('click', () => window.location.reload());
  const card = el('div', { class: 'auth-gate-card' },
    el('div', { class: 'auth-gate-glyph' }, icon('approvals')),
    el('h2', { id: 'auth-gate-title' }, t('session.expired.title')),
    el('p', {}, t('session.expired.text')),
    el('p', { class: 'auth-gate-hint' }, t('session.expired.hint')),
    el('div', { class: 'auth-gate-actions' }, reload),
  );
  gate.replaceChildren(card);
}
