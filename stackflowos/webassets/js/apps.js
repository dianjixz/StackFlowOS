/**
 * apps.js — the slide-up application sheets.
 *
 * Projects (list + detail + task board), Agent roster, Units, Approvals,
 * Event console and Settings. Deep-linkable through location.hash:
 *   #projects #agents #units #approvals #events #settings #project/<id>
 */

import * as agentChat from './agentchat.js';
import { api } from './api.js';
import { applyConfig, loadState, on, state } from './store.js';
import * as units from './units.js';
import { renderCapabilitiesPane } from './capabilities.js';
import { renderLlmUsagePanel, renderRoutesEditor } from './llm.js';
import { canWrite, renderSessionPanel } from './session.js';
import {
  isDependencyBlocked,
  mountGitSection,
  renderTaskDependencySection,
  taskDeps,
  depBadgeText,
} from './projectextras.js';
import { getLang, setLang, t } from './i18n.js';
import {
  ago,
  agoShort,
  appNote,
  avatar,
  bar,
  clip,
  el,
  emptyState,
  eventSummary,
  field,
  fmtCountdown,
  fmtDateTime,
  fmtTime,
  fmtUptime,
  icon,
  input,
  loadingInline,
  replace,
  ring,
  riskPill,
  roleChip,
  roleLabel,
  sectionHead,
  select,
  statusLabel,
  statusPill,
  textarea,
  toast,
  toastError,
  toneForType,
  withBusy,
} from './ui.js';

const APP_META = {
  projects: { titleKey: 'app.projects', subKey: 'app.sub.projects', icon: 'projects' },
  agents: { titleKey: 'app.agents', subKey: 'app.sub.agents', icon: 'agents' },
  units: { titleKey: 'app.units', subKey: 'app.sub.units', icon: 'units' },
  approvals: { titleKey: 'app.approvals', subKey: 'app.sub.approvals', icon: 'approvals' },
  events: { titleKey: 'app.events', subKey: 'app.sub.events', icon: 'events' },
  settings: { titleKey: 'app.settings', subKey: 'app.sub.settings', icon: 'settings' },
};
const APP_SUB_KEYS = Object.fromEntries(
  Object.entries(APP_META).map(([key, meta]) => [key, meta.subKey]));

const ROLES = ['lead', 'architect', 'coder', 'tester', 'reviewer', 'researcher'];
const ROLE_OPTIONS = ROLES.map((role) => ({ value: role, label: roleLabel(role) }));
const TASK_STATUSES = ['todo', 'doing', 'review', 'done', 'blocked'];
const TASK_LABELS = { todo: '待办', doing: '进行中', review: '待审', done: '完成', blocked: '阻塞' };
const TASK_TRANSITIONS = {
  todo: ['doing', 'blocked'],
  doing: ['review', 'blocked', 'todo'],
  review: ['done', 'todo', 'blocked'],
  blocked: ['todo', 'doing'],
  done: [],
};
const TASK_COLUMNS = [
  { key: 'todo', label: '待办' },
  { key: 'doing', label: '进行中' },
  { key: 'review', label: '待审' },
  { key: 'done', label: '完成' },
];
const EVENT_FILTERS = ['all', 'chat', 'project', 'task', 'agent', 'unit', 'approval', 'rule', 'tool', 'failure', 'sys'];
const RESTART_LABELS = { 'transport.mode': '传输模式', 'web.port': 'Web 端口' };
// Agent statuses that count as "on duty".  panel.js keeps an identical local
// set; both must stay in sync until the vocabulary moves to a shared module.
const ACTIVE_AGENT_STATUSES = new Set(['queued', 'spawning', 'idle', 'working', 'waiting']);

/** @type {Record<string, HTMLElement>} */
const els = {};
let current = null;
let lastFocused = null;
let renderTimer = null;
let pendingRender = false;
let pointerDown = false;
let lastSheetSignature = '';
let consoleTimer = null;

const view = {
  projectTasks: [],
  tasksProjectId: '',
  tasksLoading: false,
  tasksError: '',
  tasksToken: 0,
  selectedTaskId: null,
  scrollToTask: false,
  agentFilter: 'all',
  rules: [],
  rulesLoading: false,
  recentDecisions: [],
  decisionsLoading: false,
  consoleEvents: [],
  consoleLoading: false,
  consoleError: '',
  consoleToken: 0,
  eventsType: 'all',
  eventsProject: '',
  settingsTab: 'general',
  settingsNotice: [],
  settingsReady: false,
  ruleForm: null,
};

/* ================================================================== */
/* boot / routing                                                      */
/* ================================================================== */

export function initApps() {
  els.layer = document.getElementById('sheet-layer');
  els.sheet = document.getElementById('sheet');
  els.backdrop = document.getElementById('sheet-backdrop');
  els.body = document.getElementById('sheet-body');
  els.title = document.getElementById('sheet-title');
  els.sub = document.getElementById('sheet-sub');
  els.icon = document.getElementById('sheet-icon');
  els.close = document.getElementById('sheet-close');
  if (!els.layer) return;

  els.close.addEventListener('click', () => closeSheet());
  els.backdrop.addEventListener('click', () => closeSheet());
  document.addEventListener('keydown', onDocumentKeydown);
  window.addEventListener('hashchange', applyHash);
  els.body.addEventListener('focusout', () => {
    window.setTimeout(() => {
      if (current && !isFormFocused() && pendingRender) {
        pendingRender = false;
        scheduleSheetRender();
      }
    }, 0);
  });

  // A re-render between mousedown and mouseup swallows the click, so hold
  // back live refreshes while a button is being pressed inside the sheet.
  els.body.addEventListener('pointerdown', () => { pointerDown = true; }, true);
  const pointerRelease = () => {
    if (!pointerDown) return;
    pointerDown = false;
    if (current && pendingRender && !isFormFocused()) {
      window.setTimeout(() => {
        if (!pendingRender) return;
        pendingRender = false;
        scheduleSheetRender();
      }, 0);
    }
  };
  document.addEventListener('pointerup', pointerRelease, true);
  document.addEventListener('pointercancel', pointerRelease, true);

  on('state', () => {
    if (!current) return;
    // The agent conversation has its own reducer (agentchat.js) and must not
    // be torn down by the roster's state re-render.
    if (current.name === 'agents' && current.agentId) return;
    if (current.name === 'settings' && view.settingsReady) return;
    if (current.name === 'approvals' && sheetSignature() === lastSheetSignature) return;
    if (isFormFocused() || pointerDown) {
      pendingRender = true;
      return;
    }
    scheduleSheetRender();
  });
  on('event', (event) => {
    if (!current) return;
    if (current.name === 'events') scheduleConsoleReload();
    if (current.name === 'approvals' && String(event.type || '').startsWith('approval.')) {
      scheduleDecisionsReload();
    }
    if (current.name === 'units') units.noteUnitStateEvent(event);
  });
  on('open-settings-tab', (detail) => {
    view.settingsTab = detail?.tab || 'security';
    if (current?.name === 'settings') {
      renderSheet({ preserveScroll: false });
    } else {
      openApp('settings');
    }
  });

  window.setInterval(tickCountdowns, 1000);
  applyHash();
}

