/**
 * units.js — the three-tier unit explorer.
 *
 * Layout: health overview → filters → grouped tables (system / service /
 * worker) → click a row to open a side drawer with the full descriptor, its
 * runtime location, lease, a hand-drawn SVG dependency graph, recent state
 * events and the actions the descriptor actually declares.
 *
 * Degradation rules:
 *   * The list comes from `/api/units?include=descriptor`; when that endpoint
 *     is missing we fall back to the `/api/state` snapshot.
 *   * A missing/forbidden detail endpoint shows a note and the snapshot data —
 *     it never turns into a red error toast loop.
 *   * "No units" (loaded, truly empty) and "broken" (request failed) are
 *     distinct empty states.
 */

import { api, isConflict, isForbidden, isUnsupported } from './api.js';
import { on, state } from './store.js';
import { canWrite, getAuth } from './session.js';
import { t } from './i18n.js';
import {
  ago,
  appNote,
  el,
  emptyState,
  fmtDateTime,
  icon,
  loadingInline,
  statusPill,
  toast,
  toastError,
  withBusy,
} from './ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

const TIERS = ['system', 'service', 'worker'];
const ONLINE = new Set(['ready', 'degraded', 'draining', 'online', 'suspect']);
const PROBLEM = new Set(['degraded', 'failed', 'unreachable', 'offline', 'quarantined', 'suspect']);
const SYSTEM_KINDS = new Set(['sys', 'store', 'scheduler', 'gateway', 'context']);
const SERVICE_KINDS = new Set(['approval', 'llm', 'tool', 'skill', 'plugin', 'mcp']);
const ACTION_LABELS = {
  probe: 'units.action.probe',
  restart: 'units.action.restart',
  disable: 'units.action.disable',
  enable: 'units.action.enable',
  drain: 'units.action.drain',
  undrain: 'units.action.undrain',
  stop: 'units.action.stop',
};
const ACTION_CONFIRM = new Set(['restart', 'disable', 'enable', 'drain', 'undrain', 'stop']);

const view = {
  units: /** @type {any[]|null} */ (null),
  summary: null,
  loading: false,
  loaded: false,
  error: '',
  broken: false,
  endpointUnavailable: false,
  filter: 'all',
  projectId: '',
  query: '',
  listToken: 0,
  /** @type {null|{id:string,unit:any,descriptor:any,events:any[],state:string,note:string,node:HTMLElement|null}} */
  drawer: null,
  drawerToken: 0,
  graphSeq: 0,
};

let initialized = false;
let reloadTimer = null;
let liveTimer = null;

/* ================================================================== */
/* lifecycle                                                           */
/* ================================================================== */

export function initUnits() {
  if (initialized) return;
  initialized = true;
  // The drawer keeps its own copy of recent unit events; live SSE updates are
  // coalesced so a burst of unit.state_changed frames does not reload detail.
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && view.drawer?.node) {
      event.preventDefault();
      closeUnitDrawer();
    }
  });
  // Snapshots include the unit table; refresh the explorer whenever the sheet
  // is open, coalescing bursts (unit.state_changed fans out to every client).
  on('state', () => {
    const layer = document.getElementById('sheet-layer');
    if (!layer || layer.hidden) return; // sheet closed: do not poll in the background
    if (document.querySelector('.units-app')) scheduleListReload();
  });
  on('session', () => {
    // Identity arrives after the first paint; refresh the action gate.
    if (view.drawer?.node) renderDrawer();
  });
}

export function onUnitsEvent(event) {
  const drawer = view.drawer;
  if (!drawer || !event) return;
  const type = String(event.type || '');
  if (!type.startsWith('unit')) return;
  const data = event.data || {};
  const id = data.unit_id || data.work_id || data.id;
  if (id !== drawer.id) return;
  drawer.events = pushEvent(drawer.events, event);
  if (drawer.descriptor) {
    if (data.to || data.state) drawer.descriptor = { ...drawer.descriptor, state: data.to || data.state };
    if (data.reason) drawer.descriptor = { ...drawer.descriptor, reason: data.reason };
  }
  scheduleDrawerRefresh();
}

/** Re-render the page (and the open drawer) after a language change. */
export function rerenderUnits() {
  syncUnitsSheet();
  rerender();
  if (view.drawer?.node) renderDrawer();
}

/** Called by apps.js when the sheet closes / switches app. */
export function closeUnitDrawer() {
  const drawer = view.drawer;
  view.drawer = null;
  view.drawerToken += 1;
  if (drawer?.node) {
    const last = drawer.node.dataset.returnFocus;
    drawer.node.remove();
    if (last) {
      const target = document.querySelector(`[data-unit-id="${cssEscape(last)}"]`);
      target?.focus?.({ preventScroll: true });
    }
  }
  document.body.classList.remove('unit-drawer-open');
}

/** Called on every sheet render; drops a drawer whose app is no longer open. */
export function syncUnitsSheet() {
  if (view.drawer && !view.drawer.node?.isConnected) view.drawer = null;
}

/* ================================================================== */
/* data                                                                */
/* ================================================================== */

function normalizeList(data) {
  const payload = Array.isArray(data) ? { units: data } : (data || {});
  const units = Array.isArray(payload.units) ? payload.units
    : Array.isArray(payload.items) ? payload.items : [];
  return { units, summary: payload.summary || null };
}

function currentUnits() {
  return view.units || state.units || [];
}

