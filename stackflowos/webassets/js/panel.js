/**
 * panel.js — live work panel: project cards, agent strip, event ticker and the
 * top status bar counters.
 */

import { on, state } from './store.js';
import { openApp } from './apps.js';
import { sendPrompt } from './chat.js';
import {
  agoShort,
  avatar,
  el,
  eventSummary,
  fmtTime,
  icon,
  loadingInline,
  ring,
  roleLabel,
  statusPill,
  toneForType,
} from './ui.js';

/** @type {Record<string, HTMLElement>} */
const els = {};
let renderFrame = null;
let tickerFrame = null;
let lastErrorToast = 0;

const ACTIVE_AGENT_STATUSES = new Set(['working', 'waiting', 'idle', 'spawning']);

export function initPanel() {
  els.grid = document.getElementById('project-grid');
  els.projectCount = document.getElementById('project-count');
  els.agents = document.getElementById('agent-strip');
  els.agentCount = document.getElementById('agent-count');
  els.ticker = document.getElementById('event-ticker');
  els.host = document.getElementById('sys-host');
  els.transport = document.getElementById('transport-pill');
  els.transportLabel = document.getElementById('transport-label');
  els.units = document.getElementById('stat-units');
  els.leases = document.getElementById('stat-leases');
  els.topApprovals = document.getElementById('topbar-approvals');
  els.topApprovalCount = document.getElementById('topbar-approval-count');
  els.dockBadge = document.getElementById('dock-approval-badge');
  els.workHeading = document.getElementById('work-heading');

  on('state', scheduleRender);
  on('event', scheduleTicker);
  on('state-error', (error) => {
    const now = Date.now();
    if (now - lastErrorToast < 15000) return;
    lastErrorToast = now;
    console.warn('[panel] state refresh failed', error);
  });

  render();
}

function scheduleRender() {
  if (renderFrame) return;
  renderFrame = window.requestAnimationFrame(() => {
    renderFrame = null;
    render();
  });
}

function scheduleTicker() {
  if (tickerFrame) return;
  tickerFrame = window.requestAnimationFrame(() => {
    tickerFrame = null;
    renderEvents();
  });
}

function render() {
  renderStatus();
  renderProjects();
  renderAgents();
  renderEvents();
}

/* ------------------------------------------------------------------ */
/* status bar                                                          */
/* ------------------------------------------------------------------ */

function renderStatus() {
  const sys = state.sys || {};
  const stats = state.stats || {};
  if (els.host) {
    els.host.textContent = sys.host || '—';
    els.host.title = sys.host ? `主机 ${sys.host}` : '主机';
  }
  if (els.transport) {
    const mode = String(sys.transport || '').toLowerCase();
    els.transport.dataset.mode = mode;
    if (els.transportLabel) els.transportLabel.textContent = mode ? mode.toUpperCase() : '—';
    els.transport.title = `传输模式 ${mode.toUpperCase() || '未知'}${sys.sys_rpc ? ` · ${sys.sys_rpc}` : ''}`;
  }
  const onlineUnits = state.units.filter((unit) => unit.status === 'online').length;
  if (els.units) els.units.textContent = state.loaded ? String(onlineUnits) : '—';
  const leases = Array.isArray(state.leases) ? state.leases.length : Number(stats.leases) || 0;
  if (els.leases) els.leases.textContent = state.loaded ? String(leases) : '—';

  const pending = state.loaded
    ? state.approvals.length || Number(stats.pending_approvals) || 0
    : 0;
  if (els.topApprovalCount) els.topApprovalCount.textContent = String(pending);
  if (els.topApprovals) {
    els.topApprovals.dataset.count = String(pending);
    els.topApprovals.setAttribute('aria-label', pending ? `打开审批中心，${pending} 件待审批` : '打开审批中心，无待审批');
  }
  if (els.dockBadge) {
    els.dockBadge.hidden = pending === 0;
    els.dockBadge.textContent = pending > 99 ? '99+' : String(pending);
  }
  if (els.workHeading) {
    els.workHeading.textContent = state.loaded
      ? `工作面板 · ${Number(stats.active_projects) || 0} 个项目`
      : '工作面板';
  }
}