function parseHash() {
  const raw = location.hash.replace(/^#\/?/, '');
  if (!raw) return null;
  const parts = raw.split('/');
  const name = parts[0];
  if (name === 'project' && parts[1]) {
    return { app: 'projects', projectId: decodeURIComponent(parts.slice(1).join('/')) };
  }
  if (name === 'agent' && parts[1]) {
    return {
      app: 'agents',
      projectId: null,
      agentId: decodeURIComponent(parts.slice(1).join('/')),
    };
  }
  if (Object.prototype.hasOwnProperty.call(APP_META, name)) {
    return { app: name, projectId: null, agentId: null };
  }
  return null;
}

function applyHash() {
  const route = parseHash();
  if (!route) {
    if (current) hideSheet({ clearHash: false });
    return;
  }
  showApp(route.app, route.projectId, {
    fromHash: true,
    agentId: route.agentId || null,
  });
}

/**
 * Open an app, a project detail, or one agent's conversation, reflecting it
 * in the URL hash (`#agent/<id>` is deep-linkable).
 */
export function openApp(name, targetId = null) {
  if (name === 'project') {
    if (!targetId) return;
    const target = `#project/${encodeURIComponent(targetId)}`;
    if (location.hash === target) applyHash();
    else location.hash = target;
    return;
  }
  if (name === 'agent') {
    if (!targetId) return;
    const target = `#agent/${encodeURIComponent(targetId)}`;
    if (location.hash === target) applyHash();
    else location.hash = target;
    return;
  }
  if (!Object.prototype.hasOwnProperty.call(APP_META, name)) return;
  const target = `#${name}`;
  if (location.hash === target) applyHash();
  else location.hash = target;
}

export function closeSheet() {
  hideSheet({ clearHash: true });
}

/** Re-render the open app (and its header) after a language change. */
export function rerenderAfterLanguageChange() {
  if (!current) return;
  const meta = APP_META[current.name];
  if (meta) {
    setSheetHeader(
      current.projectId && current.name === 'projects'
        ? (state.projects.find((project) => project.id === current.projectId)?.name || t(meta.titleKey))
        : t(meta.titleKey),
      t(meta.subKey),
      meta.icon,
    );
  }
  renderSheet({ preserveScroll: false });
}

function hideSheet({ clearHash }) {
  const hadCurrent = Boolean(current);
  current = null;
  pendingRender = false;
  units.closeUnitDrawer();
  exitAgentChatMode();
  if (els.layer) els.layer.hidden = true;
  document.body.classList.remove('sheet-open');
  updateDockActive();
  if (clearHash && location.hash) {
    history.replaceState(null, '', `${location.pathname}${location.search}`);
  }
  if (hadCurrent && lastFocused && lastFocused.isConnected) {
    lastFocused.focus({ preventScroll: true });
  }
}

function showApp(name, projectId = null,
                   { fromHash = false, agentId = null } = {}) {
  if (!APP_META[name]) return;
  if (!fromHash) {
    openApp(name, projectId);
    return;
  }
  const wasHidden = els.layer.hidden;
  if (wasHidden) lastFocused = document.activeElement;
  current = { name, projectId: projectId || null, agentId: agentId || null };
  els.layer.hidden = false;
  document.body.classList.add('sheet-open');
  setSheetHeader(t(APP_META[name].titleKey), t(APP_META[name].subKey), APP_META[name].icon);
  updateDockActive();
  if (wasHidden) els.sheet.focus({ preventScroll: true });

  view.settingsReady = name === 'settings' ? false : view.settingsReady;
  view.selectedTaskId = null;
  if (name === 'projects' && projectId) {
    if (view.tasksProjectId !== projectId) {
      view.projectTasks = [];
      view.tasksError = '';
    }
    view.tasksProjectId = projectId;
    loadTasks(projectId);
  }
  if (name === 'approvals') {
    view.rules = state.rules.slice();
    loadRules();
    loadDecisions();
  }
  if (name === 'events') loadConsole();
  renderSheet({ preserveScroll: false });
}

function renderAgentChatSheet() {
  els.body.classList.add('is-agent-chat');
  els.sheet.classList.add('is-agent-chat');
  const agent = state.agents.find((item) => item.id === current.agentId) || null;
  const root = agentChat.mount({ agentId: current.agentId, agent });
  if (root.parentElement !== els.body || els.body.children.length !== 1) {
    els.body.replaceChildren(root);
  }
}

function exitAgentChatMode() {
  if (!els.body) return;
  els.body.classList.remove('is-agent-chat');
  if (els.sheet) els.sheet.classList.remove('is-agent-chat');
  agentChat.unmount();
}

function setSheetHeader(title, sub, iconName) {
  els.title.textContent = title;
  els.sub.textContent = sub || '';
  replace(els.icon, icon(iconName));
}

function updateDockActive() {
  const active = current ? current.name : '';
  document.querySelectorAll('.dock-btn').forEach((button) => {
    const matches = button.dataset.openApp === active;
    button.classList.toggle('is-active', matches);
    if (matches) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
}

function onDocumentKeydown(event) {
  if (!els.layer || els.layer.hidden) return;
  if (event.key === 'Escape') {
    // The unit drawer owns Escape while it is open; closing the whole sheet
    // underneath it would be surprising.
    if (document.querySelector('.unit-drawer-layer')) return;
    event.preventDefault();
    closeSheet();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = [...els.sheet.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
    'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((node) => node.getClientRects().length > 0);
  if (!focusable.length) {
    event.preventDefault();
    els.sheet.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function isFormFocused() {
  const active = document.activeElement;
  if (!active || !els.body || !els.body.contains(active)) return false;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName) || active.isContentEditable;
}

// Agents emit events constantly; the approvals sheet only needs to redraw
// when the pending list (or project names shown on cards) actually changes.
function sheetSignature() {
  if (!current || current.name !== 'approvals') return '';
  return JSON.stringify([
    state.loaded,
    state.approvals.map((item) => [item.id, item.status, item.expires_at]),
    state.projects.map((item) => [item.id, item.name]),
  ]);
}

function scheduleSheetRender() {
  if (renderTimer) return;
  renderTimer = window.setTimeout(() => {
    renderTimer = null;
    if (current) renderSheet();
  }, 80);
}

function renderSheet({ preserveScroll = true } = {}) {
  if (!current) return;
  if (current.name === 'agents' && current.agentId) {
    renderAgentChatSheet();
    return;
  }
  exitAgentChatMode();
  const scrollTop = preserveScroll ? els.body.scrollTop : 0;
  lastSheetSignature = sheetSignature();
  els.body.replaceChildren();
  let content = null;
  switch (current.name) {
    case 'projects':
      content = renderProjectsApp();
      break;
    case 'agents':
      content = renderAgentsApp();
      break;
    case 'units':
      content = renderUnitsApp();
      break;
    case 'approvals':
      content = renderApprovalsApp();
      break;
    case 'events':
      content = renderEventsApp();
      break;
    case 'settings':
      content = renderSettingsApp();
      if (state.config) view.settingsReady = true;
      break;
    default:
      content = emptyState({ iconName: 'alert', title: '未知应用' });
  }
  els.body.append(content);
  els.body.scrollTop = scrollTop;
  tickCountdowns();
  if (view.scrollToTask) {
    view.scrollToTask = false;
    window.requestAnimationFrame(() => {
      els.body.querySelector('.task-detail')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
  }
}

/* ================================================================== */
/* projects                                                            */
/* ================================================================== */

function sortedProjects() {
  const rank = { active: 0, review: 1, degraded: 2, planning: 3, done: 4, archived: 5 };
  return [...state.projects].sort((a, b) => {
    const ra = rank[a.degraded ? 'degraded' : a.status] ?? 9;
    const rb = rank[b.degraded ? 'degraded' : b.status] ?? 9;
    if (ra !== rb) return ra - rb;
    return (b.updated_at || 0) - (a.updated_at || 0);
  });
}

function renderProjectsApp() {
  if (current.projectId) {
    const project = state.projects.find((item) => item.id === current.projectId);
    if (!project) {
      if (!state.loaded) return loadingInline('载入项目…');
      return el('div', {},
        backButton(),
        emptyState({
          iconName: 'projects',
          title: '项目不存在',
          text: `没有找到项目 ${current.projectId}。它可能已被删除，或后端状态尚未同步。`,
        }),
      );
    }
    return renderProjectDetail(project);
  }
  return renderProjectsList();
}

function backButton(label = '项目') {
  return el('button', {
    class: 'back-btn',
    type: 'button',
    onclick: () => openApp('projects'),
  }, icon('back'), label);
}

function renderProjectsList() {
  const wrap = el('div', {});
  const projects = sortedProjects();

  wrap.append(el('div', { class: 'app-toolbar' },
    el('div', { class: 'toolbar-left' },
      el('span', { class: 'app-count' }, `${projects.length} 个项目`),
    ),
    el('div', { class: 'toolbar-right' },
      refreshButton(() => loadState({ quiet: true })),
    ),
  ));

  if (!state.loaded) {
    wrap.append(loadingInline('载入项目…'));
  } else if (!projects.length) {
    wrap.append(emptyState({
      iconName: 'projects',
      title: '还没有项目',
      text: '在左侧对话框说「创建一个项目：…」，主理人会马上拆任务并派工。',
    }));
  } else {
    wrap.append(el('div', { class: 'project-rows' }, ...projects.map(projectRow)));
  }

  wrap.append(renderNewProjectForm());
  wrap.append(renderAttachProjectForm());
  return wrap;
}

function projectRow(project) {
  const counts = project.counts || {};
  return el('button', {
    class: 'project-row',
    type: 'button',
    onclick: () => openApp('project', project.id),
  },
  el('div', { class: 'pr-main' },
    el('div', { class: 'pr-title-line' },
      el('span', { class: 'pr-name' }, project.name || '未命名项目'),
      statusPill(project.degraded ? 'degraded' : project.status),
    ),
    el('div', { class: 'pr-goal' }, project.goal || project.brief || '—'),
    el('div', { class: 'pr-counts mono' },
      `todo ${Number(counts.todo) || 0} · doing ${Number(counts.doing) || 0} · ` +
      `review ${Number(counts.review) || 0} · done ${Number(counts.done) || 0}`),
  ),
  el('div', { class: 'pr-side' },
    el('div', { class: 'pr-progress' },
      bar(project.progress || 0, { tone: project.status === 'done' ? 'ok' : '' }),
    ),
    el('span', { class: 'pr-counts mono' }, `${Math.round(project.progress || 0)}%`),
    el('span', { class: 'pr-age' }, agoShort(project.updated_at)),
    icon('chevron', 'ico ico-sm'),
  ));
}

function renderAttachProjectForm() {
  const path = input({ placeholder: '/home/you/existing-project', 'aria-label': '本地目录路径' });
  const name = input({ placeholder: '默认使用目录名', maxlength: '80', 'aria-label': '项目名称' });
  const goal = input({ placeholder: '可选：接入后想推进什么', 'aria-label': '目标' });
  const allow = input({ type: 'checkbox' });
  const submit = el('button', { class: 'btn btn-ghost btn-sm', type: 'submit' }, icon('plus'), '接入目录');
  const form = el('form', { class: 'form-card' },
    el('h3', {}, '接入已有目录'),
    el('p', { class: 'form-hint' },
      '把一个已存在的本地目录接入为项目。目录不会被复制或删除；默认 Agent 只读，'
      + '勾选后才允许写。'),
    el('div', { class: 'form-grid' },
      field('本地目录', path, { full: true, hint: '必须是已存在的绝对路径。' }),
      field('项目名称', name),
      field('目标', goal),
    ),
    el('label', { class: 'checkbox' }, allow, ' 允许 Agent 写入该目录（默认只读）'),
    el('div', { class: 'form-actions' }, submit),
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const dir = path.value.trim();
    if (!dir) { path.focus(); toast('请填写目录路径', 'info'); return; }
    withBusy(submit, async () => {
      try {
        const card = await api.attachProject({
          path: dir,
          name: name.value.trim(),
          goal: goal.value.trim(),
          allow_writes: allow.checked,
          confirm: dir,
        });
        toast(card.existing ? `目录已在项目「${card.name}」中` : `目录已接入项目「${card.name}」`, 'success');
        await loadState({ quiet: true });
        openApp('project', card.id);
      } catch (error) {
        toastError(error);
      }
    });
  });
  return form;
}

function renderNewProjectForm() {
  const name = input({ placeholder: '例如：命令行贪吃蛇', maxlength: '80', 'aria-label': '项目名称' });
  const goal = textarea({
    placeholder: '目标越具体，主理人拆得越准。例如：用 Python 写一个可玩的终端贪吃蛇，带计分和障碍物，附运行说明。',
    rows: '3',
    'aria-label': '项目目标',
  });
  const submit = el('button', { class: 'btn btn-primary', type: 'submit' }, icon('plus'), '创建项目');
  const form = el('form', { class: 'form-card' },
    el('h3', {}, '新建项目'),
    el('p', { class: 'form-hint' }, '创建后会立即拆解任务，并尝试为项目派出主理人。'),
    el('div', { class: 'form-grid' },
      field('项目名称', name, { hint: '留空时由后端按目标生成。' }),
      field('目标', goal, { full: true }),
    ),
    el('div', { class: 'form-actions' }, submit),
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const projectName = name.value.trim();
    const projectGoal = goal.value.trim();
    if (!projectName && !projectGoal) {
      name.focus();
      toast('至少填写项目名称或目标', 'info');
      return;
    }
    withBusy(submit, async () => {
      try {
        const card = await api.createProject(projectName || projectGoal.slice(0, 24), projectGoal || projectName);
        toast(`项目「${card.name}」已创建`, 'success');
        await loadState({ quiet: true });
        openApp('project', card.id);
      } catch (error) {
        toastError(error);
      }
    });
  });
  return form;
}

function renderHealthReport(project, report, healthSlot) {
  const summary = report.summary || {};
  const tone = report.ready ? 'ready' : 'failed';
  const rows = (report.checks || []).map((check) => {
    const pill = check.status === 'ok' ? 'ready'
      : check.status === 'fail' ? 'failed' : 'degraded';
    return el('div', { class: 'health-row' },
      statusPill(pill),
      el('span', { class: 'health-label' }, check.label || check.id),
      el('span', { class: 'health-message' }, check.message || ''),
      check.fix && check.status !== 'ok'
        ? el('div', { class: 'health-fix mono' }, `fix: ${check.fix}`)
        : null,
    );
  });
  const actions = [];
  if (!report.ready) {
    const onboardBtn = el('button', { class: 'btn btn-primary btn-sm', type: 'button' },
      icon('plus'), '补齐入住设施');
    onboardBtn.addEventListener('click', () => {
      withBusy(onboardBtn, async () => {
        try {
          await api.onboardProject(project.id);
          toast('已补齐骨架/种子任务/记忆，重新自检中…', 'success');
          const fresh = await api.getProjectHealth(project.id);
          healthSlot.replaceChildren(renderHealthReport(project, fresh, healthSlot));
          await Promise.all([loadState({ quiet: true }), loadTasks(project.id, { force: true })]);
        } catch (error) {
          toastError(error);
        }
      });
    });
    actions.push(onboardBtn);
  }
  const refresh = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' },
    icon('refresh'), '重新自检');
  refresh.addEventListener('click', () => {
    withBusy(refresh, async () => {
      try {
        const fresh = await api.getProjectHealth(project.id);
        healthSlot.replaceChildren(renderHealthReport(project, fresh, healthSlot));
      } catch (error) {
        toastError(error);
      }
    });
  });
  actions.push(refresh);
  return el('div', { class: 'form-card health-card' },
    el('div', { class: 'health-head' },
      el('h3', {}, '入住自检'),
      statusPill(tone),
      el('span', { class: 'mono health-summary' },
        `ok ${Number(summary.ok) || 0} · warn ${Number(summary.warn) || 0} · `
        + `fail ${Number(summary.fail) || 0} · 未检查 ${Number(summary.unchecked) || 0}`),
    ),
    el('p', { class: 'form-hint' },
      report.ready
        ? '门牌、目录、看板、记忆和权限检查通过，Agent 可以开工。'
        : '还有阻塞项：按 fix 补齐后重新自检；blocked 不是执行失败。'),
    ...(report.not_ready_reasons || []).map((reason) =>
      el('div', { class: 'health-blocker' }, `✕ ${reason}`)),
    el('div', { class: 'health-rows' }, ...rows),
    el('div', { class: 'form-actions' }, ...actions),
  );
}

function renderProjectDetail(project) {
  const status = project.degraded ? 'degraded' : project.status;
  setSheetHeader(project.name || '项目详情', `项目详情 · ${statusLabel(status)}`, 'projects');

  const tasks = view.tasksLoading ? [] : view.projectTasks;
  const counts = project.counts || {};
  const total = project.total_tasks ?? Object.values(counts).reduce((sum, n) => sum + (Number(n) || 0), 0);
  const agents = (project.agents || state.agents.filter((agent) => agent.project_id === project.id))
    .filter((agent) => !['dead', 'failed'].includes(agent.status));

  const wrap = el('div', {}, backButton());
  const healthSlot = el('div', { class: 'health-slot' });

  const heroRight = el('div', { class: 'dh-actions' },
    ring(project.progress || 0, { size: 76, stroke: 6 }),
    el('div', { class: 'dh-progress mono' },
      el('b', {}, `${Math.round(project.progress || 0)}`), el('span', {}, '% 完成')),
  );
  const actionRow = el('div', { class: 'form-actions', style: { 'justify-content': 'flex-end' } });

  const healthBtn = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' },
    icon('check'), '入住自检');
  healthBtn.addEventListener('click', () => {
    withBusy(healthBtn, async () => {
      try {
        const report = await api.getProjectHealth(project.id);
        healthSlot.replaceChildren(renderHealthReport(project, report, healthSlot));
        healthSlot.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      } catch (error) {
        toastError(error);
      }
    });
  });
  actionRow.append(healthBtn);

  if (!['done', 'archived'].includes(project.status)) {
    const completeBtn = el('button', { class: 'btn btn-ok btn-sm', type: 'button' }, icon('check'), '完成项目');
    completeBtn.addEventListener('click', () => {
      if (!window.confirm(`确认完成项目「${project.name}」？\n所有待审任务会被标记完成，项目 Agent 将停止。`)) return;
      withBusy(completeBtn, async () => {
        try {
          await api.completeProject(project.id);
          toast('项目已标记完成', 'success');
          await Promise.all([loadState({ quiet: true }), loadTasks(project.id, { force: true })]);
          if (current) renderSheet();
        } catch (error) {
          toastError(error);
        }
      });
    });
    actionRow.append(completeBtn);
  }
  if (project.status !== 'archived') {
    const archiveBtn = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, icon('trash'), '归档');
    archiveBtn.addEventListener('click', () => {
      if (!window.confirm(`归档项目「${project.name}」？\n归档会停止该项目下所有 Agent，之后仍可在列表中查看。`)) return;
      withBusy(archiveBtn, async () => {
        try {
          const result = await api.archiveProject(project.id);
          toast(`项目已归档，停止 ${Number(result.agents_stopped) || 0} 个 Agent`, 'success');
          await loadState({ quiet: true });
          openApp('projects');
        } catch (error) {
          toastError(error);
        }
      });
    });
    actionRow.append(archiveBtn);
  }
  if (project.external) {
    const writable = project.workspace_mode === 'attached-rw';
    const accessBtn = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' },
      writable ? '改为只读' : '允许 Agent 写入');
    accessBtn.addEventListener('click', () => {
      const message = writable
        ? `把「${project.name}」改为只读？Agent 将不能写文件或执行命令。`
        : `允许 Agent 写入外部目录「${project.name}」？\n目录中的文件可能被修改，写操作仍会经过能力审批。`;
      if (!window.confirm(message)) return;
      withBusy(accessBtn, async () => {
        try {
          await api.setProjectAccess(project.id, !writable, true);
          toast(writable ? '已改为只读' : '已允许写入', 'success');
          await loadState({ quiet: true });
          if (current) renderSheet();
        } catch (error) {
          toastError(error);
        }
      });
    });
    actionRow.append(accessBtn);
  }
  const deleteBtn = el('button', { class: 'btn btn-danger btn-sm', type: 'button' },
    icon('trash'), '删除项目');
    deleteBtn.addEventListener('click', () => {
      const typed = window.prompt(
        `永久删除项目「${project.name}」会删除项目记录、任务和整个工作区，不可恢复。\n`
        + '请输入项目名称确认：');
      if (typed === null) return;
      if (typed.trim() !== project.name) {
        toast('名称不匹配，已取消删除', 'error');
        return;
      }
      withBusy(deleteBtn, async () => {
        try {
          const result = await api.deleteProject(project.id, {
            purgeWorkspace: true, confirm: typed.trim(),
          });
          toast(`项目已删除，停止 ${Number(result.agents_stopped) || 0} 个 Agent`
            + (result.workspace_purged ? '，工作区已清空' : ''), 'success');
          await loadState({ quiet: true });
          openApp('projects');
        } catch (error) {
          toastError(error);
        }
      });
    });
  actionRow.append(deleteBtn);
  const refreshBtn = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, icon('refresh'), '刷新');
  refreshBtn.addEventListener('click', () => {
    withBusy(refreshBtn, async () => {
      try {
        await Promise.all([loadTasks(project.id, { force: true }), loadState({ quiet: true })]);
      } catch (error) {
        toastError(error);
      }
    });
  });
  actionRow.append(refreshBtn);
  heroRight.append(actionRow);

  wrap.append(el('div', { class: 'detail-hero', style: { '--project-color': project.color || '#7c5cff' } },
    el('div', {},
      el('div', { class: 'dh-top' }, statusPill(status), el('span', { class: 'ac-id mono' }, project.id)),
      el('h3', {}, project.name || '未命名项目'),
      el('p', { class: 'dh-goal' }, project.goal || project.brief || '没有填写目标。'),
      el('div', { class: 'dh-meta' },
        el('span', {}, '工作区 ', el('span', { class: 'mono' }, project.workspace || '—')),
        project.external
          ? el('span', {}, project.workspace_mode === 'attached-rw'
              ? '外部目录 · 可写' : '外部目录 · 只读')
          : null,
        el('span', {}, '更新于 ', el('span', { class: 'mono' }, ago(project.updated_at))),
        el('span', {}, '里程碑 ', el('span', { class: 'mono' },
          String(milestoneCount(project)))),
        ...(Array.isArray(project.tags) ? project.tags.map((tag) => el('span', { class: 'tag' }, tag)) : []),
      ),
    ),
    heroRight,
  ));

  wrap.append(el('div', { class: 'detail-stats' },
    detailStat('任务总数', total, ''),
    detailStat('进行中', counts.doing, 'is-doing'),
    detailStat('待审', counts.review, 'is-review'),
    detailStat('完成', counts.done, 'is-done'),
  ));
  wrap.append(healthSlot);

  /* agents on this project */
  const activeOnProject = agents.filter((agent) => ACTIVE_AGENT_STATUSES.has(agent.status)).length;
  wrap.append(sectionHead('本项目 Agent', {
    hint: agents.length ? `${agents.length} 个 Agent · ${activeOnProject} 在岗` : '暂无 Agent',
  }));
  if (agents.length) {
    wrap.append(el('div', { class: 'agent-strip' },
      ...agents.map((agent) => el('button', {
        class: 'agent-chip',
        type: 'button',
        title: `与 ${agent.display_name || agent.id} 对话`,
        onclick: () => openApp('agent', agent.id),
      },
      avatar(agent),
      el('div', { class: 'agent-chip-body' },
        el('span', { class: 'agent-chip-name' }, agent.display_name || agent.id),
        el('span', { class: 'agent-chip-role' },
          el('span', { class: `status-dot is-${agent.status}`, 'aria-hidden': 'true' }),
          roleLabel(agent.role),
        ),
      ),
      )),
    ));
  } else {
    wrap.append(appNote('还没有 Agent 在项目上工作。可以去「Agent」应用手动派工，或等待主理人自动派发。'));
  }

  /* task board */
  wrap.append(sectionHead('任务看板', {
    hint: view.tasksLoading ? '载入任务…' : `共 ${view.projectTasks.length} 个任务`,
    action: el('button', {
      class: 'text-btn',
      type: 'button',
      onclick: () => loadTasks(project.id, { force: true }).catch(toastError),
    }, icon('refresh', 'ico ico-sm'), '刷新'),
  }));

  if (view.tasksLoading) {
    wrap.append(loadingInline('载入任务…'));
  } else if (view.tasksError) {
    wrap.append(appNote(`任务载入失败：${view.tasksError}`, { warn: true }));
  } else if (!view.projectTasks.length) {
    wrap.append(emptyState({
      iconName: 'terminal',
      title: '还没有任务',
      text: '主理人会拆解目标；也可以手动在下面添加一个任务。',
    }));
  } else {
    const selected = view.projectTasks.find((task) => task.id === view.selectedTaskId);
    if (selected) wrap.append(renderTaskDetail(selected));
    wrap.append(renderTaskBoard());
  }

  if (!view.tasksLoading && !view.tasksError && view.projectTasks.length) {
    wrap.append(renderTaskDependencySection(view.projectTasks, {
      selectedId: view.selectedTaskId,
      onSelect: (taskId) => {
        view.selectedTaskId = taskId;
        view.scrollToTask = true;
        renderSheet();
      },
    }));
  }

  wrap.append(mountGitSection(project.id, { projectName: project.name }));

  wrap.append(renderAddTaskForm(project));
  return wrap;
}

function detailStat(label, value, tone) {
  return el('div', { class: 'detail-stat' },
    el('div', { class: 'ds-label' }, label),
    el('div', { class: `ds-value${tone ? ` ${tone}` : ''}` }, String(Number(value) || 0)),
  );
}

/** The backend card historically exposes `milestones` as a number; accept both. */
function milestoneCount(project) {
  if (Array.isArray(project.milestones)) return project.milestones.length;
  const value = Number(project.milestones);
  return Number.isFinite(value) ? value : 0;
}

function renderTaskBoard() {
  const board = el('div', { class: 'task-board' });
  for (const column of TASK_COLUMNS) {
    const tasks = view.projectTasks
      .filter((task) => column.key === 'todo'
        ? ['todo', 'blocked'].includes(task.status)
        : task.status === column.key)
      .sort((a, b) => (Number(b.priority) || 0) - (Number(a.priority) || 0));
    const col = el('div', { class: 'task-col', dataset: { col: column.key } },
      el('div', { class: 'task-col-head' },
        el('h4', {}, column.label),
        el('span', { class: 'count' }, String(tasks.length)),
      ),
    );
    if (!tasks.length) {
      col.append(el('div', { class: 'task-col-empty' }, '空'));
    } else {
      for (const task of tasks) col.append(renderTaskCard(task));
    }
    board.append(col);
  }
  return board;
}

function renderTaskCard(task) {
  const selected = task.id === view.selectedTaskId;
  const checkTotal = task.checks?.length || task.check_total || 0;
  const checkDone = task.checks?.filter((item) => item.done).length ?? task.check_done ?? 0;
  const depText = depBadgeText(task, view.projectTasks);
  const depBlocked = isDependencyBlocked(task, view.projectTasks);
  const titleBtn = el('button', {
    class: 'tc-title-btn',
    type: 'button',
    onclick: (event) => {
      event.stopPropagation();
      view.selectedTaskId = selected ? null : task.id;
      view.scrollToTask = !selected;
      renderSheet();
    },
  }, el('span', { class: 'tc-title' }, task.title || task.id));
  const card = el('div', {
    class: `task-card${selected ? ' is-selected' : ''}${task.status === 'blocked' ? ' is-blocked' : ''}`
      + `${depBlocked ? ' is-dep-blocked' : ''}${depText ? ' has-deps' : ''}`,
    dataset: { taskId: task.id },
  }, titleBtn);
  card.append(el('div', { class: 'tc-meta' },
    task.role ? roleChip(task.role) : null,
    Number(task.priority) > 0 ? el('span', { class: 'tc-id' }, `P${task.priority}`) : null,
    checkTotal ? el('span', { class: 'tc-checks' }, `✓ ${checkDone}/${checkTotal}`) : null,
    depText ? el('span', {
      class: `tc-deps${depBlocked ? ' is-open' : ''}`,
      title: taskDeps(task).join(', '),
    }, '⛓ ', depText) : null,
    el('span', { class: 'tc-id' }, task.id),
  ));
  const statusSelect = el('select', {
    class: 'task-status-select',
    'aria-label': `任务 ${task.title} 的状态`,
    disabled: task.status === 'done',
  });
  for (const next of [task.status, ...(TASK_TRANSITIONS[task.status] || [])]) {
    statusSelect.append(el('option', { value: next, selected: next === task.status }, TASK_LABELS[next] || next));
  }
  statusSelect.addEventListener('change', () => {
    changeTaskStatus(task, statusSelect.value, card);
  });
  card.append(statusSelect);
  return card;
}

function renderTaskDetail(task) {
  const panel = el('div', { class: 'task-detail' });
  const closeBtn = el('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-label': '收起任务详情',
    onclick: () => {
      view.selectedTaskId = null;
      renderSheet();
    },
  }, icon('close'));
  panel.append(el('div', { class: 'td-head' },
    el('div', {},
      el('div', { class: 'td-title' }, task.title || task.id),
      el('div', { class: 'td-sub' },
        statusPill(task.status),
        task.role ? roleChip(task.role) : null,
        Number(task.priority) > 0 ? el('span', { class: 'tag' }, `优先级 ${task.priority}`) : null,
        el('span', { class: 'ac-id mono' }, task.id),
        task.assignee ? el('span', { class: 'ac-id mono' }, `@${task.assignee}`) : null,
      ),
    ),
    closeBtn,
  ));
  if (task.description) panel.append(el('p', { class: 'td-desc' }, task.description));

  const statusOptions = [task.status, ...(TASK_TRANSITIONS[task.status] || [])];
  const statusSelect = select({ 'aria-label': '修改任务状态', disabled: task.status === 'done' },
    statusOptions.map((value) => ({ value, label: TASK_LABELS[value] || value })));
  statusSelect.addEventListener('change', () => changeTaskStatus(task, statusSelect.value));
  panel.append(el('div', { class: 'form-actions', style: { 'align-items': 'center' } },
    el('span', { class: 'field-hint' }, '状态：'), statusSelect,
    el('span', { class: 'field-hint', style: { 'margin-left': 'auto' } },
      `创建 ${fmtDateTime(task.created_at)} · 更新 ${fmtDateTime(task.updated_at)}`),
  ));

  const left = el('div', { class: 'td-block' }, el('h5', {}, '验收清单'));
  if (task.checks?.length) {
    for (const check of task.checks) left.append(renderCheckRow(task, check));
  } else {
    left.append(el('p', { class: 'field-hint' }, '没有验收清单。'));
  }

  const artifacts = el('div', { class: 'td-block', style: { 'margin-top': '14px' } }, el('h5', {}, '产物'));
  if (task.artifacts?.length) {
    for (const artifact of task.artifacts) {
      artifacts.append(el('div', { class: 'artifact-row' },
        el('span', { class: 'art-type' }, artifact.type || 'file'),
        el('div', { class: 'art-main' },
          el('span', { class: 'art-title' }, artifact.title || artifact.path || '未命名产物'),
          el('span', { class: 'art-path', title: artifact.path || '' }, artifact.path || ''),
        ),
      ));
    }
  } else {
    artifacts.append(el('p', { class: 'field-hint' }, '暂无产物。'));
  }

  const notes = el('div', { class: 'td-block' }, el('h5', {}, '备注'));
  if (task.notes?.length) {
    for (const note of task.notes) {
      notes.append(el('div', { class: 'note-row' },
        el('div', { class: 'note-head' },
          el('span', { class: 'mono' }, note.author || '—'),
          el('span', { class: 'mono' }, fmtDateTime(note.ts)),
        ),
        el('p', {}, note.text || ''),
      ));
    }
  } else {
    notes.append(el('p', { class: 'field-hint' }, '还没有备注。'));
  }
  const noteInput = input({ placeholder: '给任务留一条备注…', 'aria-label': '备注内容' });
  const noteBtn = el('button', { class: 'btn btn-sm', type: 'submit' }, '添加');
  const noteForm = el('form', { class: 'note-form' }, noteInput, noteBtn);
  noteForm.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = noteInput.value.trim();
    if (!text) return;
    withBusy(noteBtn, async () => {
      try {
        const updated = await api.addTaskNote(task.id, text);
        updateTaskLocal(updated);
        toast('备注已添加', 'success');
        await loadState({ quiet: true });
        renderSheet();
      } catch (error) {
        toastError(error);
      }
    });
  });
  notes.append(noteForm);

  panel.append(el('div', { class: 'td-columns' }, left, el('div', {}, artifacts, notes)));
  return panel;
}

