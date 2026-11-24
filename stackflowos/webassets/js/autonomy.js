/**
 * autonomy.js — the "自治" app: make the OS self-driving loop visible and
 * controllable.
 *
 * The loop itself lives in the backend (`GET /api/autonomy/status`): one
 * snapshot = enabled/running, the latest patrol findings, the dispatcher's
 * records, the guardian's decisions and the operator policy.  This module is
 * a pure renderer plus a small form collector:
 *
 *   * rendering reads only the status object passed in by apps.js, so a stale
 *     answer can never invent a policy;
 *   * saving builds a *partial* autonomy patch (only changed fields) — it never
 *     round-trips the whole config;
 *   * every dynamic string is written through textContent via `el()`;
 *   * viewer roles see the policy read-only and cannot trigger a patrol.
 *
 * Countdowns: the static countdown nodes carry `data-autonomy-next` (epoch ms);
 * apps.js calls `tick()` once a second so no per-app timer is left behind when
 * the sheet closes.
 */

import { t } from './i18n.js';
import { canWrite } from './session.js';
import { state } from './store.js';
import {
  ago,
  appNote,
  el,
  emptyState,
  field,
  fmtDateTime,
  icon,
  input,
  loadingInline,
  riskPill,
  sectionHead,
  select,
  statusPill,
  textarea,
  toast,
  toastError,
  withBusy,
} from './ui.js';

const DISPATCH_MODES = ['off', 'tasks', 'agents'];
const APPROVE_MODES = ['off', 'observe', 'enforce'];
const RISKS = ['low', 'medium', 'high'];
const SEVERITIES = ['critical', 'warn', 'info'];
const SEVERITY_RANK = { critical: 0, warn: 1, info: 2 };

/** Capabilities an operator may delegate to the guardian (hard-denied ones are
 *  intentionally absent: deleting files, killing units, changing policy, ...). */
const CAPABILITIES = [
  'fs.read', 'fs.list', 'fs.write', 'shell.exec', 'net.fetch',
  'llm.chat', 'task.manage', 'agent.spawn', 'data.read', 'data.write',
];

const noop = () => {};

let mounted = false;

/* ================================================================== */
/* lifecycle                                                           */
/* ================================================================== */

/**
 * Prepare the module for an open sheet.  Called by apps.js on every visit to
 * `#autonomy` (apps.js owns the fetch + event debounce; this module keeps no
 * request timers of its own).
 */
export function mount() {
  mounted = true;
}

/** The sheet is closing: drop transient UI state so the next visit is fresh. */
export function unmount() {
  mounted = false;
}

/** Whether the autonomy pane is currently mounted (used by tests / debugging). */
export function isMounted() {
  return mounted;
}

/**
 * Advance the live "next patrol" countdowns in place.  Called from apps.js'
 * shared 1 s interval while the app is open; no timer is created here.
 */
export function tick(root = document) {
  if (!root || typeof root.querySelectorAll !== 'function') return;
  for (const node of root.querySelectorAll('[data-autonomy-next]')) {
    const target = Number(node.dataset.autonomyNext);
    if (!Number.isFinite(target) || target <= 0) continue;
    node.textContent = fmtRemaining(Math.round((target - Date.now()) / 1000));
  }
}

/* ================================================================== */
/* entry point                                                         */
/* ================================================================== */

/**
 * Render the whole autonomy app.
 *
 * @param {{
 *   status?: any|null,
 *   loading?: boolean,
 *   error?: string,
 *   unsupported?: boolean,
 *   handlers?: {
 *     onRefresh?: () => Promise<any>,
 *     onPatrol?: () => Promise<any>,
 *     onSave?: (patch: Record<string, any>) => Promise<any>,
 *     onOpenProject?: (projectId: string) => void,
 *     onOpenTask?: (projectId: string, taskId: string) => void,
 *   },
 * }} [options]
 * @returns {HTMLElement}
 */
