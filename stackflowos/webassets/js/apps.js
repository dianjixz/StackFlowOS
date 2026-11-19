/**
 * apps.js — the slide-up application sheets.
 *
 * Projects (list + detail + task board), Agent roster, Units, Approvals,
 * Event console and Settings. Deep-linkable through location.hash:
 *   #projects #agents #units #approvals #events #settings #project/<id>
 */

import { api } from './api.js';
import { applyConfig, loadState, on, state } from './store.js';
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
  projects: { title: '项目', sub: '项目列表 · 任务看板 · 归档', icon: 'projects' },
  agents: { title: 'Agent', sub: '花名册 · 派工 · 终止', icon: 'agents' },
  units: { title: '单元', sub: '进程 · 地址 · 能力', icon: 'units' },
  approvals: { title: '审批', sub: '风险操作的人工闸门', icon: 'approvals' },
  events: { title: '事件', sub: '系统控制台 · 审计流', icon: 'events' },
  settings: { title: '设置', sub: '系统 · 网络 · AI · 安全 · 存储', icon: 'settings' },
};

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

/** @type {Record<string, HTMLElement>} */
const els = {};
let current = null;
let lastFocused = null;
let renderTimer = null;
let pendingRender = false;
let consoleTimer = null;

const view = {
  projectTasks: [],
  tasksProjectId: '',
  tasksLoading: false,
  tasksError: '',
  tasksToken: 0,
  selectedTaskId: null,
  agentFilter: 'active',
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

  on('state', () => {
    if (!current) return;
    if (current.name === 'settings' && view.settingsReady) return;
    if (isFormFocused()) {
      pendingRender = true;
      return;
    }
    if (current.name === 'approvals' && !view.rulesLoading) view.rules = state.rules;
    scheduleSheetRender();
  });
  on('event', (event) => {
    if (!current) return;
    if (current.name === 'events') scheduleConsoleReload();
    if (current.name === 'approvals' && String(event.type || '').startsWith('approval.')) {
      scheduleDecisionsReload();
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
  if (Object.prototype.hasOwnProperty.call(APP_META, name)) {
    return { app: name, projectId: null };
  }
  return null;
}

function applyHash() {
  const route = parseHash();
  if (!route) {
    if (current) hideSheet({ clearHash: false });
    return;
  }
  showApp(route.app, route.projectId, { fromHash: true });
}

/** Open an app (or a project detail) and reflect it in the URL hash. */
export function openApp(name, projectId = null) {
  if (!Object.prototype.hasOwnProperty.call(APP_META, name)) return;
  const target = name === 'projects' && projectId
    ? `#project/${encodeURIComponent(projectId)}`
    : `#${name}`;
  if (location.hash === target) applyHash();
  else location.hash = target;
}

export function closeSheet() {
  hideSheet({ clearHash: true });
}

function hideSheet({ clearHash }) {
  const hadCurrent = Boolean(current);
  current = null;
  pendingRender = false;
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

function showApp(name, projectId = null, { fromHash = false } = {}) {
  if (!APP_META[name]) return;
  if (!fromHash) {
    openApp(name, projectId);
    return;
  }
  const wasHidden = els.layer.hidden;
  if (wasHidden) lastFocused = document.activeElement;
  current = { name, projectId: projectId || null };
  els.layer.hidden = false;
  document.body.classList.add('sheet-open');
  setSheetHeader(APP_META[name].title, APP_META[name].sub, APP_META[name].icon);
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

function scheduleSheetRender() {
  if (renderTimer) return;
  renderTimer = window.setTimeout(() => {
    renderTimer = null;
    if (current) renderSheet();
  }, 80);
}

function renderSheet({ preserveScroll = true } = {}) {
  if (!current) return;
  const scrollTop = preserveScroll ? els.body.scrollTop : 0;
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

function renderProjectDetail(project) {
  const status = project.degraded ? 'degraded' : project.status;
  setSheetHeader(project.name || '项目详情', `项目详情 · ${statusLabel(status)}`, 'projects');

  const tasks = view.tasksLoading ? [] : view.projectTasks;
  const counts = project.counts || {};
  const total = project.total_tasks ?? Object.values(counts).reduce((sum, n) => sum + (Number(n) || 0), 0);
  const agents = (project.agents || state.agents.filter((agent) => agent.project_id === project.id))
    .filter((agent) => !['dead', 'failed'].includes(agent.status));

  const wrap = el('div', {}, backButton());

  const heroRight = el('div', { class: 'dh-actions' },
    ring(project.progress || 0, { size: 76, stroke: 6 }),
    el('div', { class: 'dh-progress mono' },
      el('b', {}, `${Math.round(project.progress || 0)}`), el('span', {}, '% 完成')),
  );
  const actionRow = el('div', { class: 'form-actions', style: { 'justify-content': 'flex-end' } });

  if (!['done', 'archived'].includes(project.status)) {
    const completeBtn = el('button', { class: 'btn btn-ok btn-sm', type: 'button' }, icon('check'), '完成项目');
    completeBtn.addEventListener('click', () => {
      if (!window.confirm(`确认完成项目「${project.name}」？\n所有待审任务会被标记完成，项目 Agent 将停止。`)) return;
      withBusy(completeBtn, async () => {
        try {
          await api.completeProject(project.id);
          toast('项目已标记完成', 'success');
          await loadState({ quiet: true });
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

  /* agents on this project */
  wrap.append(sectionHead('本项目 Agent', {
    hint: agents.length ? `${agents.length} 个在岗` : '暂无 Agent',
  }));
  if (agents.length) {
    wrap.append(el('div', { class: 'agent-strip' },
      ...agents.map((agent) => el('button', {
        class: 'agent-chip',
        type: 'button',
        title: `${agent.display_name || agent.id} · ${statusLabel(agent.status)}`,
        onclick: () => openApp('agents'),
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
    wrap.append(renderTaskBoard());
    const selected = view.projectTasks.find((task) => task.id === view.selectedTaskId);
    if (selected) wrap.append(renderTaskDetail(selected));
  }

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
  const titleBtn = el('button', {
    class: 'tc-title-btn',
    type: 'button',
    onclick: (event) => {
      event.stopPropagation();
      view.selectedTaskId = selected ? null : task.id;
      renderSheet();
    },
  }, el('span', { class: 'tc-title' }, task.title || task.id));
  const card = el('div', {
    class: `task-card${selected ? ' is-selected' : ''}${task.status === 'blocked' ? ' is-blocked' : ''}`,
    dataset: { taskId: task.id },
  }, titleBtn);
  card.append(el('div', { class: 'tc-meta' },
    task.role ? roleChip(task.role) : null,
    Number(task.priority) > 0 ? el('span', { class: 'tc-id' }, `P${task.priority}`) : null,
    checkTotal ? el('span', { class: 'tc-checks' }, `✓ ${checkDone}/${checkTotal}`) : null,
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

  wrap.append(renderSpawnForm());

  const filters = [
    { key: 'active', label: '在岗' },
    { key: 'all', label: '全部' },
    { key: 'ended', label: '已结束' },
  ];
  wrap.append(el('div', { class: 'filter-chips', style: { 'margin-top': '16px' } },
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
    if (view.agentFilter === 'ended') return ['done', 'failed', 'dead'].includes(agent.status);
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
      text: '换个筛选条件看看。',
    }));
  } else {
    const groups = new Map();
    for (const agent of filtered) {
      const key = agent.project_id || '__none__';
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(agent);
    }
    const order = { working: 0, waiting: 1, idle: 2, spawning: 3, done: 4, failed: 5, dead: 6 };
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
  const terminal = ['done', 'failed', 'dead'].includes(agent.status);
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

  return el('div', { class: 'agent-row' },
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
        el('span', {}, `心跳 ${ago(agent.last_seen)}`),
      ),
      agent.summary ? el('div', { class: 'ar-summary', title: agent.summary }, agent.summary) : null,
      agent.error ? el('div', { class: 'ar-summary', style: { color: 'var(--danger)' } }, agent.error) : null,
    ),
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
  const wrap = el('div', {});
  const units = state.units;
  wrap.append(el('div', { class: 'app-toolbar' },
    el('div', { class: 'toolbar-left' },
      el('span', { class: 'app-count' },
        `${units.filter((unit) => unit.status === 'online').length} 在线 / ${units.length} 总计`),
    ),
    el('div', { class: 'toolbar-right' }, refreshButton(() => loadState({ quiet: true }))),
  ));
  wrap.append(appNote('单元是承载 Agent 的进程。强制终止会要求它退出；离线单元会在心跳超时后自动标记。', { iconName: 'units' }));

  if (!state.loaded) {
    wrap.append(loadingInline('载入单元…'));
    return wrap;
  }
  if (!units.length) {
    wrap.append(emptyState({
      iconName: 'units',
      title: '还没有单元',
      text: '创建项目或派工后，工作进程会注册到这里，并显示它们的 RPC 地址与能力。',
    }));
    return wrap;
  }

  const table = el('table', { class: 'data-table' });
  table.append(el('thead', {}, el('tr', {},
    ...['单元', '状态', '传输', 'RPC 地址', '事件地址', 'PID', '最近心跳', '能力', '']
      .map((label) => el('th', { scope: 'col' }, label)),
  )));
  const tbody = el('tbody');
  for (const unit of units) {
    const killBtn = el('button', {
      class: 'btn btn-xs btn-danger',
      type: 'button',
      'aria-label': `终止单元 ${unit.work_id}`,
      onclick: () => {
        if (!window.confirm(`终止单元 ${unit.work_id}？\n正在使用它的 Agent 会被中断。`)) return;
        withBusy(killBtn, async () => {
          try {
            await api.killUnit(unit.work_id);
            toast('终止信号已发送', 'success');
            await loadState({ quiet: true });
            renderSheet();
          } catch (error) {
            toastError(error);
          }
        });
      },
    }, '终止');

    const abilities = Array.isArray(unit.abilities) ? unit.abilities : [];
    const abilityNames = abilities.slice(0, 4).map((ability) =>
      typeof ability === 'string' ? ability : (ability.name || ability.tool || 'ability'));
    tbody.append(el('tr', {},
      el('td', {},
        el('div', { class: 'unit-id' }, unit.work_id),
        el('div', { class: 'muted', style: { 'font-size': '9px' } },
          [unit.label, unit.kind].filter(Boolean).join(' · ') || '—'),
      ),
      el('td', {}, statusPill(unit.status)),
      el('td', { class: 'mono' }, String(unit.transport || '—').toUpperCase()),
      el('td', {}, el('div', { class: 'unit-addr', title: unit.rpc_address || '' }, unit.rpc_address || '—')),
      el('td', {}, el('div', { class: 'unit-addr', title: unit.events_address || '' }, unit.events_address || '—')),
      el('td', { class: 'mono' }, String(unit.pid ?? '—')),
      el('td', { class: 'mono', title: fmtDateTime(unit.last_seen) }, ago(unit.last_seen)),
      el('td', {},
        abilities.length
          ? el('div', { class: 'ability-list' },
            ...abilityNames.map((name) => el('span', { class: 'ability-pill' }, name)),
            abilities.length > abilityNames.length
              ? el('span', { class: 'ability-pill' }, `+${abilities.length - abilityNames.length}`)
              : null,
          )
          : el('span', { class: 'muted' }, '—'),
      ),
      el('td', { class: 'cell-actions' }, killBtn),
    ));
  }
  table.append(tbody);
  wrap.append(el('div', { class: 'table-wrap' }, table));
  return wrap;
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
  if (view.decisionsLoading && !view.recentDecisions.length) {
    wrap.append(loadingInline('载入决定记录…'));
  } else if (!view.recentDecisions.length) {
    wrap.append(appNote('还没有审批决定记录。', { iconName: 'events' }));
  } else {
    const list = el('div', { class: 'decision-list' });
    for (const event of view.recentDecisions.slice(0, 20)) {
      const data = event.data || {};
      const main = data.request_id
        ? `${data.request_id} · ${data.decision === 'allow' ? '允许' : data.decision === 'deny' ? '拒绝' : '决定'}` +
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
        await Promise.all([loadState({ quiet: true }), loadDecisions()]);
        renderSheet();
      })
      .catch((error) => {
        toastError(error);
        if (card.isConnected) buttons.forEach((button) => { button.disabled = false; });
      });
  };

  actions.append(
    el('button', { class: 'btn btn-sm btn-ok', type: 'button', onclick: () => decide('allow', 'once', '一次') }, '允许一次'),
    el('button', { class: 'btn btn-sm', type: 'button', onclick: () => decide('allow', 'session', '1 小时') }, '允许 1 小时'),
    el('button', { class: 'btn btn-sm btn-primary', type: 'button', onclick: () => decide('allow', 'always', '总是') }, '总是允许'),
    el('span', { class: 'spacer' }),
    el('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => decide('deny', 'once', '拒绝') }, '拒绝'),
  );

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
    approval.preview ? el('pre', { class: 'approval-preview' }, approval.preview) : null,
    actions,
  );
  return card;
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

function renderRuleForm() {
  const errorNode = el('div', { class: 'form-error', role: 'alert' });
  const effect = select({ 'aria-label': '规则效果' }, [
    { value: 'allow', label: '允许 allow' },
    { value: 'deny', label: '拒绝 deny' },
  ]);
  const capability = input({ placeholder: '例如 shell.run 或 fs.write', class: 'input mono', 'aria-label': '能力' });
  const principal = input({ value: '*', placeholder: '*', class: 'input mono', 'aria-label': '主体' });
  const riskMax = select({ 'aria-label': '风险上限' }, [
    { value: 'low', label: 'low' },
    { value: 'medium', label: 'medium' },
    { value: 'high', label: 'high' },
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
    el('p', { class: 'form-hint' }, '规则只影响之后的操作；deny 优先于 allow。'),
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
    if (!capabilityValue) {
      errorNode.textContent = '能力 capability 不能为空。';
      capability.focus();
      return;
    }
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
    { key: 'general', label: '常规' },
    { key: 'network', label: '网络' },
    { key: 'ai', label: 'AI' },
    { key: 'security', label: '安全' },
    { key: 'storage', label: '存储' },
    { key: 'about', label: '关于' },
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
    formCard('系统', '显示名、主机标签与界面语言。', el('div', { class: 'form-grid' },
      cfgField('系统名称', 'system.name', 'str'),
      cfgField('主机标签', 'system.host_label', 'str'),
      cfgField('语言', 'system.language', 'select', {
        options: [{ value: 'zh-CN', label: '简体中文' }, { value: 'en-US', label: 'English' }],
      }),
    )),
    formCard('调度与心跳', 'Agent 心跳、离线判定与重启策略。', el('div', { class: 'form-grid is-3' },
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
    formCard('模型服务', 'provider=mock 时完全离线；openai 走兼容端点。', el('div', { class: 'form-grid' },
      cfgField('Provider', 'llm.provider', 'select', {
        options: [{ value: 'mock', label: 'mock（离线规则）' }, { value: 'openai', label: 'OpenAI 兼容' }],
      }),
      cfgField('模型', 'llm.model', 'str', { mono: true }),
      cfgField('Base URL', 'llm.base_url', 'str', { mono: true, full: true }),
      cfgField('API Key', 'llm.api_key', 'secret', { full: true, hint: '已配置时显示 ***；只有修改时才发送。' }),
      cfgField('Temperature', 'llm.temperature', 'float'),
      cfgField('Max Tokens', 'llm.max_tokens', 'int'),
      cfgField('超时（秒）', 'llm.timeout_s', 'float'),
    )),
    appNote('API Key 以掩码显示，不会回传明文；留空保存会清除已配置的 Key。', { iconName: 'approvals' }),
  );

  panes.security.append(
    formCard('默认策略', '未命中规则时的处理方式。deny 优先于 allow。', el('div', { class: 'form-grid is-3' },
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
    formCard('路径', '所有运行期数据都落在仓库的 .stackflowos/ 下。', pathTable),
    formCard('事件日志', '事件是追加写的；这里只读。', el('table', { class: 'readonly-table' },
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
        el('p', {}, 'Agent 原生的本机操作系统 · 以对话驱动项目执行。'),
      ),
    ),
    el('div', { class: 'about-grid' },
      aboutItem('主机', sys.host || '—'),
      aboutItem('PID', sys.pid ?? '—'),
      aboutItem('传输', String(sys.transport || '—').toUpperCase()),
      aboutItem('Web 端口', sys.web_port ?? '—'),
      aboutItem('启动时间', fmtDateTime(sys.started_at)),
      aboutItem('运行时长', fmtUptime(stats.uptime_s)),
      aboutItem('系统 RPC', sys.sys_rpc || '—'),
      aboutItem('系统事件', sys.sys_events || '—'),
      aboutItem('事件序号', sys.last_seq ?? '—'),
      aboutItem('项目 / Agent', `${stats.projects ?? 0} / ${(state.agents.length)}`),
    ),
  );

  const save = el('button', { class: 'btn btn-primary', type: 'submit' }, icon('check'), '保存更改');
  const reload = el('button', {
    class: 'btn btn-ghost',
    type: 'button',
    onclick: () => {
      withBusy(reload, async () => {
        try {
          await loadState({ quiet: true });
          view.settingsNotice = [];
          toast('配置已重新载入', 'success');
          renderSheet();
        } catch (error) {
          toastError(error);
        }
      });
    },
  }, icon('refresh'), '重新载入');

  const form = el('form', { id: 'settings-form' },
    tabBar,
    ...Object.values(panes),
    el('div', { class: 'settings-actions' },
      el('span', { class: 'sa-note' }, '保存只会提交改动过的字段。'),
      el('div', { class: 'sa-buttons' }, reload, save),
    ),
  );

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    withBusy(save, async () => {
      let patch;
      try {
        patch = collectConfigPatch(form);
      } catch (error) {
        toast(error.message || String(error), 'error');
        return;
      }
      if (!Object.keys(patch).length) {
        toast('没有需要保存的更改', 'info');
        return;
      }
      try {
        const data = await api.patchConfig(patch);
        applyConfig(data.config);
        view.settingsNotice = Array.isArray(data.restart_required) ? data.restart_required : [];
        toast(view.settingsNotice.length ? '配置已保存，部分更改需重启' : '配置已保存', 'success');
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
    } else if (kind === 'nullable') {
      value = control.value.trim() === '' ? null : control.value;
      changed = String(value ?? '') !== initial;
    } else {
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
  const button = el('button', { class: 'btn btn-ghost btn-sm', type: 'button' }, icon('refresh'), '刷新');
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