function renderCheckRow(task, check) {
  const checkbox = el('input', { type: 'checkbox', checked: Boolean(check.done),
    'aria-label': `验收项：${check.text || check.id}` });
  const row = el('label', { class: `check-row${check.done ? ' is-done' : ''}` },
    checkbox,
    el('span', { class: 'check-text' },
      check.text || check.id,
      check.note ? el('span', { class: 'check-note' }, ` · ${check.note}`) : null,
    ),
  );
  checkbox.addEventListener('change', () => {
    checkbox.disabled = true;
    api.checkTask(task.id, { item_id: check.id, done: checkbox.checked })
      .then(async (updated) => {
        updateTaskLocal(updated);
        await loadState({ quiet: true });
        renderSheet();
      })
      .catch((error) => {
        checkbox.checked = !checkbox.checked;
        checkbox.disabled = false;
        toastError(error);
      });
  });
  return row;
}

function changeTaskStatus(task, status, scope) {
  if (!status || status === task.status) return;
  const controls = scope ? scope.querySelectorAll('select, button') : [];
  controls.forEach((node) => { node.disabled = true; });
  api.patchTask(task.id, { status })
    .then(async (updated) => {
      updateTaskLocal(updated);
      toast(`任务已移至「${TASK_LABELS[status] || status}」`, 'success');
      await loadState({ quiet: true });
      renderSheet();
    })
    .catch((error) => {
      toastError(error);
      controls.forEach((node) => { node.disabled = false; });
      renderSheet();
    });
}