/* ------------------------------------------------------------------ */
/* project cards                                                       */
/* ------------------------------------------------------------------ */

const STATUS_RANK = { active: 0, review: 1, degraded: 2, planning: 3, done: 4, archived: 5 };

function sortedProjects() {
  return [...state.projects].sort((a, b) => {
    const rankA = STATUS_RANK[a.degraded ? 'degraded' : a.status] ?? 9;
    const rankB = STATUS_RANK[b.degraded ? 'degraded' : b.status] ?? 9;
    if (rankA !== rankB) return rankA - rankB;
    return (b.updated_at || 0) - (a.updated_at || 0);
  });
}

function renderProjects() {
  if (!els.grid) return;
  if (!state.loaded) {
    els.grid.replaceChildren(
      skeletonCard(),
      skeletonCard(),
    );
    if (els.projectCount) els.projectCount.textContent = '';
    return;
  }

  const projects = sortedProjects();
  if (els.projectCount) {
    const active = projects.filter((project) =>
      ['active', 'review', 'degraded'].includes(project.status)).length;
    els.projectCount.textContent = `${active} 活跃 / ${projects.length} 总计`;
  }

  if (!projects.length) {
    els.grid.replaceChildren(projectEmptyState());
    return;
  }

  els.grid.replaceChildren(...projects.map(projectCard));
}

function skeletonCard() {
  return el('div', { class: 'project-card', 'aria-hidden': 'true' }, loadingInline('载入项目…'));
}

function projectEmptyState() {
  const examples = [
    '创建一个项目：做一个命令行贪吃蛇游戏',
    '创建一个项目：调研 3 个离线语音方案',
    '创建一个项目：搭建个人博客并部署',
  ];
  return el('div', { class: 'empty-state' },
    el('div', { class: 'empty-glyph' }, icon('projects')),
    el('h4', {}, '还没有项目'),
    el('p', {}, '在左侧跟系统说一句话就能开工。它会拆任务、派 Agent，并在卡片上实时汇报进度。'),
    el('div', { class: 'suggestion-row' },
      ...examples.map((text) => el('button', {
        class: 'suggestion-chip',
        type: 'button',
        onclick: () => sendPrompt(text),
      }, text.replace('创建一个项目：', ''))),
    ),
  );
}

function projectCard(project, index) {
  const status = project.degraded ? 'degraded' : project.status;
  const counts = project.counts || {};
  const agents = (project.agents || state.agents.filter((agent) => agent.project_id === project.id))
    .filter((agent) => ACTIVE_AGENT_STATUSES.has(agent.status));
  const workers = agents.slice(0, 3);
  const extra = Math.max(0, agents.length - workers.length);
  const pending = Number(project.pending_approvals) || 0;
  const currentTask = project.current_task;

  const card = el('button', {
    class: 'project-card',
    type: 'button',
    style: { '--card-color': project.color || '#7c5cff' },
    'aria-label': `打开项目 ${project.name}`,
    dataset: { projectId: project.id },
    onclick: () => openApp('project', project.id),
  });

  card.append(
    el('div', { class: 'pc-top' },
      el('div', { style: { 'min-width': '0' } },
        el('div', { class: 'pc-name', title: project.name }, project.name || '未命名项目'),
        el('div', { class: 'pc-id mono', title: project.id }, project.id),
      ),
      statusPill(status),
    ),
    el('div', { class: 'pc-mid' },
      ring(project.progress || 0, { size: index === 0 ? 56 : 52 }),
      el('div', { class: 'pc-counts' },
        countCell('待办', counts.todo, ''),
        countCell('进行', counts.doing, 'is-doing'),
        countCell('待审', counts.review, 'is-review'),
        countCell('完成', counts.done, 'is-done'),
      ),
    ),
    el('div', { class: 'pc-task' },
      icon('terminal', 'ico ico-xs'),
      el('span', { class: 'pc-task-label' }, currentTask ? '当前' : '状态'),
      el('span', { title: currentTask?.title || '' },
        currentTask?.title || projectStateHint(project)),
    ),
    el('div', { class: 'pc-foot' },
      el('div', { style: { display: 'flex', 'align-items': 'center', 'min-width': '0' } },
        el('div', { class: 'avatar-stack' },
          ...workers.map((agent) => avatar(agent)),
          extra > 0 ? el('span', { class: 'avatar-more', title: `${extra} 个 Agent` }, `+${extra}`) : null,
        ),
      ),
      el('div', { class: 'pc-badges' },
        pending > 0
          ? el('span', { class: 'pc-approval', title: `${pending} 件待审批` }, icon('approvals', 'ico ico-xs'), pending)
          : null,
        el('span', { class: 'pc-age', title: '最近更新' }, agoShort(project.updated_at)),
      ),
    ),
  );
  return card;
}