async function loadUnits({ quiet = false, force = false } = {}) {
  if (!force && view.loading) return;
  const token = ++view.listToken;
  view.loading = true;
  if (!quiet) view.error = '';
  try {
    // Summary is authoritative (critical-unit health, by-tier counts); it is
    // optional so an older backend still renders from the unit cards alone.
    const [data, summaryData] = await Promise.all([
      api.getUnits({ include: 'descriptor' }),
      api.getUnitsSummary().catch(() => null),
    ]);
    if (token !== view.listToken) return;
    const { units, summary } = normalizeList(data);
    view.summary = summaryData && typeof summaryData === 'object' ? summaryData : summary;
    if (!units.length && !summary && Array.isArray(state.units) && state.units.length) {
      // Old backend without the schema/summary wrapper: keep the snapshot.
      view.units = state.units;
    } else {
      view.units = units;
    }
    view.loaded = true;
    view.broken = false;
    view.endpointUnavailable = false;
    view.error = '';
  } catch (error) {
    if (token !== view.listToken) return;
    view.loaded = true;
    if (isUnsupported(error)) {
      view.endpointUnavailable = true;
      view.units = Array.isArray(state.units) ? state.units : [];
    } else if (isForbidden(error)) {
      view.broken = true;
      view.error = error.message || String(error);
      view.units = [];
    } else {
      view.broken = true;
      view.error = error.message || String(error);
      // A failed refresh must not hide units we already know about.
      if (!Array.isArray(view.units) && Array.isArray(state.units)) view.units = state.units;
    }
    if (!quiet && view.broken) toastError(error);
  } finally {
    if (token === view.listToken) view.loading = false;
  }
}

function scheduleListReload() {
  window.clearTimeout(reloadTimer);
  reloadTimer = window.setTimeout(() => {
    const layer = document.getElementById('sheet-layer');
    if (!layer || layer.hidden || !document.querySelector('.units-app')) return;
    loadUnits({ quiet: true, force: true }).then(() => {
      const node = document.querySelector('.sheet-body');
      if (node && view.drawer) renderUnitsInto(node);
    }).catch(() => {});
  }, 420);
}

function scheduleDrawerRefresh() {
  window.clearTimeout(liveTimer);
  liveTimer = window.setTimeout(() => {
    if (view.drawer?.node) renderDrawer();
  }, 260);
}

/** Refresh hook for the app-level SSE reducer (unit.* events). */
export function noteUnitStateEvent(event) {
  if (!event) return;
  if (String(event.type || '').startsWith('unit.')) {
    scheduleListReload();
    onUnitsEvent(event);
  }
}

/* ================================================================== */
/* classification                                                      */
/* ================================================================== */

function stateOf(unit) {
  if (!unit) return 'unknown';
  if (unit.state) return String(unit.state);
  const status = String(unit.status || '');
  return { online: 'ready', suspect: 'suspect', offline: 'offline', quarantined: 'quarantined' }[status]
    || status || 'unknown';
}

function tierOf(unit) {
  if (unit?.tier) return unit.tier;
  const kind = String(unit?.kind || '');
  if (unit?.work_id === 'sys.0' || SYSTEM_KINDS.has(kind)) return 'system';
  if (SERVICE_KINDS.has(kind)) return 'service';
  return 'worker';
}

function isOnline(unit) {
  return ONLINE.has(stateOf(unit));
}

function isProblem(unit) {
  return PROBLEM.has(stateOf(unit));
}

function isIndependent(unit) {
  const mode = unit?.runtime?.mode;
  if (mode === 'subprocess' || mode === 'sys' || mode === 'remote') return true;
  if (mode === 'in_process' || mode === 'declarative') return false;
  return unit?.pid != null;
}

function health(units) {
  if (!units.length) return 'unknown';
  const criticalDown = units.some((unit) => unit.critical
    && !['ready', 'degraded'].includes(stateOf(unit)));
  if (criticalDown) return 'down';
  if (units.some((unit) => isProblem(unit))) return 'degraded';
  return 'ok';
}

function countTier(units, tier) {
  const list = units.filter((unit) => tierOf(unit) === tier);
  return {
    total: list.length,
    online: list.filter(isOnline).length,
    busy: list.filter((unit) => unit.activity === 'busy').length,
  };
}

function runtimeModeOf(unit) {
  return String(unit?.runtime?.mode || (unit?.pid != null ? 'subprocess' : 'in_process'));
}

function runtimeText(unit) {
  const mode = runtimeModeOf(unit);
  if (mode === 'remote') return unit?.runtime?.host || unit?.host || t('units.runtime.remote');
  if (mode === 'in_process' || mode === 'declarative') {
    return unit?.runtime?.host_unit
      ? t('units.runtime.host', { host: unit.runtime.host_unit })
      : t(`units.runtime.${mode}`, {}, t('units.runtime.in_process'));
  }
  const bits = [t('units.runtime.subprocess')];
  if (unit?.runtime?.pid ?? unit?.pid) bits.push(t('units.runtime.pid', { pid: unit.runtime?.pid ?? unit.pid }));
  return bits.join(' · ');
}

function leaseText(unit) {
  const lease = unit?.lease || null;
  if (!lease || !lease.type) {
    if (tierOf(unit) === 'system') return t('units.lease.host');
    return t('units.lease.none');
  }
  const label = t(`units.lease.${lease.type}`, {}, lease.type);
  const ttl = Number(lease.ttl_s) > 0 ? ` · ${t('units.lease.ttl', { s: lease.ttl_s })}` : '';
  return `${label}${ttl}`;
}