export function render(options = {}) {
  const status = options.status || null;
  const loading = Boolean(options.loading);
  const error = options.error ? String(options.error) : '';
  const unsupported = Boolean(options.unsupported);
  const handlers = {
    onRefresh: noop,
    onPatrol: noop,
    onSave: noop,
    onOpenProject: noop,
    onOpenTask: noop,
    ...(options.handlers || {}),
  };

  const wrap = el('div', { class: 'autonomy-app' });
  wrap.append(toolbar(status, loading, handlers));

  if (unsupported) {
    wrap.append(appNote(t('autonomy.unavailable.text'), { warn: true, iconName: 'autonomy' }));
  }
  if (error) {
    wrap.append(appNote(t('autonomy.error', { message: error }), { warn: true }));
  }
  if (loading && !status) {
    wrap.append(loadingInline(t('autonomy.loading')));
    return wrap;
  }
  if (!status) {
    wrap.append(emptyState({
      iconName: 'autonomy',
      title: t('autonomy.unavailable.title'),
      text: t('autonomy.unavailable.text'),
      actions: [retryButton(handlers)],
    }));
    return wrap;
  }

  wrap.append(statusCard(status, handlers));
  wrap.append(findingsSection(status, handlers));
  wrap.append(dispatchSection(status, handlers));
  wrap.append(approvalSection(status, handlers));
  wrap.append(policyCard(status, handlers));
  tick(wrap);
  return wrap;
}

/* ================================================================== */
/* toolbar                                                             */
/* ================================================================== */

function toolbar(status, loading, handlers) {
  const counts = (status && status.counts) || {};
  const patrols = Number(counts.patrols) || 0;
  const label = status
    ? t('autonomy.toolbar.count', { n: patrols, errors: Number(counts.errors) || 0 })
    : t('autonomy.title');
  return el('div', { class: 'app-toolbar' },
    el('div', { class: 'toolbar-left' },
      el('span', { class: 'app-count' }, label),
      loading ? el('span', { class: 'app-count' }, t('autonomy.loading.short')) : null,
    ),
    el('div', { class: 'toolbar-right' },
      refreshButton(handlers),
    ),
  );
}

function refreshButton(handlers) {
  const button = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' },
    icon('refresh'), t('common.refresh'));
  button.addEventListener('click', () => withBusy(button, async () => {
    try {
      await handlers.onRefresh();
    } catch (error) {
      toastError(error);
    }
  }));
  return button;
}

function retryButton(handlers) {
  const button = el('button', { class: 'btn btn-sm', type: 'button' }, icon('refresh'), t('common.refresh'));
  button.addEventListener('click', () => withBusy(button, async () => {
    try {
      await handlers.onRefresh();
    } catch (error) {
      toastError(error);
    }
  }));
  return button;
}

/* ================================================================== */
/* hero: enabled switch, running state, live counters                  */
/* ================================================================== */

function statusCard(status, handlers) {
  const writable = canWrite();
  const enabled = Boolean(status.enabled);
  const running = Boolean(status.running);
  const report = status.report || null;
  const summary = severitySummary(status.findings, report);
  const nextAt = nextRunAt(status);

  const toggle = el('input', {
    type: 'checkbox',
    checked: enabled,
    disabled: !writable,
    'aria-label': t('autonomy.policy.enabled'),
  });
  if (writable) {
    toggle.addEventListener('change', async () => {
      const next = Boolean(toggle.checked);
      toggle.disabled = true;
      try {
        await handlers.onSave({ enabled: next });
        toast(next ? t('autonomy.enabled.toast') : t('autonomy.disabled.toast'), 'success');
      } catch (error) {
        toggle.checked = !next;
        toastError(error);
      } finally {
        if (toggle.isConnected) toggle.disabled = false;
      }
    });
  }

  const patrol = patrolButton(status, handlers);

  return el('section', { class: 'autonomy-hero' },
    el('div', { class: 'autonomy-hero-main' },
      el('div', { class: 'autonomy-title-line' },
        el('h3', { class: 'autonomy-title' }, t('autonomy.title')),
        enabled
          ? statusPill('active', t('autonomy.enabled'))
          : statusPill('disabled', t('autonomy.disabled')),
        running ? statusPill('working', t('autonomy.running')) : null,
      ),
      el('p', { class: 'autonomy-headline' },
        report && report.headline ? String(report.headline) : t('autonomy.never_run')),
      el('div', { class: 'autonomy-metrics' },
        metric(t('autonomy.last_run'), lastRunValue(status, report)),
        metric(t('autonomy.next_run'), nextRunValue(status, nextAt)),
        metric(t('autonomy.total_patrols'), el('span', { class: 'am-value mono' },
          t('autonomy.count.patrols', { n: Number((status.counts || {}).patrols) || 0 }))),
      ),
      el('div', { class: 'autonomy-counts' },
        countCell('critical', t('autonomy.severity.critical'), summary.critical),
        countCell('warn', t('autonomy.severity.warn'), summary.warn),
        countCell('info', t('autonomy.severity.info'), summary.info),
        countCell('total', t('autonomy.severity.total'), summary.total),
      ),
    ),
    el('div', { class: 'autonomy-hero-actions' },
      el('label', { class: 'autonomy-enabled' }, toggle, el('span', {}, t('autonomy.policy.enabled'))),
      running ? statusPill('working', t('autonomy.running')) : patrol,
      el('p', { class: 'autonomy-hint' },
        writable ? t('autonomy.patrol.hint') : t('autonomy.viewer_hint')),
    ),
  );
}

