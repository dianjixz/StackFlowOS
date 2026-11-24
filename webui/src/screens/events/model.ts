/**
 * events/model.ts — pure helpers for the Events screen.
 *
 * The store keeps one canonical ascending event list; this module adapts each
 * record into a table row (project id resolved from the several places the
 * backend has carried it) and holds the client-side filtering predicates.
 */

import { t } from '../../i18n';
import { eventSummary, type Tone } from '../../ui/format';
import type { Project, SfoEvent } from '../../state/store';

/** Legacy console filter vocabulary, one segment per event-type head. */
export const EVENT_TYPES = [
  'all',
  'chat',
  'project',
  'task',
  'agent',
  'unit',
  'approval',
  'rule',
  'tool',
  'failure',
  'sys',
] as const;

export type EventTypeFilter = (typeof EVENT_TYPES)[number];

export const LIMITS = [50, 100, 200, 300] as const;

export interface EventRow extends Record<string, unknown> {
  /** Stable table identity (event id, falling back to its seq). */
  key: string;
  seq: number;
  id: string;
  type: string;
  source: string;
  ts: number;
  data: Record<string, any>;
  project_id: string;
}

export function eventProjectId(event: SfoEvent): string {
  const direct = (event as SfoEvent & { project_id?: unknown }).project_id;
  if (direct !== undefined && direct !== null && String(direct).trim() !== '') return String(direct);
  const data = (event.data || {}) as Record<string, any>;
  if (data.project_id) return String(data.project_id);
  const scope = data.scope;
  if (scope && typeof scope === 'object' && (scope as Record<string, unknown>).project_id) {
    return String((scope as Record<string, unknown>).project_id);
  }
  return '';
}

export function toEventRow(event: SfoEvent): EventRow {
  return {
    key: String(event.id || `seq-${event.seq}`),
    seq: Number(event.seq) || 0,
    id: String(event.id || ''),
    type: String(event.type || ''),
    source: String(event.source || t('events.source.unknown')),
    ts: Number(event.ts) || 0,
    data: (event.data && typeof event.data === 'object' ? event.data : {}) as Record<string, any>,
    project_id: eventProjectId(event),
  };
}

export function projectLabel(projects: Project[], projectId: string): string {
  if (!projectId) return '—';
  return projects.find((project) => project.id === projectId)?.name || projectId;
}

export function matchesSearch(row: EventRow, needle: string): boolean {
  if (!needle) return true;
  let raw = '';
  try {
    raw = JSON.stringify(row.data);
  } catch {
    raw = '';
  }
  const haystack = [
    row.type,
    row.source,
    row.id,
    row.project_id,
    raw,
    eventSummary(row as unknown as SfoEvent),
  ].join(' ').toLowerCase();
  return haystack.includes(needle);
}

export function matchesType(row: EventRow, type: EventTypeFilter): boolean {
  if (type === 'all') return true;
  return row.type.split('.')[0] === type;
}

const DOT_VARIANTS: Record<Tone, 'success' | 'warning' | 'error' | 'accent' | 'neutral'> = {
  success: 'success',
  warning: 'warning',
  danger: 'error',
  info: 'accent',
  neutral: 'neutral',
};

export function dotVariant(tone: Tone): 'success' | 'warning' | 'error' | 'accent' | 'neutral' {
  return DOT_VARIANTS[tone] || 'neutral';
}