function abilityNames(unit) {
  const abilities = Array.isArray(unit?.abilities) ? unit.abilities : [];
  return abilities.map((ability) => (
    typeof ability === 'string' ? ability : (ability?.name || ability?.tool || '')
  )).filter(Boolean);
}

function matchesQuery(unit, query) {
  if (!query) return true;
  const haystack = [
    unit.unit_id, unit.work_id, unit.label, unit.kind, unit.role,
    unit?.metadata?.description, unit.project_id,
  ].join(' ').toLowerCase();
  return haystack.includes(query);
}

function filteredUnits(units) {
  const query = view.query.trim().toLowerCase();
  return units.filter((unit) => {
    if (view.filter === 'issues' && !isProblem(unit)) return false;
    if (view.filter === 'processes' && !isIndependent(unit)) return false;
    if (view.projectId && String(unit.project_id || '') !== view.projectId) return false;
    return matchesQuery(unit, query);
  });
}

/* ================================================================== */
/* rendering: list                                                     */
/* ================================================================== */

export function renderUnitsApp() {
  const wrap = el('div', { class: 'units-app' });
  if (!view.loaded && !view.loading) {
    loadUnits({ quiet: true }).then(() => rerender()).catch(() => rerender());
  }
  const loading = !view.loaded && view.loading;
  const units = currentUnits();
  if (loading) {
    wrap.append(loadingInline(t('units.detail.loading')));
    return wrap;
  }

  wrap.append(renderOverview(units));
  wrap.append(renderFilters(units));
  wrap.append(appNote(t('units.note'), { iconName: 'units' }));

  if (view.endpointUnavailable) {
    wrap.append(appNote(t('units.detail.unavailable'), { warn: true, iconName: 'units' }));
  } else if (view.broken) {
    wrap.append(appNote(t('units.empty.broken.text')
      + (view.error ? `（${view.error}）` : ''), { warn: true }));
  }

  const filtered = filteredUnits(units);
  if (!units.length) {
    if (view.broken) {
      wrap.append(emptyState({
        iconName: 'alert',
        title: t('units.empty.broken.title'),
        text: t('units.empty.broken.text'),
        actions: [retryButton()],
      }));
    } else {
      wrap.append(emptyState({
        iconName: 'units',
        title: t('units.empty.none.title'),
        text: t('units.empty.none.text'),
        actions: [retryButton()],
      }));
    }
    return wrap;
  }
  if (!filtered.length) {
    wrap.append(emptyState({
      iconName: 'units',
      title: t('units.empty.filtered.title'),
      text: t('units.empty.filtered.text'),
      actions: [el('button', {
        class: 'suggestion-chip',
        type: 'button',
        onclick: () => {
          view.filter = 'all';
          view.projectId = '';
          view.query = '';
          rerender();
        },
      }, t('units.empty.clear_filters'))],
    }));
    return wrap;
  }

  for (const tier of TIERS) {
    const list = filtered.filter((unit) => tierOf(unit) === tier);
    if (!list.length) continue;
    wrap.append(renderTier(tier, list));
  }
  return wrap;
}

function rerender() {
  const body = document.querySelector('.sheet-body');
  if (body) renderUnitsInto(body);
}

function renderUnitsInto(container) {
  const scrollTop = container.scrollTop;
  const current = container.querySelector('.units-app');
  if (!current) return;
  const fresh = renderUnitsApp();
  current.replaceWith(fresh);
  container.scrollTop = scrollTop;
}

function retryButton() {
  const button = el('button', { class: 'btn btn-sm', type: 'button' }, icon('refresh'), t('common.retry'));
  button.addEventListener('click', () => withBusy(button, async () => {
    await loadUnits({ force: true });
    rerender();
  }));
  return button;
}

function renderOverview(units) {
  const overall = view.summary?.health || health(units);
  const problems = Array.isArray(view.summary?.problems)
    ? view.summary.problems
    : units.filter(isProblem).map((unit) => ({
      unit_id: unit.unit_id || unit.work_id,
      state: stateOf(unit),
      reason: unit.reason,
    }));
  // Tier counters are computed from the same snapshot the groups render, so
  // the overview can never disagree with the list (health/problems still come
  // from the authoritative backend summary when it exists).
  const online = units.filter(isOnline).length;
  const total = units.length;
  const stats = el('div', { class: 'unit-overview-stats' });
  for (const tier of TIERS) {
    const counts = countTier(units, tier);
    stats.append(el('button', {
      class: 'unit-tier-stat',
      type: 'button',
      title: t(`units.tier.${tier}.hint`),
      onclick: () => { view.filter = 'all'; rerender(); },
    },
    el('span', { class: 'uts-label' }, t(`units.tier.${tier}`)),
    el('span', { class: 'uts-value mono' },
      t('units.tier.online', { online: counts.online, total: counts.total })),
    tier === 'worker' && counts.busy
      ? el('span', { class: 'uts-busy' }, t('units.busy', { n: counts.busy }))
      : null,
    ));
  }

  const problemButton = el('button', {
    class: 'unit-problem-btn',
    type: 'button',
    dataset: { tone: problems.length ? (overall === 'down' ? 'down' : 'degraded') : 'ok' },
    onclick: () => { view.filter = view.filter === 'issues' ? 'all' : 'issues'; rerender(); },
  },
  el('span', { class: 'upb-label' }, t('units.problems')),
  el('span', { class: 'upb-value mono' }, problems.length
    ? String(problems.length)
    : t('units.problems.none')),
  );

  return el('div', { class: 'unit-overview', dataset: { health: overall } },
    el('div', { class: 'unit-health' },
      el('span', { class: 'uh-label' }, t('units.overview.health')),
      el('span', { class: 'uh-value' },
        el('span', { class: `status-dot is-${overall === 'ok' ? 'online' : overall === 'down' ? 'failed' : 'suspect'}`, 'aria-hidden': 'true' }),
        t(`units.health.${overall}`, {}, t('units.health.unknown'))),
      el('span', { class: 'uh-count mono' }, t('units.count', { online, total })),
    ),
    stats,
    problemButton,
  );
}