function projectStateHint(project) {
  if (project.status === 'done') return '已完成';
  if (project.status === 'archived') return '已归档';
  if (project.status === 'review') return '等待验收';
  if (project.status === 'planning') return '规划中';
  if (project.degraded) return '需要关注';
  return '等待派工';
}

function countCell(label, value, tone) {
  return el('div', { class: `pc-count${tone ? ` ${tone}` : ''}` },
    el('span', {}, label),
    el('b', {}, String(Number(value) || 0)),
  );
}

/* ------------------------------------------------------------------ */
/* agents strip                                                        */
/* ------------------------------------------------------------------ */

function renderAgents() {
  if (!els.agents) return;
  if (!state.loaded) {
    els.agents.replaceChildren(el('div', { class: 'skeleton-row', style: { width: '180px', height: '44px' } }));
    if (els.agentCount) els.agentCount.textContent = '';
    return;
  }

  const active = state.agents
    .filter((agent) => ACTIVE_AGENT_STATUSES.has(agent.status))
    .sort((a, b) => statusRank(a.status) - statusRank(b.status));
  if (els.agentCount) els.agentCount.textContent = `${active.length} 在岗`;

  if (!active.length) {
    els.agents.replaceChildren(el('p', { class: 'events-empty' },
      '暂无在岗 Agent · 在「Agent」里手动派工'));
    return;
  }

  const projectName = (projectId) => state.projects.find((project) => project.id === projectId)?.name || '';
  els.agents.replaceChildren(...active.slice(0, 16).map((agent) => el('button', {
    class: 'agent-chip',
    type: 'button',
    title: `${agent.display_name || agent.id} · ${agent.status}`,
    onclick: () => openApp('agents'),
  },
  avatar(agent),
  el('div', { class: 'agent-chip-body' },
    el('span', { class: 'agent-chip-name' }, agent.display_name || agent.id),
    el('span', { class: 'agent-chip-role' },
      el('span', { class: `status-dot is-${agent.status}`, 'aria-hidden': 'true' }),
      roleLabel(agent.role),
      projectName(agent.project_id) ? el('span', { class: 'sep' }, '·') : null,
      projectName(agent.project_id)
        ? el('span', { style: { overflow: 'hidden', 'text-overflow': 'ellipsis', 'max-width': '76px' } },
          projectName(agent.project_id))
        : null,
    ),
  ),
  )));
}

function statusRank(status) {
  return { working: 0, waiting: 1, idle: 2, spawning: 3 }[status] ?? 4;
}

/* ------------------------------------------------------------------ */
/* live event ticker                                                   */
/* ------------------------------------------------------------------ */

function renderEvents() {
  if (!els.ticker) return;
  const events = [...state.events]
    .filter((event) => event.type !== 'chat.delta' && event.type !== 'chat.typing')
    .sort((a, b) => (Number(b.seq) || 0) - (Number(a.seq) || 0))
    .slice(0, 40);
  if (!events.length) {
    els.ticker.replaceChildren(el('li', { class: 'events-empty' }, '事件流待命中'));
    return;
  }
  els.ticker.replaceChildren(...events.map((event) => el('li', {
    class: 'tick-row',
    title: `${event.type} · ${event.source || 'sys'}`,
  },
  el('span', { class: 'tick-time' }, fmtTime(event.ts)),
  el('span', { class: `tick-type ${toneForType(event.type)}` }, event.type),
  el('span', { class: 'tick-msg' }, eventSummary(event)),
  )));
}