function updateTaskLocal(task) {
  if (!task || !task.id) return;
  const index = view.projectTasks.findIndex((item) => item.id === task.id);
  if (index >= 0) view.projectTasks[index] = { ...view.projectTasks[index], ...task };
  else view.projectTasks.push(task);
}

function renderAddTaskForm(project) {
  const title = input({ placeholder: '任务标题', maxlength: '120', 'aria-label': '任务标题' });
  const description = textarea({ placeholder: '任务说明（可选）', rows: '2', 'aria-label': '任务说明' });
  const role = select({ 'aria-label': '任务角色' }, [{ value: '', label: '未指定角色' }, ...ROLE_OPTIONS]);
  const priority = select({ 'aria-label': '优先级' }, [
    { value: '0', label: '普通' },
    { value: '1', label: '偏高' },
    { value: '2', label: '高' },
    { value: '3', label: '紧急' },
  ]);
  const submit = el('button', { class: 'btn btn-sm btn-primary', type: 'submit' }, icon('plus'), '添加任务');
  const form = el('form', { class: 'form-card', style: { 'margin-top': '16px' } },
    el('h3', {}, '添加任务'),
    el('p', { class: 'form-hint' }, '手动插入一个任务；主理人和工人会按优先级领取。'),
    el('div', { class: 'form-grid' },
      field('标题', title, { full: true }),
      field('说明', description, { full: true }),
      field('角色', role),
      field('优先级', priority),
    ),
    el('div', { class: 'form-actions' }, submit),
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const taskTitle = title.value.trim();
    if (!taskTitle) {
      title.focus();
      return;
    }
    withBusy(submit, async () => {
      try {
        await api.createTask(project.id, {
          title: taskTitle,
          description: description.value.trim(),
          role: role.value || null,
          priority: Number(priority.value) || 0,
        });
        toast('任务已添加', 'success');
        await Promise.all([loadTasks(project.id, { force: true }), loadState({ quiet: true })]);
      } catch (error) {
        toastError(error);
      }
    });
  });
  return form;
}

async function loadTasks(projectId, { force = false } = {}) {
  if (!projectId) return;
  const token = ++view.tasksToken;
  view.tasksLoading = true;
  view.tasksError = '';
  if (current?.name === 'projects' && current.projectId === projectId) renderSheet();
  try {
    const data = await api.getTasks(projectId);
    if (token !== view.tasksToken) return;
    view.projectTasks = Array.isArray(data.tasks) ? data.tasks : [];
    if (view.selectedTaskId && !view.projectTasks.some((task) => task.id === view.selectedTaskId)) {
      view.selectedTaskId = null;
    }
  } catch (error) {
    if (token !== view.tasksToken) return;
    view.tasksError = error.message || String(error);
    toastError(error);
  } finally {
    if (token === view.tasksToken) {
      view.tasksLoading = false;
      if (current?.name === 'projects' && current.projectId === projectId) renderSheet();
    }
  }
}

/* ================================================================== */
/* agents                                                              */
/* ================================================================== */

