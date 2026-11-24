/**
 * llm.js — LLM usage, quota, routing and circuit-breaker panels.
 *
 * `/api/llm/usage` and `/api/llm/models` are owned by the LLM workstream and
 * may be missing (older backend) or empty (no calls yet). Both cases render a
 * calm note/empty state instead of an error. The response shapes are read
 * tolerantly because the same contract allows a few natural spellings.
 *
 * The routes/fallback editor returns controls tagged with `data-cfg-path` so
 * the existing settings form saves them through `PATCH /api/config`; only
 * `llm.routes` and `llm.fallback` are touched here.
 */

import { api, isForbidden, isUnsupported } from './api.js';
import { state } from './store.js';
import { t } from './i18n.js';
import {
  appNote,
  bar,
  el,
  emptyState,
  icon,
  loadingInline,
  statusPill,
  textarea,
  toastError,
  withBusy,
} from './ui.js';

const USAGE_KEYS = ['prompt', 'tokens_in', 'input_tokens', 'prompt_tokens', 'tokens_input'];
const OUT_KEYS = ['completion', 'tokens_out', 'output_tokens', 'completion_tokens', 'tokens_output'];

function pick(source, keys, fallback = undefined) {
  if (!source || typeof source !== 'object') return fallback;
  for (const key of keys) {
    if (source[key] !== undefined && source[key] !== null) return source[key];
  }
  return fallback;
}

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function fmtInt(value) {
  return num(value).toLocaleString('en-US');
}

function fmtCost(value) {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'string' && Number.isNaN(Number(value))) return value;
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  const digits = n < 0.01 ? 5 : n < 1 ? 3 : 2;
  return `$${n.toFixed(digits)}`;
}

function listOf(data, ...keys) {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== 'object') return [];
  for (const key of keys) if (Array.isArray(data[key])) return data[key];
  return [];
}

function errorNote(kind, unavailableText, error) {
  if (kind === 'unavailable') return appNote(unavailableText, { iconName: 'alert' });
  if (kind === 'forbidden') return appNote(t('llm.usage.forbidden'), { warn: true });
  return appNote(error?.message || String(error), { warn: true });
}

function kindOf(error) {
  if (isUnsupported(error)) return 'unavailable';
  if (isForbidden(error)) return 'forbidden';
  return 'error';
}

/* ================================================================== */
/* usage panel                                                         */
/* ================================================================== */

/**
 * Build the usage + models panel. It owns its refresh button and state.
 */
export function renderLlmUsagePanel() {
  const root = el('section', { class: 'llm-panel', 'aria-live': 'polite' });
  const head = el('div', { class: 'cap-head' },
    el('h3', {}, t('llm.usage.title')),
    el('button', { class: 'btn btn-xs btn-ghost llm-refresh', type: 'button' },
      icon('refresh', 'ico ico-xs'), t('common.refresh')),
  );
  const usageBody = el('div', { class: 'llm-usage-body' });
  const modelsBody = el('div', { class: 'llm-models-body' });
  root.append(head, el('p', { class: 'form-hint' }, t('llm.usage.hint')), usageBody, modelsBody);

  const refresh = async () => {
    await Promise.all([loadUsage(usageBody), loadModels(modelsBody)]);
  };
  head.querySelector('.llm-refresh').addEventListener('click', (event) => {
    withBusy(event.currentTarget, refresh);
  });
  refresh();
  return root;
}

async function loadUsage(body) {
  body.replaceChildren(loadingInline(t('llm.usage.loading')));
  try {
    const data = await api.getLlmUsage('');
    body.replaceChildren(renderUsage(data));
  } catch (error) {
    const kind = kindOf(error);
    if (kind === 'unavailable') body.replaceChildren(appNote(t('llm.usage.unavailable'), { iconName: 'alert' }));
    else if (kind === 'forbidden') body.replaceChildren(appNote(t('llm.usage.forbidden'), { warn: true }));
    else {
      body.replaceChildren(appNote(t('llm.usage.error', { message: error?.message || String(error) }), { warn: true }));
      if (!isUnsupported(error)) toastError(error);
    }
  }
}

/**
 * Flatten the usage aggregates into table rows.  The backend reports
 * `providers` / `models` / `roles` as maps of bucket → counters; arrays are
 * accepted too so the panel keeps working if that shape is consolidated.
 */