function renderFilters(units) {
  const chips = [
    { key: 'all', label: t('units.filter.all') },
    { key: 'issues', label: t('units.filter.issues') },
    { key: 'processes', label: t('units.filter.processes') },
  ];
  const projectIds = new Set(units.map((unit) => unit.project_id).filter(Boolean));
  for (const project of state.projects) {
    if (project?.id) projectIds.add(project.id);
  }
  const projectSelect = el('select', {
    class: 'select unit-project-filter',
    'aria-label': t('units.filter.project'),
  },
  el('option', { value: '' }, t('units.filter.all_projects')),
  ...[...projectIds].map((id) => {
    const name = state.projects.find((project) => project.id === id)?.name || id;
    return el('option', { value: id }, name);
  }),
  );
  projectSelect.value = view.projectId;
  projectSelect.addEventListener('change', () => {
    view.projectId = projectSelect.value;
    rerender();
  });

  const search = el('input', {
    class: 'input unit-search',
    type: 'search',
    value: view.query,
    placeholder: t('units.filter.search'),
    'aria-label': t('units.search.label'),
  });
  search.addEventListener('input', () => {
    view.query = search.value;
    rerender();
    const next = document.querySelector('.unit-search');
    if (next) {
      next.focus();
      const end = next.value.length;
      try { next.setSelectionRange(end, end); } catch { /* type=search */ }
    }
  });

  const refresh = el('button', { class: 'btn btn-sm btn-ghost', type: 'button' },
    icon('refresh', 'ico ico-sm'), t('common.refresh'));
  refresh.title = t('units.refresh');
  refresh.addEventListener('click', () => withBusy(refresh, async () => {
    await loadUnits({ force: true });
    rerender();
  }));

  return el('div', { class: 'unit-filters' },
    el('div', { class: 'filter-chips', role: 'group', 'aria-label': t('units.filter.all') },
      ...chips.map((chip) => el('button', {
        class: `chip${view.filter === chip.key ? ' is-active' : ''}`,
        type: 'button',
        'aria-pressed': String(view.filter === chip.key),
        'aria-label': chip.key === 'issues' ? t('units.filter.issues.aria') : chip.label,
        onclick: () => {
          view.filter = chip.key;
          rerender();
        },
      }, chip.label)),
    ),
    el('div', { class: 'unit-filter-tools' }, projectSelect, search, refresh),
  );
}

function renderTier(tier, units) {
  const counts = countTier(units, tier);
  const group = el('section', { class: `unit-group unit-group-${tier}`, dataset: { tier } },
    el('div', { class: 'unit-group-head' },
      el('div', {},
        el('h3', {}, t(`units.tier.${tier}`)),
        el('span', { class: 'unit-group-hint' }, t(`units.tier.${tier}.hint`)),
      ),
      el('span', { class: 'unit-group-count mono' },
        t('units.tier.online', { online: counts.online, total: counts.total }),
        tier === 'worker' && counts.busy
          ? el('span', { class: 'uts-busy' }, t('units.busy', { n: counts.busy }))
          : null,
      ),
    ),
  );
  const rows = el('div', { class: 'unit-rows', role: 'list' });
  for (const unit of units) rows.append(renderRow(unit));
  group.append(rows);
  return group;
}

function renderRow(unit) {
  const unitId = String(unit.unit_id || unit.work_id || '?');
  const stateValue = stateOf(unit);
  const abilities = abilityNames(unit);
  const project = state.projects.find((item) => item.id === unit.project_id);
  const sub = [unit.label, unit.kind, unit.role].filter(Boolean).join(' · ');
  const pill = statusPill(stateValue);
  if (unit.reason) pill.title = `${t('units.reason')}: ${unit.reason}`;
  const row = el('button', {
    class: 'unit-row',
    type: 'button',
    dataset: { unitId, state: stateValue, tier: tierOf(unit) },
    role: 'listitem',
    title: `${unitId} · ${sub || tierOf(unit)}`,
    onclick: () => openUnitDrawer(unitId, unit),
  },
  el('span', { class: 'ur-main' },
    el('span', { class: 'unit-id mono' }, unitId),
    el('span', { class: 'unit-sub' }, sub || '—'),
  ),
  el('span', { class: 'ur-state' }, pill),
  el('span', { class: 'ur-runtime' }, runtimeText(unit)),
  el('span', { class: 'ur-lease' }, leaseText(unit)),
  el('span', { class: 'ur-abilities' },
    abilities.length
      ? abilities.slice(0, 3).map((name) => el('span', { class: 'ability-pill' }, name))
      : el('span', { class: 'muted' }, '—'),
    abilities.length > 3
      ? el('span', { class: 'ability-pill' }, `+${abilities.length - 3}`)
      : null,
  ),
  el('span', { class: 'ur-project' }, project?.name || unit.project_id || '—'),
  el('span', { class: 'ur-seen mono' }, ago(unit.last_seen)),
  icon('chevron', 'ico ico-sm ur-chevron'),
  );
  return row;
}