function renderAgentsApp() {
  const wrap = el('div', {});
  const agents = state.agents;
  const activeCount = agents.filter((agent) => ['working', 'waiting', 'idle', 'spawning'].includes(agent.status)).length;

  wrap.append(el('div', { class: 'app-toolbar' },
    el('div', { class: 'toolbar-left' },
      el('span', { class: 'app-count' }, `${agents.length} 个 Agent · ${activeCount} 在岗`),
    ),
    el('div', { class: 'toolbar-right' },
      refreshButton(() => loadState({ quiet: true })),
    ),
  ));

  wrap.append(appNote(
    '点击任一 Agent 进入对话：它的角色、项目工作区和工具都在；'
    + '高风险动作仍会进入审批。对话记录按 Agent 持久保存。',
    { iconName: 'chat' }));
  wrap.append(renderSpawnForm());

  const filters = [
    { key: 'active', label: '在岗' },
    { key: 'all', label: '全部' },
    { key: 'ended', label: '已结束' },
  ];
  wrap.append(el('div', { class: 'filter-chips', style: { 'margin-top': '16px', 'margin-bottom': '12px' } },
    ...filters.map((filter) => el('button', {
      class: `chip${view.agentFilter === filter.key ? ' is-active' : ''}`,
      type: 'button',
      onclick: () => {
        view.agentFilter = filter.key;
        renderSheet();
      },
    }, filter.label)),
  ));

  const filtered = agents.filter((agent) => {
    if (view.agentFilter === 'active') return ['working', 'waiting', 'idle', 'spawning'].includes(agent.status);
    if (view.agentFilter === 'ended') return ['done', 'blocked', 'failed', 'dead'].includes(agent.status);
    return true;
  });

  if (!state.loaded) {
    wrap.append(loadingInline('载入 Agent…'));
  } else if (!agents.length) {
    wrap.append(emptyState({
      iconName: 'agents',
      title: '还没有 Agent',
      text: '用上面的表单派一个；或者创建一个项目，让主理人自己招人。',
    }));
  } else if (!filtered.length) {
    wrap.append(emptyState({
      iconName: 'agents',
      title: '没有符合条件的 Agent',
      text: '换个筛选条件看看；已结束的 Agent 也能点进对话。',
      actions: view.agentFilter !== 'all' ? [el('button', {
        class: 'suggestion-chip',
        type: 'button',
        onclick: () => { view.agentFilter = 'all'; renderSheet(); },
      }, '查看全部 Agent')] : [],
    }));
  } else {
    const groups = new Map();
    for (const agent of filtered) {
      const key = agent.project_id || '__none__';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(agent);
    }
    const order = { working: 0, waiting: 1, idle: 2, spawning: 3, queued: 4, done: 5, blocked: 6, failed: 7, dead: 8 };
    for (const [, list] of groups) list.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
    const keys = [...groups.keys()].sort((a, b) => {
      if (a === '__none__') return 1;
      if (b === '__none__') return -1;
      return String(a).localeCompare(String(b));
    });
    for (const key of keys) {
      const project = state.projects.find((item) => item.id === key);
      const list = groups.get(key);
      wrap.append(el('div', { class: 'roster-group' },
        el('div', { class: 'roster-head' },
          el('h4', {}, project ? project.name : '未绑定项目'),
          el('span', { class: 'mono' }, project ? project.id : '—'),
          project ? statusPill(project.degraded ? 'degraded' : project.status) : null,
          el('span', { class: 'mono', style: { 'margin-left': 'auto' } }, `${list.length} 个`),
        ),
        ...list.map(agentRow),
      ));
    }
  }
  return wrap;
}

function agentRow(agent) {
  const terminal = ['done', 'blocked', 'failed', 'dead'].includes(agent.status);
  const open = () => openApp('agent', agent.id);
  const killBtn = terminal ? null : el('button', {
    class: 'btn btn-xs btn-danger',
    type: 'button',
    'aria-label': `终止 Agent ${agent.display_name || agent.id}`,
    onclick: () => {
      if (!window.confirm(`终止 ${agent.display_name || agent.id}？\n它的工作进程会被要求退出。`)) return;
      withBusy(killBtn, async () => {
        try {
          await api.killAgent(agent.id);
          toast('终止信号已发送', 'success');
          await loadState({ quiet: true });
          renderSheet();
        } catch (error) {
          toastError(error);
        }
      });
    },
  }, icon('close'), '终止');

  const main = el('button', {
    class: 'agent-row-main',
    type: 'button',
    title: `与 ${agent.display_name || agent.id} 对话`,
    onclick: open,
  },
  avatar(agent, { large: true }),
  el('div', { class: 'ar-main' },
    el('div', { class: 'ar-name-line' },
      el('span', { class: 'ar-name' }, agent.display_name || agent.id),
      roleChip(agent.role),
      statusPill(agent.status),
      el('span', { class: 'ar-id' }, agent.id),
    ),
    el('div', { class: 'ar-sub' },
      agent.model ? el('span', { class: 'mono' }, agent.model) : null,
      agent.task_id ? el('span', { class: 'mono' }, `task ${agent.task_id}`) : null,
      agent.error_code ? el('span', { class: 'mono' }, agent.error_code) : null,
      el('span', {}, `心跳 ${ago(agent.last_seen)}`),
    ),
    agent.summary ? el('div', { class: 'ar-summary', title: agent.summary }, agent.summary) : null,
    agent.error ? el('div', { class: 'ar-summary', style: { color: 'var(--danger)' } }, agent.error) : null,
  ),
  icon('chat', 'ico ico-sm ar-chat-ico'),
  );
  return el('div', { class: 'agent-row' },
    main,
    el('div', { class: 'ar-side' },
      el('span', { class: 'ar-time' }, agoShort(agent.created_at)),
      killBtn,
    ),
  );
}

function renderSpawnForm() {
  const role = select({ 'aria-label': 'Agent 角色' }, ROLE_OPTIONS);
  const project = select({ 'aria-label': '所属项目' }, [
    { value: '', label: '不绑定项目' },
    ...state.projects
      .filter((item) => !['archived'].includes(item.status))
      .map((item) => ({ value: item.id, label: `${item.name} (${item.id})` })),
  ]);
  const submit = el('button', { class: 'btn btn-sm btn-primary', type: 'submit' }, icon('plus'), '派工');
  const form = el('form', { class: 'form-card' },
    el('h3', {}, '手动派工'),
    el('p', { class: 'form-hint' }, '每个 Agent 是一个独立进程；任务结束后会自行退出。'),
    el('div', { class: 'form-grid' },
      field('角色', role),
      field('项目', project),
    ),
    el('div', { class: 'form-actions' }, submit),
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    withBusy(submit, async () => {
      try {
        const agent = await api.spawnAgent(role.value, project.value || null);
        toast(`已派工：${agent.display_name || agent.id}`, 'success');
        await loadState({ quiet: true });
        renderSheet();
      } catch (error) {
        toastError(error);
      }
    });
  });
  return form;
}

/* ================================================================== */
/* units                                                               */
/* ================================================================== */

function renderUnitsApp() {
  return units.renderUnitsApp();
}

/* ================================================================== */
/* approvals                                                           */
/* ================================================================== */

function renderApprovalsApp() {
  const wrap = el('div', {});
  const pending = [...state.approvals].sort((a, b) => (a.expires_at || 0) - (b.expires_at || 0));
  setSheetHeader('审批', pending.length ? `${pending.length} 件待审批 · 高风险操作需要你放行` : '没有待审批的操作', 'approvals');

  wrap.append(el('div', { class: 'app-toolbar' },
    el('div', { class: 'toolbar-left' },
      el('span', { class: 'app-count' }, `待审批 ${pending.length}`),
    ),
    el('div', { class: 'toolbar-right' },
      refreshButton(async () => {
        await Promise.all([loadState({ quiet: true }), loadRules(), loadDecisions()]);
      }),
    ),
  ));

  if (!state.loaded) {
    wrap.append(loadingInline('载入审批…'));
  } else if (!pending.length) {
    wrap.append(emptyState({
      iconName: 'approvals',
      title: '没有待审批的操作',
      text: '低风险动作会按规则自动放行；中高风险动作会在这里等你放行或拒绝。',
    }));
  } else {
    wrap.append(el('div', { class: 'approval-list' }, ...pending.map(renderApprovalCard)));
  }

  wrap.append(sectionHead('自动放行规则', {
    hint: `风险上限 low / medium / high · 共 ${view.rules.length} 条`,
  }));
  if (view.rulesLoading && !view.rules.length) {
    wrap.append(loadingInline('载入规则…'));
  } else if (!view.rules.length) {
    wrap.append(appNote('暂无自定义规则。低风险动作按系统默认策略处理。', { iconName: 'settings' }));
  } else {
    wrap.append(el('div', { class: 'rules-list' }, ...view.rules.map(ruleRow)));
  }
  wrap.append(renderRuleForm());

  wrap.append(sectionHead('最近决定', {
    hint: '来自审计事件流',
    action: el('button', {
      class: 'text-btn',
      type: 'button',
      onclick: () => loadDecisions().catch(toastError),
    }, icon('refresh', 'ico ico-sm'), '刷新'),
  }));
  const decisions = view.recentDecisions.filter(isDecisionEvent);
  if (view.decisionsLoading && !decisions.length) {
    wrap.append(loadingInline('载入决定记录…'));
  } else if (!decisions.length) {
    wrap.append(appNote('还没有审批决定记录。', { iconName: 'events' }));
  } else {
    const list = el('div', { class: 'decision-list' });
    for (const event of decisions.slice(0, 20)) {
      const data = event.data || {};
      const decision = data.decision
        || (data.status === 'expired' ? 'expired' : data.status === 'canceled' ? 'canceled' : '');
      const label = {
        allow: '允许', deny: '拒绝', expired: '已过期', canceled: '已取消',
      }[decision] || '已决定';
      const main = data.request_id
        ? `${data.request_id} · ${label}` +
          `${data.mode ? ` · ${data.mode}` : ''}${data.by ? ` · ${data.by}` : ''}`
        : eventSummary(event);
      list.append(el('div', { class: 'decision-row' },
        el('span', { class: 'dec-type' }, event.type),
        el('span', { class: 'dec-main', title: main }, main),
        el('span', { class: 'dec-time' }, fmtTime(event.ts)),
      ));
    }
    wrap.append(list);
  }
  return wrap;
}

function openApprovalPreview(approval) {
  const body = el('pre', { class: 'preview-full' }, '加载中…');
  const onKey = (event) => {
    if (event.key === 'Escape') close();
  };
  const close = () => {
    document.removeEventListener('keydown', onKey);
    overlay.remove();
  };
  const overlay = el('div', {
    class: 'preview-overlay',
    role: 'dialog',
    'aria-modal': 'true',
    'aria-label': `审批 ${approval.id} 的完整预览`,
  });
  const dialog = el('div', { class: 'preview-dialog' },
    el('div', { class: 'preview-head' },
      el('div', { class: 'preview-title' },
        el('span', { class: 'mono' }, approval.id),
        el('span', { class: 'preview-sub' },
          `完整内容 ${approval.preview_bytes || 0} 字节`
          + (approval.preview_sha256 ? ` · sha256 ${approval.preview_sha256.slice(0, 16)}…` : '')),
      ),
      el('button', { class: 'btn btn-sm', type: 'button', onclick: close }, '关闭'),
    ),
    body,
  );
  overlay.append(dialog);
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener('keydown', onKey);
  document.body.append(overlay);
  // Full content is rendered with textContent only, never as markup.
  api.getApproval(approval.id)
    .then((data) => {
      body.textContent = data.preview_full || approval.preview || '（没有预览内容）';
    })
    .catch((error) => {
      body.textContent = `无法加载完整内容：${error.message || error}`;
    });
}