function usageRows(data) {
  const rows = listOf(data, 'by_model', 'breakdown').slice();
  const addMap = (source, key, columns) => {
    if (!source || typeof source !== 'object' || Array.isArray(source)) return;
    for (const [name, bucket] of Object.entries(source)) {
      if (!bucket || typeof bucket !== 'object') continue;
      rows.push({ ...bucket, ...columns, [key]: name });
    }
  };
  addMap(data.providers, 'provider', {});
  addMap(data.models, 'model', {});
  addMap(data.roles, 'role', {});
  return rows;
}

/** Project budget bars: `projects` is a map of project id → counters. */
function usageBudgets(data) {
  const payload = data && typeof data === 'object' ? data : {};
  const budget = Number(payload.budget?.project_token_budget) || 0;
  if (budget <= 0) return [];
  const projects = payload.projects;
  if (!projects || typeof projects !== 'object' || Array.isArray(projects)) return [];
  return Object.entries(projects)
    .filter(([, bucket]) => bucket && typeof bucket === 'object')
    .map(([id, bucket]) => ({
      name: id,
      used: num(pick(bucket, ['total', 'tokens_used', 'used'], 0)),
      budget,
    }));
}

function renderUsage(data) {
  const payload = data && typeof data === 'object' ? data : {};
  const totals = payload.totals || payload.usage || payload.summary || payload;
  const rows = usageRows(payload);
  const budgets = usageBudgets(payload);
  const circuitRaw = payload.circuit_breaker || payload.breaker || payload.circuit || {};
  const circuitState = String(
    typeof circuitRaw === 'string' ? circuitRaw
      : pick(circuitRaw, ['state', 'status'], pick(payload, ['circuit_state', 'breaker_state'], '')),
  ).toLowerCase();
  const hasNumbers = [pick(totals, USAGE_KEYS), pick(totals, OUT_KEYS), pick(totals, ['requests', 'calls', 'count'])]
    .some((value) => value !== undefined);
  if (!hasNumbers && !rows.length && !budgets.length && !circuitState) {
    return emptyState({
      iconName: 'agents',
      title: t('llm.usage.empty.title'),
      text: t('llm.usage.empty.text'),
    });
  }

  const wrap = el('div', { class: 'llm-usage' });
  const limits = payload.limits && typeof payload.limits === 'object' ? payload.limits : {};
  const rateValue = (usedKey, limitKey, fallback) => {
    const limit = num(pick(limits, [limitKey], 0));
    if (limit > 0) return `${fmtInt(pick(limits, [usedKey], 0))}/${fmtInt(limit)}`;
    return fmtInt(pick(totals, fallback));
  };
  const metrics = el('div', { class: 'llm-metrics' });
  const metric = (label, value, tone = '') => el('div', { class: `llm-metric${tone ? ` is-${tone}` : ''}` },
    el('span', { class: 'lm-label' }, label),
    el('span', { class: 'lm-value mono' }, value));
  metrics.append(
    metric(t('llm.tokens_in'), fmtInt(pick(totals, USAGE_KEYS))),
    metric(t('llm.tokens_out'), fmtInt(pick(totals, OUT_KEYS))),
    metric(t('llm.cost'), fmtCost(pick(totals, ['cost_usd', 'cost', 'total_cost', 'cost_total']))),
    metric(t('llm.requests'), fmtInt(pick(totals, ['requests', 'calls', 'count']))),
    metric(t('llm.rpm'), rateValue('rpm_used', 'rpm_limit', ['rpm', 'requests_per_minute'])),
    metric(t('llm.tpm'), rateValue('tpm_used', 'tpm_limit', ['tpm', 'tokens_per_minute'])),
  );
  wrap.append(metrics);

  const circuitTone = circuitState === 'open' ? 'failed'
    : circuitState === 'half_open' || circuitState === 'half-open' ? 'degraded'
      : circuitState === 'closed' ? 'ready' : 'disabled';
  const circuitLabel = circuitState
    ? t(`llm.circuit.${circuitState === 'half-open' ? 'half_open' : circuitState}`, {},
      circuitState)
    : t('llm.circuit.unknown');
  wrap.append(el('div', { class: 'llm-circuit' },
    el('span', { class: 'lc-label' }, t('llm.circuit')),
    statusPill(circuitTone, circuitLabel),
    circuitRaw?.reason ? el('span', { class: 'lc-reason' }, String(circuitRaw.reason)) : null,
    circuitRaw?.failures !== undefined
      ? el('span', { class: 'lc-reason mono' }, `failures ${circuitRaw.failures}`)
      : null,
  ));

  if (rows.length) {
    const table = el('table', { class: 'llm-table' },
      el('thead', {}, el('tr', {},
        [t('llm.provider'), t('llm.model'), t('llm.role'), t('llm.tokens_in'), t('llm.tokens_out'), t('llm.cost'), t('llm.rpm'), t('llm.tpm')]
          .map((label) => el('th', { scope: 'col' }, label))),
      ),
    );
    const tbody = el('tbody');
    for (const row of rows) {
      if (!row || typeof row !== 'object') continue;
      tbody.append(el('tr', {},
        el('td', { class: 'mono' }, String(pick(row, ['provider', 'name'], row.provider || '—'))),
        el('td', { class: 'mono' }, String(pick(row, ['model', 'model_id'], '—'))),
        el('td', {}, String(pick(row, ['role', 'agent_role'], '—'))),
        el('td', { class: 'mono' }, fmtInt(pick(row, USAGE_KEYS))),
        el('td', { class: 'mono' }, fmtInt(pick(row, OUT_KEYS))),
        el('td', { class: 'mono' }, fmtCost(pick(row, ['cost_usd', 'cost', 'total_cost']))),
        el('td', { class: 'mono' }, fmtInt(pick(row, ['rpm', 'requests_per_minute']))),
        el('td', { class: 'mono' }, fmtInt(pick(row, ['tpm', 'tokens_per_minute']))),
      ));
    }
    table.append(tbody);
    wrap.append(el('div', { class: 'table-wrap' }, table));
  }

  if (budgets.length) {
    const budgetWrap = el('div', { class: 'llm-budgets' },
      el('h4', {}, t('llm.budget.title')));
    for (const item of budgets) {
      if (!item || typeof item !== 'object') continue;
      const budget = pick(item, ['budget', 'tokens_budget', 'limit', 'quota'], 0);
      const used = pick(item, ['used', 'total', 'tokens_used', 'used_tokens', 'spent'], 0);
      const ratio = num(budget) > 0 ? Math.min(100, (num(used) / num(budget)) * 100) : 0;
      const label = pick(item, ['name', 'project_name', 'project_id', 'id'], t('common.project'));
      const tone = ratio >= 90 ? 'danger' : ratio >= 75 ? 'warn' : '';
      budgetWrap.append(el('div', { class: 'llm-budget-row' },
        el('div', { class: 'lb-head' },
          el('span', { class: 'lb-name' }, String(label)),
          el('span', { class: 'lb-value mono' },
            t('llm.budget.used', { used: fmtInt(used), budget: fmtInt(budget) })),
        ),
        bar(ratio, { tone }),
      ));
    }
    if (budgetWrap.querySelectorAll('.llm-budget-row').length) wrap.append(budgetWrap);
  }
  return wrap;
}

