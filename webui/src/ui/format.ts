/**
 * format.ts — time, label and event helpers shared by every screen.
 *
 * Everything here is pure; language-dependent text goes through `t()`.
 */

import { t, getLang } from '../i18n';
import type { Agent, SfoEvent } from '../state/store';

export function toDate(ts: unknown): Date | null {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return null;
  return new Date(n < 1e12 ? n * 1000 : n);
}

const pad2 = (n: number): string => String(n).padStart(2, '0');

/** HH:MM */
export function fmtClock(ts: number | undefined | null = Date.now()): string {
  const date = toDate(ts);
  if (!date) return '--:--';
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** HH:MM:SS */
export function fmtTime(ts: unknown): string {
  const date = toDate(ts);
  if (!date) return '--:--:--';
  return `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}

/** YYYY-MM-DD HH:MM */
export function fmtDateTime(ts: unknown): string {
  const date = toDate(ts);
  if (!date) return '—';
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} `
    + `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

/** Verbose relative time in the active UI language. */
export function ago(ts: unknown, now = Date.now()): string {
  const date = toDate(ts);
  if (!date) return '—';
  const english = getLang() === 'en';
  const seconds = Math.max(0, Math.round((now - date.getTime()) / 1000));
  if (seconds < 10) return english ? 'just now' : '刚刚';
  if (seconds < 60) return english ? `${seconds}s ago` : `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return english ? `${minutes}m ago` : `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return english ? `${hours}h ago` : `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return english ? `${days}d ago` : `${days} 天前`;
  return fmtDateTime(date.getTime());
}

/** Compact relative time for dense rows. */
export function agoShort(ts: unknown, now = Date.now()): string {
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

/** TTL countdown: mm:ss plus urgency flags. */
export function fmtCountdown(targetTs: unknown, now = Date.now()): { text: string; urgent: boolean; expired: boolean } {
  const date = toDate(targetTs);
  if (!date) return { text: '—', urgent: false, expired: false };
  const left = Math.max(0, Math.round((date.getTime() - now) / 1000));
  return {
    text: `${pad2(Math.floor(left / 60))}:${pad2(left % 60)}`,
    urgent: left <= 60,
    expired: left <= 0,
  };
}

/** Uptime seconds → "3 小时 12 分" / "3h 12m". */
export function fmtUptime(seconds: unknown): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const english = getLang() === 'en';
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return english ? `${days}d ${hours}h` : `${days} 天 ${hours} 小时`;
  if (hours > 0) return english ? `${hours}h ${minutes}m` : `${hours} 小时 ${minutes} 分`;
  if (minutes > 0) return english ? `${minutes}m` : `${minutes} 分钟`;
  return english ? `${total}s` : `${total} 秒`;
}

/** Truncate a long single-line string. */
export function clip(text: unknown, max = 120): string {
  const value = String(text ?? '').replace(/\s+/g, ' ').trim();
  return value.length > max ? `${value.slice(0, Math.max(0, max - 1))}…` : value;
}

/* ------------------------------------------------------------------ */
/* domain labels                                                       */
/* ------------------------------------------------------------------ */

export function statusLabel(status: unknown): string {
  const key = String(status || '');
  return t(`status.${key}`, {}, key || '—');
}

export function roleLabel(role: unknown): string {
  const key = String(role || '');
  return t(`role.${key}`, {}, key || '—');
}

export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';

const STATUS_TONES: Record<string, Tone> = {
  active: 'info',
  doing: 'info',
  working: 'info',
  planning: 'info',
  review: 'warning',
  pending: 'warning',
  waiting: 'warning',
  degraded: 'warning',
  suspect: 'warning',
  blocked: 'danger',
  failed: 'danger',
  dead: 'danger',
  denied: 'danger',
  expired: 'neutral',
  canceled: 'neutral',
  archived: 'neutral',
  todo: 'neutral',
  idle: 'neutral',
  offline: 'neutral',
  done: 'success',
  approved: 'success',
  online: 'success',
  ready: 'success',
  live: 'success',
  running: 'success',
};

export function statusTone(status: unknown): Tone {
  return STATUS_TONES[String(status || '')] || 'neutral';
}

export function riskTone(risk: unknown): Tone {
  switch (String(risk || '').toLowerCase()) {
    case 'high':
    case 'critical':
      return 'danger';
    case 'medium':
      return 'warning';
    case 'low':
      return 'success';
    default:
      return 'neutral';
  }
}

/** Readable one-line summary of an event's data. */
export function eventSummary(evt: SfoEvent): string {
  const data = (evt && evt.data) || {};
  const type = String((evt && evt.type) || '');
  const head = type.split('.')[0];
  const parts: string[] = [];
  const push = (value: unknown): void => {
    if (value === undefined || value === null) return;
    // Event payloads carry objects (patrol reports, scopes).  Never let them
    // stringify into "[object Object]" in the ticker.
    const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
    if (text.trim() !== '') parts.push(clip(text, 140));
  };

  if (head === 'chat') {
    if (data.text) push(data.text);
    else if (data.delta) push(data.delta);
    else if (data.typing) push(t('event.thinking'));
    else if (data.message_id) push(String(data.message_id));
  } else if (type.startsWith('agent.chat.')) {
    if (type === 'agent.chat.tool') {
      push(data.tool || t('event.tool'));
      push(data.state || '');
      if (data.error) push(data.error);
    } else if (type === 'agent.chat.message') {
      push(data.text);
    } else if (type === 'agent.chat.state') {
      push(data.busy ? t('event.agent_busy') : t('event.agent_idle'));
    } else if (type === 'agent.chat.thread') {
      push(t('event.thread_cleared'));
    } else if (data.typing) {
      push(t('event.agent_thinking'));
    } else if (data.delta) {
      push(data.delta);
    }
  } else if (head === 'project') {
    push(data.name || data.title);
    if (data.status) push(statusLabel(data.status));
    if (data.project_id && !data.name) push(data.project_id);
    if (data.by) push(t('event.by', { who: data.by }));
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
    if (data.risk) push(t('event.risk', { risk: data.risk }));
    if (data.principal) push(data.principal);
    if (data.decision) push(data.decision === 'allow' ? t('event.allowed') : t('event.denied'));
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
  } else if (head === 'autonomy') {
    push(data.headline || data.summary || data.message || data.reason);
    if (data.findings !== undefined && typeof data.findings === 'number') push(t('event.findings', { n: data.findings }));
    if (data.project_id) push(data.project_id);
  }

  if (!parts.length) {
    const source = data.summary || data.message || data.id || data.seq;
    if (source) push(source);
  }
  return parts.length ? parts.join(' · ') : type;
}

/** Event type → tone, used for the dot next to a row. */
export function toneForType(type: unknown): Tone {
  const head = String(type || '').split('.')[0];
  switch (head) {
    case 'approval':
    case 'failure':
      return 'danger';
    case 'task':
    case 'rule':
      return 'warning';
    case 'chat':
    case 'agent':
      return 'info';
    case 'project':
    case 'unit':
      return 'success';
    default:
      return 'neutral';
  }
}

/* ------------------------------------------------------------------ */
/* avatars                                                             */
/* ------------------------------------------------------------------ */

const AVATAR_COLORS = ['#9a80ff', '#5bc0ff', '#3ddc97', '#ffb454', '#ff7ea0', '#57d6d0', '#c19dff', '#7ea6ff'];

function hashString(text: string): number {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
  return hash;
}

export function colorFor(seed: unknown): string {
  return AVATAR_COLORS[hashString(String(seed || 'x')) % AVATAR_COLORS.length];
}

export function initialsFor(agent: Agent | string | null | undefined): string {
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