function renderApprovalCard(approval) {
  const ttl = el('span', {
    class: 'ac-ttl',
    dataset: { expires: String(approval.expires_at || '') },
    title: `到期 ${fmtDateTime(approval.expires_at)}`,
  }, icon('clock', 'ico ico-xs'), el('span', { class: 'ttl-text' }, '—'));

  const card = el('article', {
    class: 'approval-card',
    dataset: { risk: approval.risk || 'medium', approvalId: approval.id },
  });

  const scopeEntries = Object.entries(approval.scope || {});
  const scope = scopeEntries.length
    ? el('div', { class: 'scope-grid' },
      ...scopeEntries.flatMap(([key, value]) => [
        el('span', { class: 'scope-key' }, key),
        el('span', { class: 'scope-val' }, formatScopeValue(value)),
      ]))
    : null;

  const project = state.projects.find((item) => item.id === approval.project_id);
  const actions = el('div', { class: 'ac-actions' });
  const decide = (decision, mode, label) => {
    const buttons = [...card.querySelectorAll('button')];
    buttons.forEach((button) => { button.disabled = true; });
    api.decideApproval(approval.id, decision, mode)
      .then(async () => {
        toast(decision === 'allow' ? `已允许 ${approval.capability}` : `已拒绝 ${approval.capability}`,
          decision === 'allow' ? 'success' : 'info');
        await Promise.all([loadState({ quiet: true }), loadDecisions(), loadRules()]);
        renderSheet();
      })
      .catch((error) => {
        toastError(error);
        if (card.isConnected) buttons.forEach((button) => { button.disabled = false; });
      });
  };

  if (!canWrite()) {
    actions.append(el('p', { class: 'field-hint' }, t('approvals.viewer_hint')));
  } else {
    actions.append(
      el('button', { class: 'btn btn-sm btn-ok', type: 'button', title: t('approvals.allow_once.tip'), onclick: () => decide('allow', 'once', t('approvals.allow_once')) }, t('approvals.allow_once')),
      el('button', { class: 'btn btn-sm', type: 'button', title: t('approvals.allow_hour.tip'), onclick: () => decide('allow', 'session', t('approvals.allow_hour')) }, t('approvals.allow_hour')),
      el('button', { class: 'btn btn-sm btn-primary', type: 'button', title: t('approvals.allow_always.tip'), onclick: () => decide('allow', 'always', t('approvals.allow_always')) }, t('approvals.allow_always')),
      el('span', { class: 'spacer' }),
      el('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => decide('deny', 'once', t('approvals.deny')) }, t('approvals.deny')),
    );
  }

  card.append(
    el('div', { class: 'ac-head' },
      el('div', {},
        el('div', { class: 'ac-title-line' },
          riskPill(approval.risk),
          el('span', { class: 'ac-capability' }, approval.capability || 'unknown'),
          el('span', { class: 'ac-id' }, approval.id),
        ),
        el('div', { class: 'ac-sub' },
          el('span', {}, '发起：', el('span', { class: 'mono' }, approval.principal || '—')),
          approval.project_id
            ? el('span', {}, '项目：', el('span', { class: 'mono' }, project?.name || approval.project_id))
            : null,
          approval.task_id
            ? el('span', {}, '任务：', el('span', { class: 'mono' }, approval.task_id))
            : null,
          el('span', {}, '请求于 ', el('span', { class: 'mono' }, ago(approval.created_at))),
        ),
      ),
      ttl,
    ),
    approval.reason ? el('p', { class: 'ac-reason' }, approval.reason) : null,
    scope,
    approval.preview
      ? el('div', { class: 'ac-preview' },
        el('div', { class: 'ac-preview-bar' },
          el('span', { class: 'ac-preview-badge', dataset: { truncated: String(Boolean(approval.preview_truncated)) } },
            approval.preview_truncated ? `已截断 · ${approval.preview_bytes || 0} 字节` : '完整预览'),
          el('button', {
            class: 'btn btn-xs',
            type: 'button',
            onclick: () => openApprovalPreview(approval),
          }, '查看完整内容'),
        ),
        el('pre', { class: 'approval-preview' }, approval.preview),
      )
      : null,
    actions,
  );
  return card;
}

function isDecisionEvent(event) {
  const data = event?.data || {};
  if (data.decision) return true;
  return ['approved', 'denied', 'expired', 'canceled'].includes(data.status);
}

function formatScopeValue(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  const text = String(value);
  return text.length > 220 ? `${text.slice(0, 220)}…` : text;
}

function ruleRow(rule) {
  const scopeText = Object.entries(rule.scope || {})
    .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
    .join(' · ') || 'scope 不限';
  const expires = rule.expires_at ? `至 ${fmtDateTime(rule.expires_at)}` : '长期';
  const uses = rule.uses_left === null || rule.uses_left === undefined ? '次数不限' : `剩余 ${rule.uses_left} 次`;
  const deleteBtn = el('button', {
    class: 'icon-btn',
    type: 'button',
    'aria-label': `删除规则 ${rule.capability}`,
    title: '删除规则',
    onclick: () => {
      if (!window.confirm(`删除规则 ${rule.capability}（${rule.effect}）？`)) return;
      withBusy(deleteBtn, async () => {
        try {
          await api.deleteRule(rule.id);
          toast('规则已删除', 'success');
          await Promise.all([loadRules(), loadState({ quiet: true })]);
          renderSheet();
        } catch (error) {
          toastError(error);
        }
      });
    },
  }, icon('trash', 'ico ico-sm'));

  return el('div', { class: 'rule-row' },
    el('span', { class: 'effect-pill', dataset: { effect: rule.effect || 'allow' } }, String(rule.effect || 'allow').toUpperCase()),
    el('div', { class: 'rr-main' },
      el('div', { class: 'rr-line' },
        el('span', { class: 'rr-cap' }, rule.capability || '*'),
        el('span', { class: 'muted' }, '→', rule.principal || '*'),
        el('span', { class: 'tag' }, `风险 ≤ ${rule.risk_max || 'high'}`),
        rule.source === 'grant' ? el('span', { class: 'tag' }, 'GRANT') : null,
      ),
      el('div', { class: 'rr-scope', title: scopeText }, `${scopeText} · ${expires} · ${uses}`),
    ),
    el('div', { class: 'rr-side' }, deleteBtn),
  );
}

const RULE_CAPABILITIES = [
  'fs.read', 'fs.list', 'fs.write', 'fs.delete', 'shell.exec', 'net.fetch',
  'llm.chat', 'project.create', 'project.update', 'project.archive',
  'project.delete', 'task.manage', 'agent.spawn', 'agent.kill',
  'unit.control', 'sys.config', 'data.read', 'data.write',
];

// The form is built once and re-attached on every render, so a half-typed
// rule survives the live refreshes of the sheet.
function renderRuleForm() {
  if (!view.ruleForm) view.ruleForm = buildRuleForm();
  return view.ruleForm;
}

function buildRuleForm() {
  const errorNode = el('div', { class: 'form-error', role: 'alert' });
  const effect = select({ 'aria-label': '规则效果' }, [
    { value: 'allow', label: '允许 allow' },
    { value: 'deny', label: '拒绝 deny' },
  ]);
  const capability = select({ 'aria-label': '能力', class: 'select mono' }, [
    { value: '*', label: '* （全部能力）' },
    ...RULE_CAPABILITIES,
  ]);
  const principal = input({ value: '*', placeholder: '* 或 agent-coder-*', class: 'input mono', 'aria-label': '主体' });
  const riskMax = select({ 'aria-label': '风险上限' }, [
    { value: 'high', label: 'high（含高风险）' },
    { value: 'medium', label: 'medium' },
    { value: 'low', label: 'low' },
  ]);
  const scopeJson = textarea({
    placeholder: '{ "project_id": "p-1" }（可选）',
    class: 'textarea mono',
    rows: '2',
    'aria-label': 'scope JSON',
  });
  const pathPrefix = input({ placeholder: '例如 /repo/.stackflowos/projects/p-1', class: 'input mono', 'aria-label': '路径前缀' });
  const commandRegex = input({ placeholder: '例如 ^pytest\\b', class: 'input mono', 'aria-label': '命令正则' });
  const note = input({ placeholder: '这条规则为什么存在（可选）', 'aria-label': '备注' });
  const submit = el('button', { class: 'btn btn-sm btn-primary', type: 'submit' }, icon('plus'), '添加规则');

  const form = el('form', { class: 'form-card', style: { 'margin-top': '12px' } },
    el('h3', {}, '新增规则'),
    el('p', { class: 'form-hint' }, '规则只影响之后的操作；deny 优先于 allow。主体示例：agent-coder-*（所有 coder）、*（所有人）；风险上限表示该规则最多放行到哪一级风险。'),
    el('div', { class: 'form-grid is-3' },
      field('效果', effect),
      field('能力 capability', capability),
      field('主体 principal', principal),
      field('风险上限', riskMax),
      field('路径前缀', pathPrefix, { hint: '写入 scope.path_prefix' }),
      field('命令正则', commandRegex, { hint: '写入 scope.command_regex' }),
      field('scope JSON', scopeJson, { full: true, hint: '与上面的路径/命令字段合并；路径/命令字段优先。' }),
      field('备注', note, { full: true }),
    ),
    errorNode,
    el('div', { class: 'form-actions' }, submit),
  );

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    errorNode.textContent = '';
    const capabilityValue = capability.value.trim();
    let scope = {};
    if (scopeJson.value.trim()) {
      try {
        const parsed = JSON.parse(scopeJson.value);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('scope 必须是 JSON 对象');
        }
        scope = parsed;
      } catch (error) {
        errorNode.textContent = `scope JSON 无效：${error.message}`;
        scopeJson.focus();
        return;
      }
    }
    if (pathPrefix.value.trim()) scope.path_prefix = pathPrefix.value.trim();
    if (commandRegex.value.trim()) scope.command_regex = commandRegex.value.trim();

    withBusy(submit, async () => {
      try {
        await api.createRule({
          effect: effect.value,
          capability: capabilityValue,
          principal: principal.value.trim() || '*',
          scope,
          risk_max: riskMax.value,
          note: note.value.trim(),
        });
        toast('规则已添加', 'success');
        form.reset();
        await Promise.all([loadRules(), loadState({ quiet: true })]);
        renderSheet();
      } catch (error) {
        errorNode.textContent = error.message || String(error);
        toastError(error);
      }
    });
  });
  return form;
}

let rulesToken = 0;
async function loadRules() {
  const token = ++rulesToken;
  view.rulesLoading = true;
  if (current?.name === 'approvals' && !view.rules.length) renderSheet();
  try {
    const data = await api.getRules();
    if (token !== rulesToken) return;
    view.rules = Array.isArray(data.rules) ? data.rules : [];
  } catch (error) {
    if (token === rulesToken) toastError(error);
  } finally {
    if (token === rulesToken) {
      view.rulesLoading = false;
      if (current?.name === 'approvals') renderSheet();
    }
  }
}

let decisionsToken = 0;
async function loadDecisions() {
  const token = ++decisionsToken;
  view.decisionsLoading = true;
  if (current?.name === 'approvals' && !view.recentDecisions.length) renderSheet();
  try {
    const data = await api.getConsole({ types: ['approval'], limit: 30 });
    if (token !== decisionsToken) return;
    view.recentDecisions = [...(data.events || [])].reverse();
  } catch (error) {
    if (token === decisionsToken) toastError(error);
  } finally {
    if (token === decisionsToken) {
      view.decisionsLoading = false;
      if (current?.name === 'approvals') renderSheet();
    }
  }
}

function scheduleDecisionsReload() {
  window.clearTimeout(scheduleDecisionsReload.timer);
  scheduleDecisionsReload.timer = window.setTimeout(() => {
    if (current?.name === 'approvals') loadDecisions().catch(() => {});
  }, 500);
}

/* ================================================================== */
/* events console                                                      */
/* ================================================================== */