/* ================================================================== */
/* models panel                                                        */
/* ================================================================== */

async function loadModels(body) {
  body.replaceChildren(loadingInline(t('llm.models.loading')));
  try {
    const data = await api.getLlmModels();
    body.replaceChildren(renderModels(data));
  } catch (error) {
    const kind = kindOf(error);
    if (kind === 'unavailable') body.replaceChildren(appNote(t('llm.models.unavailable'), { iconName: 'alert' }));
    else if (kind === 'forbidden') body.replaceChildren(appNote(t('llm.usage.forbidden'), { warn: true }));
    else body.replaceChildren(appNote(t('llm.error', { message: error?.message || String(error) }), { warn: true }));
  }
}

function modelHealth(row) {
  const value = pick(row, ['healthy', 'ok', 'available', 'up'], pick(row, ['status', 'health'], ''));
  if (value === true || value === 'true' || String(value).toLowerCase() === 'ok') return 'ready';
  if (String(value).toLowerCase() === 'degraded') return 'degraded';
  if (value === false || ['down', 'failed', 'error', 'unavailable'].includes(String(value).toLowerCase())) return 'failed';
  return String(value || 'unknown');
}

function renderModels(data) {
  const payload = data && typeof data === 'object' ? data : {};
  const models = listOf(payload, 'models', 'providers', 'items', 'routes');
  const routes = listOf(payload, 'routes', 'role_routes');
  const fallback = listOf(payload, 'fallback', 'fallback_order', 'fallbacks');
  if (!models.length && !routes.length && !fallback.length) {
    return el('div', { class: 'llm-models' },
      el('h4', {}, t('llm.models.title')),
      el('p', { class: 'field-hint' }, t('llm.models.empty')));
  }
  const wrap = el('div', { class: 'llm-models' });
  wrap.append(el('h4', {}, t('llm.models.title')));
  if (models.length) {
    const table = el('table', { class: 'llm-table' },
      el('thead', {}, el('tr', {},
        [t('llm.provider'), t('llm.model'), t('llm.role'), t('llm.health'), t('llm.rpm'), t('llm.tpm')]
          .map((label) => el('th', { scope: 'col' }, label)))));
    const tbody = el('tbody');
    for (const row of models) {
      if (!row || typeof row !== 'object') continue;
      const health = modelHealth(row);
      tbody.append(el('tr', {},
        el('td', { class: 'mono' }, String(pick(row, ['provider', 'name'], '—'))),
        el('td', { class: 'mono' }, String(pick(row, ['model', 'model_id'], '—'))),
        el('td', {}, String(pick(row, ['role', 'agent_role'], '—'))),
        el('td', {}, statusPill(health)),
        el('td', { class: 'mono' }, fmtInt(pick(row.limits || {}, ['rpm_limit'], pick(row, ['rpm', 'rpm_limit'])))),
        el('td', { class: 'mono' }, fmtInt(pick(row.limits || {}, ['tpm_limit'], pick(row, ['tpm', 'tpm_limit'])))),
      ));
    }
    table.append(tbody);
    wrap.append(el('div', { class: 'table-wrap' }, table));
  }
  if (routes.length) {
    const routeWrap = el('div', { class: 'llm-routes' }, el('h5', {}, t('llm.routes.title')));
    for (const route of routes) {
      const role = pick(route, ['role', 'name', 'id'], '—');
      const chain = pick(route, ['models', 'routes', 'chain', 'fallback'], route);
      const text = Array.isArray(chain)
        ? chain.map((item) => (typeof item === 'string' ? item : `${pick(item, ['provider'], '?')}/${pick(item, ['model'], '?')}`)).join(' → ')
        : String(chain);
      routeWrap.append(el('div', { class: 'llm-route-row' },
        el('span', { class: 'lr-role' }, String(role)),
        el('span', { class: 'lr-chain mono' }, text || '—'),
      ));
    }
    wrap.append(routeWrap);
  }
  if (fallback.length) {
    const chain = fallback.map((item) => (typeof item === 'string' ? item
      : `${pick(item, ['provider'], '?')}/${pick(item, ['model'], '?')}`));
    wrap.append(el('div', { class: 'llm-fallback' },
      el('span', { class: 'lf-label' }, t('llm.fallback')),
      el('span', { class: 'mono' }, chain.join(' → ') || t('llm.fallback.none')),
    ));
  }
  return wrap;
}