function patrolButton(status, handlers) {
  const running = Boolean(status && status.running);
  const writable = canWrite();
  const disabled = !writable || running;
  const button = el('button', {
    class: 'btn btn-sm btn-primary',
    type: 'button',
    disabled,
    title: disabled ? (writable ? t('autonomy.patrol.running_hint') : t('autonomy.viewer_hint')) : '',
  }, icon('autonomy', 'ico ico-sm'), running ? t('autonomy.patrol_running') : t('autonomy.patrol_now'));
  if (!disabled) {
    button.addEventListener('click', () => {
      const idleChildren = [...button.childNodes];
      withBusy(button, async () => {
        button.replaceChildren(icon('autonomy', 'ico ico-sm'), t('autonomy.patrol_running'));
        try {
          await handlers.onPatrol();
          toast(t('autonomy.patrol.done'), 'success');
        } catch (error) {
          toastError(error);
        } finally {
          if (button.isConnected) button.replaceChildren(...idleChildren);
        }
      });
    });
  }
  return button;
}

function metric(label, value) {
  const node = el('div', { class: 'autonomy-metric' },
    el('span', { class: 'am-label' }, label));
  node.append(value instanceof Node ? value : el('span', { class: 'am-value' }, String(value)));
  return node;
}

function countCell(severity, label, value) {
  return el('div', {
    class: 'autonomy-count',
    dataset: { severity },
  },
  el('span', { class: 'am-label' }, label),
  el('b', {}, String(Number(value) || 0)),
  );
}

function lastRunValue(status, report) {
  const finished = Number(status.last_run) || Number(report && report.finished_at) || 0;
  if (!finished) return el('span', { class: 'am-value' }, t('autonomy.never_run'));
  const trigger = report && report.trigger ? String(report.trigger) : '';
  return el('span', { class: 'am-value', title: fmtDateTime(finished) },
    ago(finished),
    trigger ? el('span', { class: 'autonomy-mode-hint' }, ` · ${triggerLabel(trigger)}`) : null);
}

function nextRunValue(status, nextAt) {
  if (!status.enabled || !nextAt) {
    return el('span', { class: 'am-value' }, t('autonomy.next_disabled'));
  }
  return el('span', {
    class: 'am-value mono',
    dataset: { autonomyNext: String(nextAt) },
    title: fmtDateTime(nextAt),
  }, fmtRemaining(Math.round((nextAt - Date.now()) / 1000)));
}

function triggerLabel(trigger) {
  return t(`autonomy.trigger.${trigger}`, {}, String(trigger || '—'));
}

/** Epoch-ms of the next scheduled patrol, derived from the relative answer. */
function nextRunAt(status) {
  const seconds = status && status.next_run_in_s;
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return 0;
  if (!status.enabled) return 0;
  return Date.now() + Math.max(0, Number(seconds)) * 1000;
}