function renderEventsApp() {
  const wrap = el('div', {});
  const chips = el('div', { class: 'filter-chips' },
    ...EVENT_FILTERS.map((key) => el('button', {
      class: `chip${view.eventsType === key ? ' is-active' : ''}`,
      type: 'button',
      onclick: () => {
        view.eventsType = key;
        loadConsole();
        renderSheet();
      },
    }, key === 'all' ? '全部' : key)),
  );

  const projectSelect = select({
    'aria-label': '按项目过滤事件',
    onchange: (event) => {
      view.eventsProject = event.target.value;
      loadConsole();
      renderSheet();
    },
  }, [
    { value: '', label: '全部项目' },
    ...state.projects.map((project) => ({ value: project.id, label: project.name })),
  ]);
  projectSelect.value = view.eventsProject;

  wrap.append(el('div', { class: 'app-toolbar', style: { 'align-items': 'flex-start' } },
    el('div', { class: 'toolbar-left', style: { 'flex-direction': 'column', 'align-items': 'flex-start' } }, chips),
    el('div', { class: 'toolbar-right' },
      projectSelect,
      refreshButton(() => loadConsole()),
    ),
  ));

  wrap.append(el('div', { class: 'app-toolbar' },
    el('span', { class: 'app-count' }, `显示 ${view.consoleEvents.length} 条事件`),
    view.consoleLoading ? el('span', { class: 'app-count' }, '刷新中…') : null,
  ));

  if (view.consoleError) {
    wrap.append(appNote(`事件载入失败：${view.consoleError}`, { warn: true }));
  }
  if (view.consoleLoading && !view.consoleEvents.length) {
    wrap.append(loadingInline('载入事件…'));
    return wrap;
  }
  if (!view.consoleEvents.length) {
    wrap.append(emptyState({
      iconName: 'events',
      title: '没有匹配的事件',
      text: '换个类型或项目筛选条件看看。',
    }));
    return wrap;
  }

  const consoleList = el('div', { class: 'event-console' });
  for (const event of view.consoleEvents) {
    const details = el('details', {},
      el('summary', {}, 'JSON'),
      el('pre', { class: 'ev-json' }, JSON.stringify(event.data || {}, null, 2)),
    );
    const project = state.projects.find((item) => item.id === event.project_id);
    consoleList.append(el('div', { class: 'ev-row' },
      el('span', { class: 'ev-time', title: fmtDateTime(event.ts) }, fmtTime(event.ts)),
      el('span', { class: `ev-type ${toneForType(event.type)}` }, event.type),
      el('span', { class: 'ev-source', title: event.source || '' },
        `${event.source || 'sys'}${project ? ` · ${project.id}` : event.project_id ? ` · ${event.project_id}` : ''}`),
      el('div', { class: 'ev-main' },
        el('div', { class: 'ev-summary' }, eventSummary(event)),
        details,
      ),
    ));
  }
  wrap.append(consoleList);
  return wrap;
}

async function loadConsole() {
  const token = ++view.consoleToken;
  view.consoleLoading = true;
  view.consoleError = '';
  try {
    const data = await api.getConsole({
      types: view.eventsType === 'all' ? [] : [view.eventsType],
      projectId: view.eventsProject,
      limit: 250,
    });
    if (token !== view.consoleToken) return;
    view.consoleEvents = [...(data.events || [])].reverse();
  } catch (error) {
    if (token !== view.consoleToken) return;
    view.consoleError = error.message || String(error);
    toastError(error);
  } finally {
    if (token === view.consoleToken) {
      view.consoleLoading = false;
      if (current?.name === 'events') renderSheet();
    }
  }
}

function scheduleConsoleReload() {
  window.clearTimeout(consoleTimer);
  consoleTimer = window.setTimeout(() => {
    if (current?.name === 'events') loadConsole().catch(() => {});
  }, 600);
}

/* ================================================================== */
/* settings                                                            */
/* ================================================================== */

function renderSettingsApp() {
  const wrap = el('div', {});
  if (!state.config) return loadingInline('载入配置…');
  const config = state.config;

  if (view.settingsNotice?.length) {
    const fields = view.settingsNotice
      .map((fieldName) => RESTART_LABELS[fieldName] || fieldName)
      .join('、');
    wrap.append(appNote(`已保存。以下项需要重启 StackFlowOS 才会生效：${fields}。`, { warn: true }));
  }

  const tabs = [
    { key: 'general', label: t('settings.tab.general') },
    { key: 'network', label: t('settings.tab.network') },
    { key: 'ai', label: t('settings.tab.ai') },
    { key: 'capabilities', label: t('settings.tab.capabilities') },
    { key: 'security', label: t('settings.tab.security') },
    { key: 'storage', label: t('settings.tab.storage') },
    { key: 'about', label: t('settings.tab.about') },
  ];
  const tabBar = el('div', { class: 'settings-tabs', role: 'tablist', 'aria-label': '设置分类' });
  const panes = {};
  for (const tab of tabs) {
    const button = el('button', {
      class: `settings-tab${view.settingsTab === tab.key ? ' is-active' : ''}`,
      type: 'button',
      role: 'tab',
      'aria-selected': String(view.settingsTab === tab.key),
      dataset: { settingsTab: tab.key },
      onclick: () => {
        view.settingsTab = tab.key;
        tabBar.querySelectorAll('.settings-tab').forEach((node) => {
          const active = node.dataset.settingsTab === tab.key;
          node.classList.toggle('is-active', active);
          node.setAttribute('aria-selected', String(active));
        });
        Object.entries(panes).forEach(([key, pane]) => {
          pane.classList.toggle('is-active', key === tab.key);
        });
      },
    }, tab.label);
    tabBar.append(button);
    panes[tab.key] = el('div', {
      class: `settings-pane${view.settingsTab === tab.key ? ' is-active' : ''}`,
      role: 'tabpanel',
      'aria-label': tab.label,
    });
  }

  panes.general.append(
    languageCard(),
    formCard(t('settings.general.system.title'), t('settings.general.system.hint'), el('div', { class: 'form-grid' },
      cfgField('系统名称', 'system.name', 'str'),
      cfgField('主机标签', 'system.host_label', 'str'),
      cfgField('语言', 'system.language', 'select', {
        options: [{ value: 'zh-CN', label: '简体中文' }, { value: 'en-US', label: 'English' }],
      }),
    )),
    formCard(t('settings.general.scheduler.title'), t('settings.general.scheduler.hint'), el('div', { class: 'form-grid is-3' },
      cfgField('心跳间隔（秒）', 'agents.heartbeat_s', 'float'),
      cfgField('离线判定（秒）', 'agents.offline_after_s', 'float'),
      cfgField('最大工人数', 'agents.max_workers', 'int'),
      cfgField('单任务最大步数', 'agents.max_steps', 'int'),
      cfgField('任务轮询（秒）', 'agents.task_poll_s', 'float'),
      cfgField('汇总间隔（秒）', 'agents.aggregator_interval_s', 'float'),
      cfgField('重启上限', 'agents.restart_limit', 'int'),
      cfgField('重启窗口（秒）', 'agents.restart_window_s', 'float'),
      cfgField('自动派出主理人', 'agents.auto_spawn_project_lead', 'bool', { full: true }),
    )),
  );

  panes.network.append(
    formCard('传输', 'IPC 适合单机；TCP 用于跨主机连接（修改后需重启）。', el('div', { class: 'form-grid is-3' },
      cfgField('模式', 'transport.mode', 'select', {
        options: [{ value: 'ipc', label: 'IPC（本机）' }, { value: 'tcp', label: 'TCP（网络）' }],
      }),
      cfgField('监听地址', 'transport.host', 'str', { mono: true }),
      cfgField('对外地址', 'transport.advertise_host', 'nullable', { mono: true, hint: '留空表示使用监听地址。' }),
      cfgField('端口池起点', 'transport.port_pool_start', 'int'),
      cfgField('端口池终点', 'transport.port_pool_end', 'int'),
      cfgField('RPC 超时（秒）', 'transport.rpc_timeout_s', 'float'),
      cfgField('IPC 目录', 'transport.ipc_dir', 'nullable', { mono: true, full: true, hint: '留空使用默认运行目录。' }),
      field('系统 RPC 地址', readonlyValue(config.transport?.sys_rpc_address || '启动时分配'), { full: true, hint: '只读。' }),
    )),
    formCard('Web 控制台', '操作台监听地址与 SSE 心跳。', el('div', { class: 'form-grid' },
      cfgField('监听地址', 'web.host', 'str', { mono: true }),
      cfgField('端口', 'web.port', 'int', { hint: '修改后需要重启。' }),
      cfgField('SSE 心跳（秒）', 'web.sse_heartbeat_s', 'float'),
      cfgField('启动时打开浏览器', 'web.open_browser', 'bool', { inline: true }),
    )),
  );

  panes.ai.append(
    renderLlmUsagePanel(),
    formCard(t('settings.ai.routes.title'), t('settings.ai.routes.hint'), renderRoutesEditor()),
    formCard(t('settings.ai.models.title'), t('settings.ai.models.hint'), el('div', { class: 'form-grid' },
      cfgField('Provider', 'llm.provider', 'select', {
        options: [{ value: 'mock', label: 'mock（离线规则）' }, { value: 'openai', label: 'OpenAI 兼容' }],
      }),
      cfgField('模型', 'llm.model', 'str', { mono: true }),
      cfgField('Base URL', 'llm.base_url', 'str', { mono: true, full: true }),
      cfgField('API Key', 'llm.api_key', 'secret', { full: true, hint: '已配置时显示 ***；只有修改时才发送。' }),
      cfgField('Temperature', 'llm.temperature', 'float'),
      cfgField('Max Tokens', 'llm.max_tokens', 'int',
        { hint: '由模型端点决定上限；系统不再裁剪，端点拒绝时「测试连接」会显示错误。' }),
      cfgField('超时（秒）', 'llm.timeout_s', 'float'),
    )),
    formCard(t('settings.ai.test.title'), t('settings.ai.test.hint'),
      (() => {
        const result = el('p', { class: 'form-hint' }, '尚未测试。');
        const button = el('button', { class: 'btn btn-sm btn-primary', type: 'button' }, t('settings.ai.test.title'));
        button.addEventListener('click', async () => {
          button.disabled = true;
          result.textContent = '测试中…';
          try {
            const data = await api.testLlm();
            if (data.ok) {
              result.textContent = `连接成功 · ${data.provider}/${data.model}`
                + (data.latency_ms != null ? ` · ${data.latency_ms}ms` : '');
              toast('模型连接正常', 'success');
            } else {
              result.textContent = `连接失败 · ${data.error || '未知错误'}`;
              toast(data.error || '模型连接失败', 'error');
            }
          } catch (error) {
            result.textContent = `连接失败 · ${error.message || error}`;
            toastError(error);
          } finally {
            button.disabled = false;
          }
        });
        return el('div', {}, button, result);
      })()),
    appNote('API Key 以掩码显示，不会回传明文；留空保存会清除已配置的 Key。'
      + ' 系统助手以「操作员」身份直接执行项目/任务/Agent/审批/配置操作，'
      + '每次动作都记入事件流；工人 Agent 仍走审批口。', { iconName: 'approvals' }),
  );

  panes.capabilities.append(renderCapabilitiesPane());

  panes.security.append(
    renderSessionPanel(),
    formCard(t('settings.security.policy.title'), t('settings.security.policy.hint'), el('div', { class: 'form-grid is-3' },
      cfgField('低风险自动放行', 'security.auto_allow_low', 'bool', { full: true }),
      cfgField('中风险默认', 'security.medium_default', 'select', {
        options: [{ value: 'ask', label: 'ask（询问）' }, { value: 'deny', label: 'deny（拒绝）' }],
      }),
      cfgField('高风险默认', 'security.high_default', 'select', {
        options: [{ value: 'ask', label: 'ask（询问）' }, { value: 'deny', label: 'deny（拒绝）' }],
      }),
      cfgField('审批 TTL（秒）', 'security.approval_ttl_s', 'int'),
      cfgField('授权 TTL（秒）', 'security.grant_ttl_s', 'int'),
      cfgField('Shell 超时（秒）', 'security.shell_timeout_s', 'int'),
      cfgField('允许 Shell', 'security.allow_shell', 'bool'),
      cfgField('预览最大字符', 'security.max_preview_chars', 'int'),
      cfgField('完整预览保存字符', 'security.preview_store_chars', 'int'),
    )),
    appNote('自动放行规则在审批应用的「新增规则」中维护。', { iconName: 'settings' }),
  );

  const paths = config.paths || {};
  const pathTable = el('table', { class: 'readonly-table' });
  for (const [key, value] of Object.entries(paths)) {
    pathTable.append(el('tr', {},
      el('td', { class: 'rt-key' }, key),
      el('td', { class: 'rt-val' }, String(value)),
    ));
  }
  panes.storage.append(
    formCard(t('settings.storage.paths.title'), t('settings.storage.paths.hint'), pathTable),
    formCard(t('settings.storage.events.title'), t('settings.storage.events.hint'), el('table', { class: 'readonly-table' },
      el('tr', {}, el('td', { class: 'rt-key' }, 'last_seq'), el('td', { class: 'rt-val' }, String(state.sys?.last_seq ?? '—'))),
      el('tr', {}, el('td', { class: 'rt-key' }, 'events_loaded'), el('td', { class: 'rt-val' }, String(state.events.length))),
      el('tr', {}, el('td', { class: 'rt-key' }, 'projects_dir'), el('td', { class: 'rt-val' }, String(paths.projects_dir || '—'))),
    )),
  );

  const sys = state.sys || {};
  const stats = state.stats || {};
  panes.about.append(
    el('div', { class: 'about-hero' },
      el('span', { class: 'brand-glyph', 'aria-hidden': 'true' }, icon('brand')),
      el('div', {},
        el('h3', {}, `${sys.name || 'StackFlowOS'} ${sys.version || ''}`.trim()),
        el('p', {}, t('settings.about.tagline')),
      ),
    ),
    el('div', { class: 'about-grid' },
      aboutItem(t('settings.about.host'), sys.host || '—'),
      aboutItem(t('settings.about.pid'), sys.pid ?? '—'),
      aboutItem(t('settings.about.transport'), String(sys.transport || '—').toUpperCase()),
      aboutItem(t('settings.about.web_port'), sys.web_port ?? '—'),
      aboutItem(t('settings.about.started'), fmtDateTime(sys.started_at)),
      aboutItem(t('settings.about.uptime'), fmtUptime(stats.uptime_s)),
      aboutItem(t('settings.about.sys_rpc'), sys.sys_rpc || '—'),
      aboutItem(t('settings.about.sys_events'), sys.sys_events || '—'),
      aboutItem(t('settings.about.seq'), sys.last_seq ?? '—'),
      aboutItem(t('settings.about.projects_agents'), `${stats.projects ?? 0} / ${(state.agents.length)}`),
    ),
  );

  const save = el('button', { class: 'btn btn-primary', type: 'submit' }, icon('check'), t('common.save'));
  const writable = canWrite();
  if (!writable) save.hidden = true;
  const reload = el('button', {
    class: 'btn btn-ghost',
    type: 'button',
    onclick: () => {
      withBusy(reload, async () => {
        try {
          await loadState({ quiet: true });
          view.settingsNotice = [];
          toast(t('settings.reloaded'), 'success');
          renderSheet();
        } catch (error) {
          toastError(error);
        }
      });
    },
  }, icon('refresh'), t('common.reload'));

  const form = el('form', { id: 'settings-form' },
    tabBar,
    ...Object.values(panes),
    el('div', { class: 'settings-actions' },
      el('span', { class: 'sa-note' }, writable ? t('settings.save_hint') : t('settings.viewer_hint')),
      el('div', { class: 'sa-buttons' }, reload, save),
    ),
  );

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (!canWrite()) {
      toast(t('settings.viewer_hint'), 'error');
      return;
    }
    withBusy(save, async () => {
      let patch;
      try {
        patch = collectConfigPatch(form);
      } catch (error) {
        toast(error.message || String(error), 'error');
        return;
      }
      if (!Object.keys(patch).length) {
        toast(t('settings.save_nothing'), 'info');
        return;
      }
      try {
        const data = await api.patchConfig(patch);
        applyConfig(data.config);
        view.settingsNotice = Array.isArray(data.restart_required) ? data.restart_required : [];
        toast(view.settingsNotice.length ? t('settings.saved_restart') : t('settings.saved'), 'success');
        view.settingsReady = Boolean(data.config);
        renderSheet();
      } catch (error) {
        toastError(error);
      }
    });
  });

  wrap.append(form);
  return wrap;
}