/* ================================================================== */
/* routes / fallback editor (settings → AI)                            */
/* ================================================================== */

/**
 * JSON textareas wired into the existing settings form collector
 * (`data-cfg-path` + `data-cfg-kind="json"`). Invalid JSON is rejected before
 * the PATCH is sent; the server still validates the final shape.
 */
export function renderRoutesEditor() {
  const config = state.config || {};
  const routes = config.llm?.routes;
  const fallback = config.llm?.fallback;
  const asText = (value) => {
    if (value === undefined || value === null) return '[]';
    try {
      return JSON.stringify(value, null, 2);
    } catch {
      return '[]';
    }
  };
  const routesArea = textarea({
    class: 'textarea mono llm-routes-json',
    rows: '5',
    spellcheck: 'false',
    'aria-label': t('settings.ai.routes.title'),
    dataset: {
      cfgPath: 'llm.routes',
      cfgKind: 'json',
      cfgInitial: asText(routes),
      cfgLabel: t('settings.ai.routes.title'),
    },
  });
  routesArea.value = asText(routes);
  const fallbackArea = textarea({
    class: 'textarea mono llm-routes-json',
    rows: '3',
    spellcheck: 'false',
    'aria-label': t('llm.fallback'),
    dataset: {
      cfgPath: 'llm.fallback',
      cfgKind: 'json',
      cfgInitial: asText(fallback),
      cfgLabel: t('llm.fallback'),
    },
  });
  fallbackArea.value = asText(fallback);
  return el('div', { class: 'llm-routes-editor' },
    el('div', { class: 'form-grid' },
      el('div', { class: 'field is-full' },
        el('label', {}, 'routes'),
        routesArea,
      ),
      el('div', { class: 'field is-full' },
        el('label', {}, 'fallback'),
        fallbackArea,
      ),
    ),
  );
}