function fmtRemaining(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${pad(minutes)}:${pad(secs)}`;
}

function severitySummary(findings, report) {
  const summary = (report && report.summary) || null;
  const result = {
    critical: summary ? Number(summary.critical) || 0 : 0,
    warn: summary ? Number(summary.warn) || 0 : 0,
    info: summary ? Number(summary.info) || 0 : 0,
    total: summary ? Number(summary.total) || 0 : 0,
  };
  if (!summary) {
    for (const finding of findings || []) {
      const severity = String(finding && finding.severity || 'info');
      if (!SEVERITIES.includes(severity)) continue;
      result[severity] += 1;
      result.total += 1;
    }
  }
  return result;
}

/* ================================================================== */
/* findings                                                            */
/* ================================================================== */

function findingsSection(status, handlers) {
  const findings = (Array.isArray(status.findings) ? status.findings : [])
    .filter(Boolean)
    .sort((a, b) => (SEVERITY_RANK[a.severity] ?? 9) - (SEVERITY_RANK[b.severity] ?? 9));
  const summary = severitySummary(findings, status.report);
  const hint = t('autonomy.findings.hint', {
    critical: summary.critical, warn: summary.warn, info: summary.info,
  });
  const section = el('section', { 'aria-labelledby': 'autonomy-findings-heading' },
    sectionHead(t('autonomy.findings.title'), { hint }));
  if (!findings.length) {
    section.append(el('div', { class: 'autonomy-empty' },
      icon('check', 'ico ico-sm'),
      el('span', {}, t('autonomy.findings.empty.title')),
      el('span', { class: 'autonomy-mode-hint' }, t('autonomy.findings.empty.text'))));
    return section;
  }
  const list = el('div', { class: 'autonomy-list' });
  for (const finding of findings.slice(0, 100)) list.append(findingItem(finding, handlers));
  section.append(list);
  return section;
}

function findingItem(finding, handlers) {
  const severity = String(finding.severity || 'info');
  const item = el('article', {
    class: 'autonomy-item',
    dataset: { severity },
  });
  item.append(
    el('div', { class: 'autonomy-item-head' },
      el('span', { class: 'pill', dataset: { severity } }, t(`autonomy.severity.${severity}`, {}, severity)),
      el('span', { class: 'autonomy-item-check' }, String(finding.check || '—')),
      el('span', { class: 'autonomy-item-title' }, String(finding.title || finding.check || '—')),
      finding.dispatchable ? el('span', { class: 'tag' }, t('autonomy.finding.dispatchable')) : null,
    ),
  );
  const sub = [];
  if (finding.project_id) {
    sub.push(el('span', {}, `${t('autonomy.finding.project')}：`,
      projectLink(finding.project_id, handlers)));
  }
  if (finding.task_id) {
    sub.push(el('span', {}, `${t('autonomy.finding.task')}：`,
      taskLink(finding.project_id, finding.task_id, handlers)));
  }
  if (finding.agent_id) sub.push(el('span', {}, `${t('autonomy.finding.agent')}：`, el('span', { class: 'mono' }, String(finding.agent_id))));
  if (finding.unit_id) sub.push(el('span', {}, `${t('autonomy.finding.unit')}：`, el('span', { class: 'mono' }, String(finding.unit_id))));
  if (finding.detected_at) sub.push(el('span', { title: fmtDateTime(finding.detected_at) }, ago(finding.detected_at)));
  if (sub.length) item.append(el('div', { class: 'autonomy-item-sub' }, ...sub));
  if (finding.detail) item.append(el('p', { class: 'autonomy-item-body' }, String(finding.detail)));
  if (finding.fix) {
    item.append(el('p', { class: 'autonomy-item-fix' },
      `${t('autonomy.finding.fix')}：${String(finding.fix)}`));
  }
  return item;
}

/* ================================================================== */
/* dispatches                                                          */
/* ================================================================== */

function dispatchSection(status, handlers) {
  const records = (Array.isArray(status.dispatches) ? status.dispatches : []).filter(Boolean);
  const mode = dispatchMode((status.policy || {}).dispatch_mode);
  const hint = t('autonomy.dispatch.hint', { mode: modeLabel(mode, 'dispatch'), n: records.length });
  const section = el('section', { 'aria-labelledby': 'autonomy-dispatch-heading' },
    sectionHead(t('autonomy.dispatch.title'), { hint }));
  if (!records.length) {
    section.append(el('div', { class: 'autonomy-empty' },
      icon('agents', 'ico ico-sm'),
      el('span', {}, t('autonomy.dispatch.empty.title')),
      el('span', { class: 'autonomy-mode-hint' }, t('autonomy.dispatch.empty.text'))));
    return section;
  }
  const list = el('div', { class: 'autonomy-list' });
  for (const record of records.slice(0, 50)) list.append(dispatchItem(record, handlers));
  section.append(list);
  return section;
}

function dispatchItem(record, handlers) {
  const action = String(record.action || '');
  const status = String(record.status || '');
  const statusMap = { ok: 'done', skipped: 'archived', error: 'failed' };
  const sub = [];
  if (record.role) sub.push(el('span', {}, `${t('autonomy.dispatch.role')}：`, el('span', { class: 'mono' }, String(record.role))));
  if (record.project_id) sub.push(el('span', {}, `${t('autonomy.finding.project')}：`, projectLink(record.project_id, handlers)));
  if (record.task_id) sub.push(el('span', {}, `${t('autonomy.dispatch.task')}：`, taskLink(record.project_id, record.task_id, handlers)));
  if (record.agent_id) sub.push(el('span', {}, `${t('autonomy.dispatch.agent')}：`, el('span', { class: 'mono' }, String(record.agent_id))));
  if (record.ts) sub.push(el('span', { title: fmtDateTime(record.ts) }, ago(record.ts)));
  return el('article', { class: 'autonomy-item' },
    el('div', { class: 'autonomy-item-head' },
      el('span', { class: 'tag' }, t(`autonomy.dispatch.action.${action}`, {}, action || '—')),
      statusPill(statusMap[status] || 'archived', t(`autonomy.dispatch.status.${status}`, {}, status || '—')),
      el('span', { class: 'autonomy-item-check' }, String(record.fingerprint || '')),
    ),
    sub.length ? el('div', { class: 'autonomy-item-sub' }, ...sub) : null,
    record.reason ? el('p', { class: 'autonomy-item-reason' }, String(record.reason)) : null,
  );
}

/* ================================================================== */
/* guardian decisions                                                  */
/* ================================================================== */

function approvalSection(status, handlers) {
  const records = (Array.isArray(status.approvals) ? status.approvals : []).filter(Boolean);
  const mode = approveMode((status.policy || {}).approve_mode);
  const hint = t('autonomy.approvals.hint', { mode: modeLabel(mode, 'approve'), n: records.length });
  const section = el('section', { 'aria-labelledby': 'autonomy-approvals-heading' },
    sectionHead(t('autonomy.approvals.title'), { hint }));
  if (!records.length) {
    section.append(el('div', { class: 'autonomy-empty' },
      icon('approvals', 'ico ico-sm'),
      el('span', {}, t('autonomy.approvals.empty.title')),
      el('span', { class: 'autonomy-mode-hint' }, t('autonomy.approvals.empty.text'))));
    return section;
  }
  const list = el('div', { class: 'autonomy-list' });
  for (const record of records.slice(0, 50)) list.append(approvalItem(record, handlers));
  section.append(list);
  return section;
}

function approvalItem(record, handlers) {
  const decision = String(record.decision || 'skip');
  const decisionMap = { allow: 'done', observe: 'waiting', skip: 'archived' };
  const sub = [];
  if (record.principal) sub.push(el('span', {}, `${t('autonomy.approval.principal')}：`, el('span', { class: 'mono' }, String(record.principal))));
  if (record.project_id) sub.push(el('span', {}, `${t('autonomy.finding.project')}：`, projectLink(record.project_id, handlers)));
  if (record.request_id) sub.push(el('span', {}, `${t('autonomy.approval.request')}：`, el('span', { class: 'mono' }, String(record.request_id))));
  if (record.ts) sub.push(el('span', { title: fmtDateTime(record.ts) }, ago(record.ts)));
  return el('article', { class: 'autonomy-item' },
    el('div', { class: 'autonomy-item-head' },
      statusPill(decisionMap[decision] || 'archived', t(`autonomy.approval.decision.${decision}`, {}, decision)),
      decision === 'observe' ? el('span', { class: 'tag' }, t('autonomy.approval.observe_tag')) : null,
      el('span', { class: 'autonomy-item-check' }, String(record.capability || '—')),
      riskPill(record.risk || 'low'),
    ),
    sub.length ? el('div', { class: 'autonomy-item-sub' }, ...sub) : null,
    record.reason ? el('p', { class: 'autonomy-item-reason' }, String(record.reason)) : null,
  );
}

/* ================================================================== */
/* policy form                                                         */
/* ================================================================== */

function policyCard(status, handlers) {
  const policy = status.policy && typeof status.policy === 'object' ? status.policy : {};
  const writable = canWrite();
  const form = el('form', { class: 'form-card', autocomplete: 'off' });
  form.append(
    el('h3', {}, t('autonomy.policy.title')),
    el('p', { class: 'form-hint' }, t('autonomy.policy.hint')),
  );

  const enabled = el('input', {
    type: 'checkbox',
    checked: Boolean(policy.enabled),
    disabled: !writable,
    dataset: { autonomyPolicy: 'enabled', autonomyKind: 'bool' },
  });
  form.append(el('div', { class: 'form-grid' },
    field(t('autonomy.policy.enabled'), enabled, { full: true, inline: true }),
    policySelect('dispatch_mode', t('autonomy.policy.dispatch_mode'), DISPATCH_MODES,
      policy.dispatch_mode, writable, 'dispatch'),
    policySelect('approve_mode', t('autonomy.policy.approve_mode'), APPROVE_MODES,
      policy.approve_mode, writable, 'approve'),
    policySelect('approve_risk_max', t('autonomy.policy.risk_max'), RISKS,
      policy.approve_risk_max, writable, 'risk'),
  ));

  // `<textarea>` ignores a `value` attribute; populate the property instead so
  // the saved shell allowlist is visible (and a partial save cannot clear it).
  const shellPatterns = textarea({
    class: 'textarea mono',
    rows: '2',
    placeholder: '^pytest\\b',
    disabled: !writable,
    dataset: { autonomyPolicy: 'approve_shell_patterns', autonomyKind: 'lines' },
  });
  shellPatterns.value = (Array.isArray(policy.approve_shell_patterns)
    ? policy.approve_shell_patterns : []).join('\n');

  form.append(
    el('div', { class: 'autonomy-policy-block' },
      el('div', { class: 'section-head' },
        el('h3', {}, t('autonomy.policy.capabilities')),
        el('span', { class: 'section-hint' }, t('autonomy.policy.capabilities.hint')),
      ),
      capabilityGrid(policy, writable),
      el('p', { class: 'form-hint' }, t('autonomy.policy.shell_hint')),
      el('p', { class: 'form-hint' }, t('autonomy.policy.net_hint')),
      el('div', { class: 'form-grid' },
        field(t('autonomy.policy.domains'),
          input({
            class: 'input mono',
            value: (Array.isArray(policy.approve_domains) ? policy.approve_domains : []).join(', '),
            placeholder: 'example.com, api.example.com',
            disabled: !writable,
            dataset: { autonomyPolicy: 'approve_domains', autonomyKind: 'list' },
          }), { hint: t('autonomy.policy.domains.hint') }),
        field(t('autonomy.policy.shell'),
          el('input', {
            type: 'checkbox',
            checked: Boolean(policy.approve_shell),
            disabled: !writable,
            dataset: { autonomyPolicy: 'approve_shell', autonomyKind: 'bool' },
          }), { inline: true }),
        field(t('autonomy.policy.shell_patterns'), shellPatterns, { full: true }),
      ),
    ),
  );

  form.append(
    el('div', { class: 'autonomy-policy-block' },
      sectionHead(t('autonomy.policy.quota'), { hint: t('autonomy.policy.quota.hint') }),
      el('div', { class: 'form-grid is-3' },
        quotaField('dispatch_max_agents', t('autonomy.policy.quota.agents'), policy, writable),
        quotaField('dispatch_max_tasks_per_hour', t('autonomy.policy.quota.tasks'), policy, writable),
        quotaField('dispatch_max_spawns_per_hour', t('autonomy.policy.quota.spawns'), policy, writable),
        quotaField('approve_max_per_hour', t('autonomy.policy.quota.approvals'), policy, writable),
      ),
    ),
  );

  const save = el('button', {
    class: 'btn btn-primary',
    type: 'submit',
    disabled: !writable,
  }, icon('check'), t('autonomy.policy.save'));
  form.append(el('div', { class: 'autonomy-policy-actions' },
    save,
    el('span', { class: 'field-hint' },
      writable ? t('autonomy.policy.save_hint') : t('autonomy.policy.viewer')),
  ));

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!canWrite()) {
      toast(t('autonomy.policy.viewer'), 'error');
      return;
    }
    let patch;
    try {
      patch = collectPolicyPatch(form, policy);
    } catch (error) {
      toast(error.message || String(error), 'error');
      return;
    }
    if (!Object.keys(patch).length) {
      toast(t('autonomy.policy.nothing'), 'info');
      return;
    }
    const refused = policyGuard(patch, policy);
    if (refused) {
      toast(refused, 'error');
      return;
    }
    withBusy(save, async () => {
      try {
        await handlers.onSave(patch);
        toast(t('autonomy.policy.saved'), 'success');
      } catch (error) {
        toastError(error);
      }
    });
  });

  return form;
}

function policySelect(key, label, values, currentValue, writable, kind) {
  const control = select({
    'aria-label': label,
    disabled: !writable,
    dataset: { autonomyPolicy: key, autonomyKind: 'str' },
  }, values.map((value) => ({ value, label: modeLabel(value, kind) })));
  control.value = String(currentValue || values[0]);
  return field(label, control);
}

function quotaField(key, label, policy, writable) {
  const value = policy[key];
  return field(label, el('input', {
    class: 'input mono',
    type: 'number',
    min: '0',
    step: '1',
    value: value === null || value === undefined ? '0' : String(value),
    'aria-label': label,
    disabled: !writable,
    dataset: { autonomyPolicy: key, autonomyKind: 'int' },
  }));
}

function capabilityGrid(policy, writable) {
  const selected = new Set(Array.isArray(policy.approve_capabilities)
    ? policy.approve_capabilities.map(String) : []);
  const grid = el('div', { class: 'autonomy-capability-grid' });
  for (const capability of CAPABILITIES) {
    grid.append(el('label', { class: 'autonomy-cap', title: capability },
      el('input', {
        type: 'checkbox',
        value: capability,
        checked: selected.has(capability),
        disabled: !writable,
        dataset: { autonomyCap: '1' },
      }),
      el('span', {}, t(`autonomy.cap.${capability}`, {}, capability),
        el('span', { class: 'mono' }, ` · ${capability}`)),
    ));
  }
  return grid;
}

/** Validate combinations the backend would reject, with a human hint first. */
function policyGuard(patch, policy) {
  const capabilities = Object.prototype.hasOwnProperty.call(patch, 'approve_capabilities')
    ? patch.approve_capabilities
    : (Array.isArray(policy.approve_capabilities) ? policy.approve_capabilities : []);
  const shell = Object.prototype.hasOwnProperty.call(patch, 'approve_shell')
    ? Boolean(patch.approve_shell)
    : Boolean(policy.approve_shell);
  const patterns = Object.prototype.hasOwnProperty.call(patch, 'approve_shell_patterns')
    ? patch.approve_shell_patterns
    : (Array.isArray(policy.approve_shell_patterns) ? policy.approve_shell_patterns : []);
  const domains = Object.prototype.hasOwnProperty.call(patch, 'approve_domains')
    ? patch.approve_domains
    : (Array.isArray(policy.approve_domains) ? policy.approve_domains : []);
  const risk = Object.prototype.hasOwnProperty.call(patch, 'approve_risk_max')
    ? patch.approve_risk_max
    : policy.approve_risk_max;
  const hasShell = capabilities.includes('shell.exec');
  const hasNet = capabilities.includes('net.fetch');

  if (hasShell) {
    if (!shell) return t('autonomy.policy.invalid_shell');
    if (!patterns.length) return t('autonomy.policy.invalid_shell');
    if (String(risk) !== 'high') return t('autonomy.policy.invalid_shell');
  }
  if (shell && !hasShell) return t('autonomy.policy.invalid_shell');
  if (hasNet && !domains.length) return t('autonomy.policy.invalid_net');
  return '';
}

/**
 * Build a partial autonomy patch: only fields whose control differs from the
 * server policy.  The whole config object is never sent back.
 */
function collectPolicyPatch(form, policy) {
  const patch = {};
  for (const control of form.querySelectorAll('[data-autonomy-policy]')) {
    const key = control.dataset.autonomyPolicy;
    const kind = control.dataset.autonomyKind || 'str';
    if (!key) continue;
    const before = policy[key];
    if (kind === 'bool') {
      const value = Boolean(control.checked);
      if (value !== Boolean(before)) patch[key] = value;
    } else if (kind === 'int') {
      const raw = String(control.value || '').trim();
      const value = Number(raw);
      if (raw === '' || !Number.isFinite(value) || !Number.isInteger(value)) {
        throw new Error(t('autonomy.policy.number_invalid', { field: key }));
      }
      if (value !== Number(before)) patch[key] = value;
    } else if (kind === 'list') {
      const value = String(control.value || '')
        .split(',').map((item) => item.trim()).filter(Boolean);
      const previous = Array.isArray(before) ? before.map(String) : [];
      if (JSON.stringify(value) !== JSON.stringify(previous)) patch[key] = value;
    } else if (kind === 'lines') {
      const value = String(control.value || '')
        .split('\n').map((item) => item.trim()).filter(Boolean);
      const previous = Array.isArray(before) ? before.map(String) : [];
      if (JSON.stringify(value) !== JSON.stringify(previous)) patch[key] = value;
    } else {
      const value = String(control.value || '');
      if (value !== String(before === null || before === undefined ? '' : before)) {
        patch[key] = value;
      }
    }
  }

  // Capability checkboxes are one logical value; keep the canonical order.
  const boxes = [...form.querySelectorAll('[data-autonomy-cap]')];
  if (boxes.length) {
    const selected = new Set(boxes.filter((box) => box.checked).map((box) => box.value));
    const value = CAPABILITIES.filter((capability) => selected.has(capability));
    const previous = Array.isArray(policy.approve_capabilities)
      ? policy.approve_capabilities.map(String) : [];
    const same = value.length === previous.length
      && [...value].sort().join('\n') === [...previous].sort().join('\n');
    if (!same) patch.approve_capabilities = value;
  }
  return patch;
}

/* ================================================================== */
/* small shared bits                                                   */
/* ================================================================== */

function dispatchMode(value) {
  return DISPATCH_MODES.includes(String(value)) ? String(value) : 'off';
}

function approveMode(value) {
  return APPROVE_MODES.includes(String(value)) ? String(value) : 'off';
}

function modeLabel(value, kind) {
  if (kind === 'dispatch') return t(`autonomy.mode.${value}`, {}, String(value));
  if (kind === 'approve') return t(`autonomy.mode.${value}`, {}, String(value));
  return t(`autonomy.risk.${value}`, {}, String(value));
}

function projectLink(projectId, handlers) {
  if (!projectId) return el('span', { class: 'muted' }, '—');
  const id = String(projectId);
  const project = state.projects.find((item) => item.id === id);
  return el('button', {
    class: 'autonomy-link',
    type: 'button',
    title: id,
    onclick: () => handlers.onOpenProject(id),
  }, project && project.name ? String(project.name) : id);
}

function taskLink(projectId, taskId, handlers) {
  if (!taskId) return el('span', { class: 'muted' }, '—');
  const id = String(taskId);
  if (!projectId) return el('span', { class: 'mono' }, id);
  const project = String(projectId);
  return el('button', {
    class: 'autonomy-link mono',
    type: 'button',
    title: id,
    onclick: () => handlers.onOpenTask(project, id),
  }, id);
}