function languageCard() {
  const row = el('div', { class: 'lang-switch', role: 'group', 'aria-label': t('settings.language.title') });
  const current = getLang();
  for (const option of ['zh', 'en']) {
    const button = el('button', {
      class: `lang-btn${current === option ? ' is-active' : ''}`,
      type: 'button',
      'aria-pressed': String(current === option),
      lang: option === 'zh' ? 'zh-CN' : 'en',
      onclick: () => {
        if (getLang() === option) return;
        setLang(option);
        toast(t('settings.language.changed', {
          lang: t(option === 'zh' ? 'settings.language.zh' : 'settings.language.en'),
        }), 'success');
      },
    },
    el('span', { class: 'lang-code mono' }, option.toUpperCase()),
    el('span', {}, t(option === 'zh' ? 'settings.language.zh' : 'settings.language.en')),
    );
    row.append(button);
  }
  return formCard(t('settings.language.title'), t('settings.language.hint'), row);
}

function formCard(title, hint, content) {
  return el('section', { class: 'form-card' },
    el('h3', {}, title),
    hint ? el('p', { class: 'form-hint' }, hint) : null,
    content,
  );
}

function readonlyValue(text) {
  return el('div', { class: 'input mono', style: { display: 'flex', 'align-items': 'center', color: 'var(--text-3)' } }, String(text));
}

function aboutItem(label, value) {
  return el('div', { class: 'about-item' },
    el('div', { class: 'ai-label' }, label),
    el('div', { class: 'ai-value' }, String(value)),
  );
}

function getPath(object, path) {
  return path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), object);
}

function cfgField(label, path, kind, options = {}) {
  const raw = getPath(state.config, path);
  const initial = raw === null || raw === undefined ? '' : String(raw);
  let control;
  if (kind === 'select') {
    control = select({ 'aria-label': label, dataset: cfgDataset(path, kind, initial, label) }, options.options || []);
    control.value = raw === null || raw === undefined ? '' : String(raw);
  } else if (kind === 'bool') {
    control = el('input', {
      type: 'checkbox',
      checked: Boolean(raw),
      'aria-label': label,
      dataset: cfgDataset(path, kind, String(Boolean(raw)), label),
    });
  } else if (kind === 'secret') {
    const configured = raw === '***';
    control = el('input', {
      class: 'input mono',
      type: 'password',
      value: configured ? '***' : '',
      autocomplete: 'off',
      placeholder: configured ? '已配置（***）' : '未配置',
      'aria-label': label,
      dataset: cfgDataset(path, 'secret', configured ? '***' : '', label),
    });
  } else if (kind === 'int' || kind === 'float') {
    control = el('input', {
      class: 'input mono',
      type: 'number',
      step: kind === 'int' ? '1' : 'any',
      value: raw === null || raw === undefined ? '' : String(raw),
      'aria-label': label,
      dataset: cfgDataset(path, kind, initial, label),
    });
  } else {
    control = el('input', {
      class: `input${options.mono ? ' mono' : ''}`,
      type: 'text',
      value: raw === null || raw === undefined ? '' : String(raw),
      'aria-label': label,
      dataset: cfgDataset(path, kind, initial, label),
    });
  }
  if (kind === 'bool') {
    return field(label, control, { hint: options.hint || '', full: options.full, inline: true });
  }
  return field(label, control, { hint: options.hint || '', full: options.full });
}

function cfgDataset(path, kind, initial, label) {
  return {
    cfgPath: path,
    cfgKind: kind,
    cfgInitial: initial,
    cfgLabel: label,
  };
}

function setPath(target, path, value) {
  const keys = path.split('.');
  let node = target;
  for (const key of keys.slice(0, -1)) {
    if (!node[key] || typeof node[key] !== 'object') node[key] = {};
    node = node[key];
  }
  node[keys[keys.length - 1]] = value;
}

function collectConfigPatch(form) {
  const patch = {};
  for (const control of form.querySelectorAll('[data-cfg-path]')) {
    const path = control.dataset.cfgPath;
    const kind = control.dataset.cfgKind;
    const initial = control.dataset.cfgInitial ?? '';
    const label = control.dataset.cfgLabel || path;
    let value;
    let changed = false;

    if (kind === 'bool') {
      value = control.checked;
      changed = String(value) !== initial;
    } else if (kind === 'int' || kind === 'float') {
      if (control.value.trim() === '') throw new Error(`${label} 不能为空`);
      value = Number(control.value);
      if (!Number.isFinite(value)) throw new Error(`${label} 需要是数字`);
      changed = String(value) !== String(Number(initial));
    } else if (kind === 'secret') {
      if (control.value === '***') continue;
      value = control.value;
      changed = value !== initial;
    } else if (kind === 'json') {
      const source = control.value.trim();
      let parsed;
      if (!source) {
        parsed = [];
      } else {
        try {
          parsed = JSON.parse(source);
        } catch (error) {
          throw new Error(t('settings.ai.routes.invalid', { label, message: error.message || String(error) }));
        }
      }
      if (!Array.isArray(parsed)) {
        throw new Error(t('settings.ai.routes.invalid', { label, message: 'array expected' }));
      }
      value = parsed;
      let before = [];
      try {
        before = JSON.parse(initial || '[]');
      } catch {
        before = [];
      }
      changed = JSON.stringify(value) !== JSON.stringify(before);
    } else if (kind === 'nullable') {
      value = control.value;
      changed = value !== initial;
    }
    if (changed) setPath(patch, path, value);
  }
  return patch;
}

/* ================================================================== */
/* shared bits                                                         */
/* ================================================================== */

function refreshButton(onClick) {
  const button = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, icon('refresh'), t('common.refresh'));
  button.addEventListener('click', () => withBusy(button, async () => {
    try {
      await onClick();
    } catch (error) {
      toastError(error);
    }
  }));
  return button;
}

function tickCountdowns() {
  if (!els.body) return;
  for (const node of els.body.querySelectorAll('[data-expires]')) {
    const expires = Number(node.dataset.expires);
    const textNode = node.querySelector('.ttl-text');
    if (!textNode || !expires) continue;
    const { text, urgent, expired } = fmtCountdown(expires);
    textNode.textContent = expired ? '已过期' : text;
    node.classList.toggle('is-urgent', urgent);
  }
}