/* ================================================================== */
/* drawer                                                              */
/* ================================================================== */

export async function openUnitDrawer(unitId, seed = null, { keepScroll = false } = {}) {
  const existing = view.drawer;
  if (existing && existing.id === unitId && existing.node?.isConnected && !keepScroll) {
    return;
  }
  // Prefer the caller's seed; otherwise locate the unit in the freshly loaded
  // list/snapshot so the drawer is useful even when /api/units/{id} is absent.
  const seedUnit = seed
    || currentUnits().find((unit) => String(unit.unit_id || unit.work_id) === String(unitId))
    || (state.units || []).find((unit) => String(unit.unit_id || unit.work_id) === String(unitId))
    || null;
  const returnFocus = document.activeElement?.dataset?.unitId || '';
  closeUnitDrawer();

  const layer = el('div', { class: 'unit-drawer-layer' });
  const backdrop = el('div', {
    class: 'unit-drawer-backdrop',
    'aria-hidden': 'true',
    onclick: () => closeUnitDrawer(),
  });
  const drawer = el('aside', {
    class: 'unit-drawer',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': `${t('units.detail.title')} ${unitId}`,
    tabindex: '-1',
    dataset: { returnFocus },
  });
  layer.dataset.returnFocus = returnFocus;
  layer.append(backdrop, drawer);
  document.body.append(layer);
  document.body.classList.add('unit-drawer-open');
  drawer.focus({ preventScroll: true });

  view.drawer = {
    id: unitId,
    unit: seedUnit,
    descriptor: seedUnit,
    actions: Array.isArray(seedUnit?.actions) ? seedUnit.actions : [],
    events: localEvents(unitId),
    state: 'loading',
    note: '',
    node: layer,
  };
  renderDrawer();
  await loadDetail(unitId);
  return view.drawer;
}

function localEvents(unitId) {
  return [...state.events]
    .filter((event) => {
      if (!String(event.type || '').startsWith('unit')) return false;
      const data = event.data || {};
      return (data.unit_id || data.work_id || data.id) === unitId;
    })
    .sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0))
    .slice(-20);
}

function pushEvent(events, event) {
  const key = event.id || event.seq;
  const next = key === undefined
    ? [...events, event]
    : [...events.filter((item) => (item.id || item.seq) !== key), event];
  return next.sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0)).slice(-20);
}

async function loadDetail(unitId, { quiet = false } = {}) {
  const drawer = view.drawer;
  if (!drawer || drawer.id !== unitId) return;
  const token = ++view.drawerToken;
  if (!quiet) drawer.state = 'loading';
  renderDrawer();
  try {
    const data = await api.getUnit(unitId);
    if (token !== view.drawerToken || view.drawer !== drawer) return;
    const descriptor = data?.unit || data?.descriptor
      || (data && (data.unit_id || data.work_id) ? data : drawer.unit);
    const events = Array.isArray(data?.events) ? data.events
      : Array.isArray(data?.recent_events) ? data.recent_events
        : Array.isArray(data?.state_events) ? data.state_events
          : drawer.events;
    drawer.descriptor = descriptor || drawer.unit;
    drawer.events = events.length ? events.slice(-20) : drawer.events;
    drawer.state = 'ready';
    drawer.note = '';
  } catch (error) {
    if (token !== view.drawerToken || view.drawer !== drawer) return;
    if (isUnsupported(error)) {
      drawer.state = 'unavailable';
      drawer.note = t('units.detail.unavailable');
    } else if (isForbidden(error)) {
      drawer.state = 'forbidden';
      drawer.note = t('units.detail.forbidden');
    } else {
      drawer.state = 'error';
      drawer.note = t('units.detail.error', { message: error?.message || String(error) });
    }
    if (!quiet && !isUnsupported(error) && !isForbidden(error)) toastError(error);
  } finally {
    if (token === view.drawerToken && view.drawer === drawer) renderDrawer();
  }
}

function renderDrawer() {
  const drawer = view.drawer;
  if (!drawer?.node) return;
  const box = drawer.node.querySelector('.unit-drawer');
  if (!box) return;
  box.replaceChildren(renderDrawerBody(drawer));
  if (drawer.state === 'loading') {
    box.classList.add('is-loading');
  } else {
    box.classList.remove('is-loading');
  }
}

