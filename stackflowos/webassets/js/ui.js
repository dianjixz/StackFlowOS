/**
 * ui.js — tiny DOM toolkit + formatting helpers.
 *
 * Security rule for the whole frontend: user/agent supplied strings are only
 * ever written through `textContent` (never innerHTML). The only innerHTML in
 * the app is the static icon sprite embedded in index.html.
 */

/** @typedef {string|Node|number|null|undefined|false} Child */

const SVG_NS = 'http://www.w3.org/2000/svg';

/**
 * Create an element.
 * @param {string} tag
 * @param {Record<string, any>} [attrs]
 * @param {...Child|Child[]} children
 */
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  applyAttrs(node, attrs);
  append(node, ...children);
  return node;
}

function applyAttrs(node, attrs) {
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = String(value);
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key === 'style' && typeof value === 'object') Object.assign(node.style, value);
    else if (key === 'html') {
      // Reserved for static, developer-authored SVG only. Never pass user text.
      node.innerHTML = String(value);
    } else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
}

/**
 * Append children, flattening arrays; strings become text nodes.
 * @param {Node} node
 * @param {...Child|Child[]} children
 */
export function append(node, ...children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** @param {string} name @param {string} [className] */
export function icon(name, className = 'ico') {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', className);
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', `#i-${name}`);
  svg.append(use);
  return svg;
}

/** Replace a node's children in place. */
export function replace(node, ...children) {
  node.replaceChildren();
  append(node, ...children);
  return node;
}

/* ------------------------------------------------------------------ */
/* formatting                                                          */
/* ------------------------------------------------------------------ */

export const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
export const nowMs = () => Date.now();

/** @param {number|undefined|null} ts */
export function toDate(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return null;
  return new Date(n < 1e12 ? n * 1000 : n);
}

const pad2 = (n) => String(n).padStart(2, '0');

/** HH:MM */
export function fmtClock(ts = Date.now()) {
  const date = toDate(ts);
  if (!date) return '--:--';
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** HH:MM:SS */
export function fmtTime(ts) {
  const date = toDate(ts);
  if (!date) return '--:--:--';
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

/** YYYY-MM-DD HH:MM */
export function fmtDateTime(ts) {
  const date = toDate(ts);
  if (!date) return '—';
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ` +
    `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** Verbose Chinese relative time. */
export function ago(ts, now = Date.now()) {
  const date = toDate(ts);
  if (!date) return '—';
  const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (seconds < 10) return '刚刚';
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return fmtDateTime(date.getTime());
}

/** Compact relative time for dense cards. */
export function agoShort(ts, now = Date.now()) {
  const date = toDate(ts);
  if (!date) return '—';
  const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (seconds < 10) return '刚刚';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/** Seconds → mm:ss (TTL countdown). */
export function fmtCountdown(targetTs, now = Date.now()) {
  const date = toDate(targetTs);
  if (!date) return { text: '—', urgent: false, expired: false };
  const left = Math.max(0, Math.round((date.getTime() - now) / 1000));
  const minutes = Math.floor(left / 60);
  const seconds = left % 60;
  return {
    text: `${pad2(minutes)}:${pad2(seconds)}`,
    urgent: left <= 60,
    expired: left <= 0,
  };
}

/** Uptime seconds → "3 小时 12 分". */
export function fmtUptime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return `${minutes} 分钟`;
  return `${total} 秒`;
}

/** Truncate a long single-line string. */
export function clip(text, max = 120) {
  const value = String(text ?? '').replace(/\s+/g, ' ').trim();
  return value.length > max ? `${value.slice(0, Math.max(0, max - 1))}…` : value;
}

/* ------------------------------------------------------------------ */
/* domain labels                                                       */
/* ------------------------------------------------------------------ */

const STATUS_LABELS = {
  planning: '规划中',
  active: '进行中',
  review: '待验收',
  done: '已完成',
  degraded: '降级',
  archived: '已归档',
  todo: '待办',
  doing: '进行中',
  blocked: '阻塞',
  pending: '待审批',
  approved: '已批准',
  denied: '已拒绝',
  expired: '已过期',
  canceled: '已取消',
  spawning: '启动中',
  idle: '空闲',
  working: '工作中',
  waiting: '等待',
  failed: '失败',
  dead: '已退出',
  online: '在线',
  suspect: '延迟',
  offline: '离线',
  quarantined: '隔离',
  running: '运行中',
  ok: '正常',
};

const ROLE_LABELS = {
  lead: '主理人',
  architect: '架构师',
  coder: '工程师',
  tester: '测试',
  reviewer: '评审',
  researcher: '研究',
  aggregator: '汇总',
  assistant: '助手',
  operator: '操作员',
};

const APP_LABELS = {
  projects: '项目',
  agents: 'Agent',
  units: '单元',
  approvals: '审批',
  events: '事件',
  settings: '设置',
};

export const statusLabel = (status) => STATUS_LABELS[String(status || '')] || String(status || '未知');
export const roleLabel = (role) => ROLE_LABELS[String(role || '')] || String(role || '—');
export const appLabel = (app) => APP_LABELS[String(app || '')] || String(app || '');

/** @param {string} status */
export function statusPill(status, label = null) {
  return el('span', { class: 'pill', dataset: { status: String(status || '') } }, label || statusLabel(status));
}

export function riskPill(risk) {
  return el('span', { class: 'risk-pill', dataset: { risk: String(risk || 'low') } }, String(risk || 'low'));
}

export function roleChip(role) {
  return el('span', { class: 'role-chip' }, String(role || '—'));
}

/** Map event type to a CSS tone class. */
export function toneForType(type) {
  const head = String(type || '').split('.')[0];
  const known = ['chat', 'project', 'task', 'agent', 'unit', 'approval', 'rule', 'tool', 'failure', 'sys'];
  return `tone-${known.includes(head) ? head : 'sys'}`;
}

/** Readable one-line summary of an event's data. */
export function eventSummary(evt) {
  const data = (evt && evt.data) || {};
  const type = String((evt && evt.type) || '');
  const head = type.split('.')[0];
  const parts = [];
  const push = (value) => {
    if (value !== undefined && value !== null && String(value).trim() !== '') {
      parts.push(clip(value, 140));
    }
  };

  if (head === 'chat') {
    if (data.text) push(data.text);
    else if (data.delta) push(data.delta);
    else if (data.typing) push('助手正在思考');
    else if (data.message_id) push(String(data.message_id));
  } else if (head === 'project') {
    push(data.name || data.title);
    if (data.status) push(statusLabel(data.status));
    if (data.project_id && !data.name) push(data.project_id);
    if (data.by) push(`由 ${data.by}`);
  } else if (head === 'task') {
    push(data.title);
    if (data.status) push(statusLabel(data.status));
    if (data.task_id) push(data.task_id);
    if (data.assignee) push(`@${data.assignee}`);
  } else if (head === 'agent') {
    push(data.display_name || data.role || data.agent_id || data.id);
    if (data.status) push(statusLabel(data.status));
    if (data.project_id) push(data.project_id);
    if (data.error) push(data.error);
  } else if (head === 'unit') {
    push(data.work_id || data.id);
    if (data.status) push(statusLabel(data.status));
    if (data.kind) push(data.kind);
  } else if (head === 'approval') {
    push(data.capability);
    if (data.risk) push(`风险 ${data.risk}`);
    if (data.principal) push(data.principal);
    if (data.decision) push(data.decision === 'allow' ? '已允许' : '已拒绝');
    if (data.request_id && !data.capability) push(data.request_id);
  } else if (head === 'rule') {
    push(data.capability);
    if (data.effect) push(data.effect);
    if (data.principal) push(data.principal);
  } else if (head === 'tool') {
    push(data.tool || data.name);
    if (data.status) push(data.status);
    if (data.agent_id) push(data.agent_id);
    if (data.path) push(data.path);
  } else if (head === 'failure') {
    push(data.message || data.reason || data.error);
    if (data.work_id) push(data.work_id);
  } else if (head === 'sys') {
    push(data.message || data.summary);
    if (data.patch) push(JSON.stringify(data.patch));
  }

  if (!parts.length) {
    const source = data.summary || data.message || data.id || data.seq;
    if (source) push(source);
  }
  return parts.length ? parts.join(' · ') : type;
}

/* ------------------------------------------------------------------ */
/* avatars                                                             */
/* ------------------------------------------------------------------ */

const AVATAR_COLORS = ['#9a80ff', '#5bc0ff', '#3ddc97', '#ffb454', '#ff7ea0', '#57d6d0', '#c19dff', '#7ea6ff'];

function hashString(text) {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
  return hash;
}

export function colorFor(seed) {
  return AVATAR_COLORS[hashString(String(seed || 'x')) % AVATAR_COLORS.length];
}

export function initialsFor(agent) {
  if (agent && typeof agent === 'object') {
    const role = String(agent.role || '').trim();
    const id = String(agent.id || agent.display_name || '');
    const roleChar = role ? role[0].toUpperCase() : (agent.display_name || '?')[0];
    const digits = (id.match(/\d+/) || [])[0] || '';
    return `${roleChar}${digits.slice(-2)}`.slice(0, 3);
  }
  const text = String(agent || '?').replace(/[·\s]/g, '');
  return text.slice(0, 2).toUpperCase() || '?';
}

/** @param {any} agent */
export function avatar(agent, { large = false } = {}) {
  const seed = agent && typeof agent === 'object'
    ? (agent.project_id || agent.id || agent.role || 'agent')
    : String(agent || 'agent');
  const color = colorFor(seed);
  const label = agent && typeof agent === 'object' ? (agent.display_name || agent.id || agent.role) : String(agent || '');
  return el('span', {
    class: `avatar${large ? ' is-lg' : ''}`,
    style: { '--av-color': color },
    title: label,
    'aria-hidden': 'true',
  }, initialsFor(agent));
}

/* ------------------------------------------------------------------ */
/* small components                                                    */
/* ------------------------------------------------------------------ */

/** SVG progress ring with a centered percentage. */
export function ring(progress, { size = 54, stroke = 4.5 } = {}) {
  const value = clamp(Math.round(Number(progress) || 0), 0, 100);
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - value / 100);
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'pc-ring');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('viewBox', `0 0 ${size} ${size}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `进度 ${value}%`);

  const center = size / 2;
  const track = document.createElementNS(SVG_NS, 'circle');
  track.setAttribute('class', 'ring-track');
  track.setAttribute('cx', String(center));
  track.setAttribute('cy', String(center));
  track.setAttribute('r', String(radius));
  track.setAttribute('fill', 'none');
  track.setAttribute('stroke-width', String(stroke));

  const fill = document.createElementNS(SVG_NS, 'circle');
  fill.setAttribute('class', 'ring-fill');
  fill.setAttribute('cx', String(center));
  fill.setAttribute('cy', String(center));
  fill.setAttribute('r', String(radius));
  fill.setAttribute('fill', 'none');
  fill.setAttribute('stroke-width', String(stroke));
  fill.setAttribute('stroke-linecap', 'round');
  fill.setAttribute('stroke-dasharray', `${circumference} ${circumference}`);
  fill.setAttribute('stroke-dashoffset', String(offset));
  fill.setAttribute('transform', `rotate(-90 ${center} ${center})`);

  const text = document.createElementNS(SVG_NS, 'text');
  text.setAttribute('class', 'ring-text');
  text.setAttribute('x', String(center));
  text.setAttribute('y', String(center));
  text.setAttribute('text-anchor', 'middle');
  text.setAttribute('dominant-baseline', 'central');
  text.textContent = `${value}%`;

  svg.append(track, fill, text);
  return svg;
}

export function bar(progress, { tone = '' } = {}) {
  const value = clamp(Math.round(Number(progress) || 0), 0, 100);
  return el('div', { class: `bar${tone ? ` is-${tone}` : ''}`, role: 'progressbar',
    'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(value) },
  el('i', { style: { width: `${value}%` } }));
}

export function spinner() {
  return el('span', { class: 'spinner', 'aria-hidden': 'true' });
}

export function loadingInline(text = '载入中…') {
  return el('div', { class: 'loading-inline' }, spinner(), text);
}

/**
 * Empty state block.
 * @param {{iconName?:string,title:string,text?:string,actions?:Node[]}} options
 */
export function emptyState({ iconName = 'projects', title, text = '', actions = [] } = {}) {
  return el('div', { class: 'empty-state' },
    el('div', { class: 'empty-glyph' }, icon(iconName)),
    el('h4', {}, title || '暂无数据'),
    text ? el('p', {}, text) : null,
    actions.length ? el('div', { class: 'suggestion-row' }, ...actions) : null,
  );
}

/** @param {string} title @param {{hint?:string, action?:Node}} [options] */
export function sectionHead(title, { hint = '', action = null } = {}) {
  return el('div', { class: 'section-head' },
    el('h3', {}, title),
    action || (hint ? el('span', { class: 'section-hint' }, hint) : null),
  );
}

export function appNote(text, { warn = false, iconName = 'alert' } = {}) {
  return el('div', { class: `app-note${warn ? ' is-warn' : ''}` }, icon(iconName), el('span', {}, text));
}

/** A labelled form field. */
export function field(label, control, { hint = '', full = false, inline = false } = {}) {
  return el('div', { class: `field${full ? ' is-full' : ''}${inline ? ' is-inline' : ''}` },
    el('label', {}, label),
    control,
    hint ? el('span', { class: 'field-hint' }, hint) : null,
  );
}

export const input = (attrs = {}) => el('input', { class: 'input', ...attrs });
export const textarea = (attrs = {}) => el('textarea', { class: 'textarea', ...attrs });
export const select = (attrs = {}, options = []) => {
  const node = el('select', { class: 'select', ...attrs });
  for (const option of options) {
    const { value, label } = typeof option === 'string' ? { value: option, label: option } : option;
    node.append(el('option', { value }, label));
  }
  return node;
};

/* ------------------------------------------------------------------ */
/* toasts                                                              */
/* ------------------------------------------------------------------ */

const TOAST_ICONS = { error: 'alert', success: 'check', info: 'events' };

/**
 * Show a transient message. Errors include `error.message` from the API.
 * @param {string} message
 * @param {'error'|'success'|'info'} [kind]
 * @param {number} [timeout]
 */
export function toast(message, kind = 'error', timeout = 4600) {
  const host = document.getElementById('toast-stack');
  if (!host) return;
  const text = String(message || '未知错误');
  const node = el('div', { class: 'toast', dataset: { kind }, role: kind === 'error' ? 'alert' : 'status' },
    el('span', { class: 'toast-icon' }, icon(TOAST_ICONS[kind] || 'events', 'ico ico-sm')),
    el('span', { class: 'toast-msg' }, text),
    el('button', { class: 'toast-close', type: 'button', 'aria-label': '关闭提示' },
      icon('close', 'ico ico-sm')),
  );
  const close = () => {
    if (!node.isConnected) return;
    node.classList.add('is-leaving');
    window.setTimeout(() => node.remove(), 190);
  };
  node.querySelector('.toast-close').addEventListener('click', close);
  host.append(node);
  while (host.children.length > 4) host.firstElementChild.remove();
  if (timeout > 0) window.setTimeout(close, timeout);
  return close;
}

/** Turn an API/other error into a toast. */
export function toastError(error) {
  const message = error && error.message ? error.message : String(error || '操作失败');
  toast(message, 'error');
}

/* ------------------------------------------------------------------ */
/* async button helpers                                                */
/* ------------------------------------------------------------------ */

/**
 * Disable a button while an async task runs; always restores it.
 * @template T
 * @param {HTMLElement|null} button
 * @param {() => Promise<T>} task
 * @returns {Promise<T>}
 */
export async function withBusy(button, task) {
  if (button) {
    if (button.dataset.busy === '1') return undefined;
    button.dataset.busy = '1';
    button.disabled = true;
    button.classList.add('is-busy');
  }
  try {
    return await task();
  } finally {
    if (button) {
      button.dataset.busy = '0';
      button.disabled = false;
      button.classList.remove('is-busy');
    }
  }
}

/** Escape-safe rich text: **bold**, `inline code`, newlines via CSS. */
export function richText(text) {
  const fragment = document.createDocumentFragment();
  const source = String(text ?? '');
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
  let last = 0;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    if (match.index > last) fragment.append(document.createTextNode(source.slice(last, match.index)));
    const token = match[0];
    if (token.startsWith('**')) {
      fragment.append(el('strong', {}, token.slice(2, -2)));
    } else {
      fragment.append(el('code', {}, token.slice(1, -1)));
    }
    last = match.index + token.length;
  }
  if (last < source.length) fragment.append(document.createTextNode(source.slice(last)));
  return fragment;
}