function renderDrawerBody(drawer) {
  const unit = drawer.descriptor || drawer.unit || {};
  const unitId = drawer.id;
  const stateValue = stateOf(unit);
  const closeBtn = el('button', {
    class: 'icon-btn unit-drawer-close',
    type: 'button',
    'aria-label': t('units.drawer.close'),
    onclick: () => closeUnitDrawer(),
  }, icon('close'));

  const header = el('header', { class: 'unit-drawer-head' },
    el('div', { class: 'udd-title' },
      el('div', { class: 'udd-id-line' },
        el('span', { class: 'mono unit-id' }, unitId),
        statusPill(stateValue),
        unit.activity ? el('span', { class: 'tag' }, unit.activity) : null,
      ),
      el('div', { class: 'udd-sub' },
        el('span', {}, unit.label || unit.kind || '—'),
        el('span', { class: 'muted' }, '·'),
        el('span', { class: 'mono' }, `${tierOf(unit)} / ${unit.kind || '—'}`),
        unit.reason ? el('span', { class: 'udd-reason' }, unit.reason) : null,
      ),
    ),
    closeBtn,
  );

  const children = [header];
  if (drawer.note) {
    children.push(appNote(drawer.note, {
      warn: drawer.state === 'error' || drawer.state === 'forbidden',
      iconName: drawer.state === 'forbidden' ? 'alert' : 'units',
    }));
  }
  children.push(renderActions(unit, drawer));

  /* meta grid */
  const meta = el('div', { class: 'unit-meta-grid' });
  const addMeta = (label, value) => {
    if (value === null || value === undefined || value === '') return;
    meta.append(el('div', { class: 'umg-item' },
      el('span', { class: 'umg-label' }, label),
      el('span', { class: 'umg-value' }, value),
    ));
  };
  addMeta(t('units.detail.runtime'), runtimeText(unit));
  if (unit?.runtime?.host_unit) addMeta(t('units.host_unit'), unit.runtime.host_unit);
  if (unit.host) addMeta(t('units.host'), unit.host);
  if (unit.supervisor) addMeta(t('units.detail.supervisor'), unit.supervisor);
  if (Number(unit.restart_count) > 0) addMeta(t('units.detail.restarts'), String(unit.restart_count));
  if (unit.project_id) addMeta(t('common.project'), unit.project_id);
  if (unit.since) addMeta(t('units.since'), fmtDateTime(unit.since));
  children.push(meta);

  children.push(renderLease(unit));
  children.push(renderDependencies(unit, drawer));
  children.push(renderAbilities(unit));
  children.push(renderEndpoints(unit));
  children.push(renderEvents(drawer));
  children.push(renderDescriptor(unit));

  const fragment = document.createDocumentFragment();
  for (const child of children) if (child) fragment.append(child);
  return fragment;
}

function actionList(unit, drawer = null) {
  if (Array.isArray(drawer?.actions) && drawer.actions.length) return drawer.actions;
  if (Array.isArray(unit?.actions) && unit.actions.length) return unit.actions;
  return [];
}

function renderActions(unit, drawer) {
  const actions = actionList(unit, drawer);
  const wrap = el('section', { class: 'unit-actions-block' },
    el('div', { class: 'unit-section-title' },
      icon('terminal', 'ico ico-sm'), t('units.detail.actions')),
  );
  if (!actions.length) {
    wrap.append(el('p', { class: 'field-hint' }, t('units.detail.no_actions')));
    return wrap;
  }
  const writable = canWrite();
  const row = el('div', { class: 'unit-action-row' });
  if (!writable) {
    row.append(el('p', { class: 'field-hint' },
      t('units.action.forbidden', { role: getAuth().role || 'viewer' })));
  }
  for (const action of actions) {
    const label = t(ACTION_LABELS[action] || `units.action.${action}`, {}, String(action));
    const button = el('button', { class: 'btn btn-sm unit-action-btn', type: 'button', dataset: { action } },
      label);
    button.disabled = !writable;
    button.addEventListener('click', () => runAction(unit, drawer, action, button));
    row.append(button);
  }
  wrap.append(row);
  return wrap;
}

async function runAction(unit, drawer, action, button) {
  if (!canWrite()) {
    toast(t('units.action.forbidden', { role: getAuth().role || 'viewer' }), 'error');
    return;
  }
  const unitId = String(unit.unit_id || unit.work_id || drawer.id);
  if (ACTION_CONFIRM.has(action)) {
    const label = t(ACTION_LABELS[action] || `units.action.${action}`, {}, action);
    if (!window.confirm(t('units.action.confirm', { id: unitId, action: label }))) return;
  }
  button.disabled = true;
  button.dataset.busy = '1';
  const previous = button.textContent;
  button.textContent = t('units.action.running');
  try {
    if (action === 'probe') {
      await api.probeUnit(unitId);
      toast(t('units.action.probe_sent'), 'success');
    } else {
      await api.unitAction(unitId, action);
      toast(t('units.action.done', { action }), 'success');
    }
    await loadUnits({ quiet: true, force: true });
    await loadDetail(unitId, { quiet: true });
    const body = document.querySelector('.sheet-body');
    if (body) renderUnitsInto(body);
  } catch (error) {
    const refused = isForbidden(error)
      || /operator role|forbidden|denied|unauthor/i.test(String(error?.message || ''));
    if (refused) {
      toast(t('units.action.forbidden', { role: getAuth().role || 'viewer' }), 'error');
    } else if (isConflict(error)) {
      toast(t('units.action.conflict'), 'error');
      await loadDetail(unitId, { quiet: true });
    } else {
      toast(t('units.action.failed', { message: error?.message || String(error) }), 'error');
    }
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.dataset.busy = '0';
      button.textContent = previous;
    }
  }
}

function unitSection(title, ...children) {
  return el('section', { class: 'unit-section' },
    el('div', { class: 'unit-section-title' }, title),
    ...children,
  );
}

function renderLease(unit) {
  const lease = unit?.lease;
  const rows = [];
  if (!lease || !lease.type) {
    rows.push(el('p', { class: 'field-hint' }, t('units.detail.no_lease')));
  } else {
    const label = t(`units.lease.${lease.type}`, {}, lease.type);
    rows.push(el('div', { class: 'unit-kv' },
      el('span', { class: 'uk-label' }, t('units.lease.type')),
      el('span', { class: 'uk-value' }, label,
        Number(lease.ttl_s) > 0 ? el('span', { class: 'muted' }, ` · ${t('units.lease.ttl', { s: lease.ttl_s })}`) : null),
    ));
    if (lease.host_unit) {
      rows.push(el('div', { class: 'unit-kv' },
        el('span', { class: 'uk-label' }, t('units.host_unit')),
        el('span', { class: 'uk-value mono' }, lease.host_unit)));
    }
    if (lease.renewed_at) {
      rows.push(el('div', { class: 'unit-kv' },
        el('span', { class: 'uk-label' }, t('units.lease.renewed')),
        el('span', { class: 'uk-value' }, ago(lease.renewed_at),
          el('span', { class: 'muted' }, ` · ${fmtDateTime(lease.renewed_at)}`))));
    }
    if (lease.expires_at) {
      rows.push(el('div', { class: 'unit-kv' },
        el('span', { class: 'uk-label' }, t('units.lease.expires')),
        el('span', { class: 'uk-value' }, ago(lease.expires_at),
          el('span', { class: 'muted' }, ` · ${fmtDateTime(lease.expires_at)}`))));
    }
  }
  return unitSection(el('span', {}, icon('clock', 'ico ico-sm'), t('units.detail.lease')), ...rows);
}

/* ---- dependency graph ---- */

function dependencyIds(unit) {
  const requires = Array.isArray(unit.requires) ? unit.requires.filter(Boolean) : [];
  const wants = Array.isArray(unit.wants) ? unit.wants.filter(Boolean) : [];
  return { requires, wants };
}

function resolveDep(depId, units) {
  if (depId.includes('*')) {
    const prefix = depId.slice(0, depId.indexOf('*'));
    return units.some((unit) => String(unit.unit_id || unit.work_id || '').startsWith(prefix));
  }
  return units.some((unit) => (unit.unit_id || unit.work_id) === depId);
}

function truncate(text, max) {
  const value = String(text);
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  return node;
}

/**
 * A deliberately small directed graph: the unit on the left, one node per
 * dependency on the right. Solid arrows = requires, dashed = wants. No library,
 * no layout engine — deterministic and readable, with a text list underneath
 * for accessibility.
 */
function dependencyGraph(unit, units) {
  const { requires, wants } = dependencyIds(unit);
  const deps = [
    ...requires.map((id) => ({ id, kind: 'requires' })),
    ...wants.map((id) => ({ id, kind: 'wants' })),
  ];
  if (!deps.length) return el('p', { class: 'field-hint' }, t('units.deps.none'));

  const width = 520;
  const rowHeight = 40;
  const height = Math.max(150, 70 + deps.length * rowHeight);
  const selfX = 12;
  const selfY = height / 2 - 24;
  const selfW = 166;
  const depX = 314;
  const depW = 194;
  const seq = ++view.graphSeq;
  const reqMarker = `unit-dep-req-${seq}`;
  const wantMarker = `unit-dep-want-${seq}`;

  const svg = svgEl('svg', {
    class: 'unit-dep-graph',
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': `${t('units.detail.deps')}: ${deps.map((dep) => dep.id).join(', ')}`,
  });
  const defs = svgEl('defs');
  for (const [id, color] of [[reqMarker, 'var(--danger)'], [wantMarker, 'var(--warn)']]) {
    const marker = svgEl('marker', {
      id, viewBox: '0 0 10 10', refX: '9', refY: '5',
      markerWidth: '6', markerHeight: '6', orient: 'auto-start-reverse',
    });
    marker.append(svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: color }));
    defs.append(marker);
  }
  svg.append(defs);

  // self node
  const selfLabel = truncate(unit.unit_id || unit.work_id || '?', 22);
  svg.append(svgEl('rect', {
    x: selfX, y: selfY, width: selfW, height: 48, rx: 10,
    fill: 'rgba(124,92,255,0.14)', stroke: 'rgba(124,92,255,0.65)', 'stroke-width': '1.4',
  }));
  const selfText = svgEl('text', {
    x: selfX + 12, y: selfY + 20, fill: 'var(--text)', 'font-size': '11',
    'font-family': 'var(--mono)',
  });
  selfText.textContent = selfLabel;
  const selfSub = svgEl('text', {
    x: selfX + 12, y: selfY + 36, fill: 'var(--text-3)', 'font-size': '9.5',
  });
  selfSub.textContent = unit.label || unit.kind || 'unit';
  svg.append(selfText, selfSub);

  deps.forEach((dep, index) => {
    const y = 34 + index * rowHeight;
    const resolved = resolveDep(dep.id, units);
    const color = dep.kind === 'requires' ? 'var(--danger)' : 'var(--warn)';
    const startX = selfX + selfW;
    const startY = selfY + 24;
    const endX = depX - 10;
    const endY = y + 16;
    const curve = svgEl('path', {
      d: `M ${startX} ${startY} C ${startX + 90} ${startY}, ${endX - 90} ${endY}, ${endX} ${endY}`,
      fill: 'none',
      stroke: resolved ? color : 'var(--text-4)',
      'stroke-width': '1.4',
      'stroke-dasharray': dep.kind === 'wants' ? '5 4' : null,
      'marker-end': `url(#${dep.kind === 'requires' ? reqMarker : wantMarker})`,
      opacity: resolved ? '0.9' : '0.55',
    });
    svg.append(curve);
    svg.append(svgEl('rect', {
      x: depX, y, width: depW, height: 34, rx: 8,
      fill: resolved ? 'rgba(255,255,255,0.035)' : 'rgba(255,255,255,0.015)',
      stroke: resolved ? 'rgba(255,255,255,0.16)' : 'rgba(255,255,255,0.1)',
      'stroke-width': '1',
      'stroke-dasharray': resolved ? null : '4 4',
    }));
    const label = svgEl('text', {
      x: depX + 10, y: y + 15, fill: resolved ? 'var(--text-2)' : 'var(--text-4)',
      'font-size': '10', 'font-family': 'var(--mono)',
    });
    label.textContent = truncate(dep.id, 24);
    const kindText = svgEl('text', {
      x: depX + 10, y: y + 27, fill: 'var(--text-4)', 'font-size': '8.5',
    });
    kindText.textContent = dep.kind === 'requires'
      ? t('units.deps.requires')
      : t('units.deps.wants');
    svg.append(label, kindText);
    if (!resolved) {
      const missing = svgEl('text', {
        x: depX + depW - 46, y: y + 15, fill: 'var(--warn)', 'font-size': '8.5',
      });
      missing.textContent = t('units.deps.missing');
      svg.append(missing);
    }
  });

  const chips = el('div', { class: 'unit-dep-list' },
    requires.length
      ? el('div', { class: 'unit-dep-line' },
        el('span', { class: 'udl-kind requires' }, t('units.deps.requires')),
        ...requires.map((id) => el('span', {
          class: `dep-chip${resolveDep(id, units) ? '' : ' is-missing'}`,
          title: resolveDep(id, units) ? id : `${id} (${t('units.deps.missing')})`,
        }, id)))
      : null,
    wants.length
      ? el('div', { class: 'unit-dep-line' },
        el('span', { class: 'udl-kind wants' }, t('units.deps.wants')),
        ...wants.map((id) => el('span', {
          class: `dep-chip wants${resolveDep(id, units) ? '' : ' is-missing'}`,
          title: resolveDep(id, units) ? id : `${id} (${t('units.deps.missing')})`,
        }, id)))
      : null,
  );

  return el('div', { class: 'unit-dep-wrap' }, svg, chips);
}

function renderDependencies(unit, drawer) {
  const { requires, wants } = dependencyIds(unit);
  const units = currentUnits();
  const head = el('div', { class: 'unit-section-title' },
    icon('units', 'ico ico-sm'), t('units.detail.deps'));
  const graph = (requires.length || wants.length)
    ? dependencyGraph(unit, units)
    : el('p', { class: 'field-hint' }, t('units.deps.none'));
  const wrap = el('section', { class: 'unit-section unit-dep-section' }, head, graph);
  return wrap;
}

function renderAbilities(unit) {
  const names = abilityNames(unit);
  const provides = Object.entries(unit?.provides || {});
  const chips = el('div', { class: 'ability-list' });
  if (names.length) {
    for (const name of names) chips.append(el('span', { class: 'ability-pill' }, name));
  } else if (!provides.length) {
    chips.append(el('span', { class: 'field-hint' }, t('units.detail.no_abilities')));
  }
  if (provides.length) {
    chips.append(el('span', { class: 'ability-pill is-provides' },
      provides.map(([key, value]) => `${key}: ${value}`).join(' · ')));
  }
  return unitSection(el('span', {}, icon('check', 'ico ico-sm'), t('units.detail.abilities')), chips);
}

function renderEndpoints(unit) {
  const endpoints = Array.isArray(unit?.endpoints) ? unit.endpoints : [];
  const rows = [];
  for (const endpoint of endpoints) {
    if (!endpoint) continue;
    rows.push(el('div', { class: 'unit-kv' },
      el('span', { class: 'uk-label' }, `${endpoint.name || 'endpoint'} · ${endpoint.transport || '—'}`),
      el('span', { class: 'uk-value mono' }, endpoint.address || '—')));
  }
  if (!rows.length) {
    const legacy = unit?.ref || unit?.rpc_address;
    if (legacy) {
      rows.push(el('div', { class: 'unit-kv' },
        el('span', { class: 'uk-label' }, t('units.address')),
        el('span', { class: 'uk-value mono' }, legacy)));
    }
  }
  if (!rows.length) return null;
  return unitSection(el('span', {}, icon('terminal', 'ico ico-sm'), t('units.detail.endpoints')), ...rows);
}

function renderEvents(drawer) {
  const events = drawer.events || [];
  const list = el('div', { class: 'unit-event-list' });
  if (!events.length) {
    list.append(el('p', { class: 'field-hint' }, t('units.detail.no_events')));
  } else {
    for (const event of [...events].reverse()) {
      const data = event.data || {};
      const line = [
        data.from && data.to ? `${data.from} → ${data.to}` : (data.to || data.state || ''),
        data.reason ? `${t('units.reason')}: ${data.reason}` : '',
      ].filter(Boolean).join(' · ');
      list.append(el('div', { class: 'unit-event-row' },
        el('span', { class: 'ue-time mono' }, fmtDateTime(event.ts)),
        el('span', { class: 'ue-type' }, event.type || 'unit'),
        el('span', { class: 'ue-text' }, line || '—'),
      ));
    }
  }
  return unitSection(el('span', {}, icon('events', 'ico ico-sm'), t('units.detail.events')), list);
}

function renderDescriptor(unit) {
  let text = '';
  try {
    text = JSON.stringify(unit, null, 2);
  } catch {
    text = String(unit);
  }
  return el('details', { class: 'unit-descriptor' },
    el('summary', {}, t('units.detail.descriptor')),
    el('pre', { class: 'unit-descriptor-json' }, text),
  );
}

/* ------------------------------------------------------------------ */
/* small utils                                                         */
/* ------------------------------------------------------------------ */

function cssEscape(value) {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') return CSS.escape(value);
  return String(value).replace(/["\\]/g, '\\$&');
}
