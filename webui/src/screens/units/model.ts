/**
 * model.ts — pure helpers shared by the Units screen and its detail drawer.
 *
 * The backend has carried several spellings over time (`work_id` vs
 * `unit_id`, `status` vs `state`, a derived tier vs an explicit one), so the
 * translation lives here instead of being repeated in JSX.
 */

import { t } from '../../i18n';
import { eventSummary, statusLabel, statusTone, type Tone } from '../../ui/format';
import type { Project, SfoEvent, Unit } from '../../state/store';

/** A unit snapshot or descriptor; detail responses add the richer fields. */
export type UnitView = Unit & {
  unit_id?: string;
  state?: string;
  tier?: string;
  reason?: string;
  activity?: string;
  critical?: boolean;
  supervisor?: string;
  since?: number;
  restart_count?: number;
  actions?: string[];
  runtime?: { mode?: string; host?: string; host_unit?: string; pid?: number };
  lease?: {
    type?: string;
    host_unit?: string;
    ttl_s?: number;
    renewed_at?: number;
    expires_at?: number;
  };
  provides?: Record<string, unknown>;
  endpoints?: Array<{ name?: string; transport?: string; address?: string }>;
  health?: { ok?: boolean; last_latency_ms?: number };
  dependency?: {
    nodes?: Array<{ id?: string; label?: string }>;
    edges?: Array<{ from?: string; to?: string; kind?: string }>;
  };
};

export type DetailState = 'loading' | 'ready' | 'unavailable' | 'forbidden' | 'error';

export const TIERS = ['system', 'service', 'worker'] as const;
export type Tier = (typeof TIERS)[number];

export const ONLINE = new Set(['online', 'ready', 'running', 'active']);
export const PROBLEM = new Set([
  'degraded',
  'failed',
  'unreachable',
  'offline',
  'quarantined',
  'suspect',
  'blocked',
  'dead',
]);

const SYSTEM_KINDS = new Set(['sys', 'store', 'scheduler', 'gateway', 'context']);
const SERVICE_KINDS = new Set(['approval', 'llm', 'tool', 'skill', 'plugin', 'mcp']);

export function asUnitView(unit: Unit | null | undefined): UnitView | null {
  return (unit as UnitView | null | undefined) ?? null;
}

export function unitId(unit: Unit | null | undefined): string {
  if (!unit) return '';
  return String((unit as UnitView).unit_id || unit.work_id || '');
}

export function unitState(unit: Unit | null | undefined): string {
  if (!unit) return 'unknown';
  const view = unit as UnitView;
  return String(view.state || unit.status || 'unknown');
}

export function tierOf(unit: Unit | null | undefined): Tier {
  if (!unit) return 'worker';
  const explicit = String((unit as UnitView).tier || '');
  if ((TIERS as readonly string[]).includes(explicit)) return explicit as Tier;
  const kind = String(unit.kind || '');
  if (kind === 'sys' || SYSTEM_KINDS.has(kind)) return 'system';
  if (SERVICE_KINDS.has(kind)) return 'service';
  return 'worker';
}

export function isOnline(unit: Unit | null | undefined): boolean {
  return ONLINE.has(unitState(unit));
}

export function isProblem(unit: Unit | null | undefined): boolean {
  return PROBLEM.has(unitState(unit));
}

export function abilityNames(unit: Unit | null | undefined): string[] {
  const abilities = Array.isArray(unit?.abilities) ? unit.abilities : [];
  return abilities
    .map((ability) => {
      if (typeof ability === 'string') return ability;
      if (ability && typeof ability === 'object') {
        const record = ability as Record<string, unknown>;
        return String(record.name || record.tool || '');
      }
      return '';
    })
    .filter(Boolean);
}

export function runtimeText(unit: Unit | null | undefined): string {
  if (!unit) return '—';
  const view = unit as UnitView;
  const runtime = view.runtime || {};
  const mode = String(runtime.mode || (unit.pid != null ? 'subprocess' : 'in_process'));
  if (mode === 'remote') return String(runtime.host || unit.host || t('units.runtime.remote'));
  if (mode === 'in_process' || mode === 'declarative') {
    if (runtime.host_unit) return t('units.runtime.host', { host: runtime.host_unit });
    return t(`units.runtime.${mode}`, {}, t('units.runtime.in_process'));
  }
  const bits = [t('units.runtime.subprocess')];
  const pid = Number(runtime.pid ?? unit.pid);
  if (Number.isFinite(pid) && pid > 0) bits.push(t('units.runtime.pid', { pid }));
  return bits.join(' · ');
}

export function leaseText(unit: Unit | null | undefined): string {
  const lease = (unit as UnitView | null)?.lease;
  if (!lease || !lease.type) {
    return tierOf(unit) === 'system' ? t('units.lease.host') : t('units.lease.none');
  }
  const label = t(`units.lease.${lease.type}`, {}, lease.type);
  const ttl = Number(lease.ttl_s);
  return ttl > 0 ? `${label} · ${t('units.lease.ttl', { s: ttl })}` : label;
}

export function actionLabel(action: string): string {
  return t(`units.actions.${action}`, {}, action);
}

export function actionNeedsConfirmation(action: string): boolean {
  return action !== 'probe';
}

const DOT_VARIANTS = {
  success: 'success',
  warning: 'warning',
  danger: 'error',
  info: 'accent',
  neutral: 'neutral',
} as const;

export function dotVariant(tone: Tone): 'success' | 'warning' | 'error' | 'accent' | 'neutral' {
  return DOT_VARIANTS[tone] || 'neutral';
}

const TOKEN_COLORS = {
  success: 'green',
  warning: 'yellow',
  danger: 'red',
  info: 'blue',
  neutral: 'gray',
} as const;

export function tokenColor(tone: Tone): 'green' | 'yellow' | 'red' | 'blue' | 'gray' {
  return TOKEN_COLORS[tone] || 'gray';
}

export function unitTone(unit: Unit | null | undefined): Tone {
  return statusTone(unitState(unit));
}

export function unitStatusLabel(unit: Unit | null | undefined): string {
  return statusLabel(unitState(unit));
}

export function projectName(projects: Project[], projectId: unknown): string {
  const id = String(projectId || '');
  if (!id) return '—';
  return projects.find((project) => project.id === id)?.name || id;
}

export function matchesQuery(unit: Unit, query: string): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  const view = unit as UnitView;
  const metadata = (unit.metadata && typeof unit.metadata === 'object')
    ? (unit.metadata as Record<string, unknown>)
    : {};
  const haystack = [
    unitId(unit),
    unit.label,
    unit.kind,
    unit.project_id,
    unit.host,
    unit.transport,
    metadata.role,
    metadata.description,
    view.reason,
    ...abilityNames(unit),
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
  return haystack.includes(needle);
}

/** One line describing a unit-scoped event in the detail drawer. */
export function unitEventLine(event: SfoEvent): string {
  const data = (event.data || {}) as Record<string, unknown>;
  const parts: string[] = [];
  if (data.from || data.to) {
    parts.push(`${String(data.from || '?')} → ${String(data.to || '?')}`);
  } else {
    const state = data.state || data.status;
    if (state) parts.push(statusLabel(state));
  }
  if (data.reason) parts.push(`${t('units.detail.reason')}: ${String(data.reason)}`);
  if (!parts.length) return eventSummary(event);
  return parts.join(' · ');
}

/* ------------------------------------------------------------------ */
/* summary payload                                                     */
/* ------------------------------------------------------------------ */

export interface UnitSummary {
  total?: number;
  online?: number;
  degraded?: number;
  failed?: number;
  critical_bad?: boolean;
  by_tier?: Record<string, { total?: number; online?: number } | undefined>;
  problems?: unknown[];
}

export function normalizeSummary(data: unknown): UnitSummary | null {
  if (!data || typeof data !== 'object') return null;
  return data as UnitSummary;
}

export function findUnit(units: Unit[], id: string): UnitView | null {
  const target = String(id || '');
  if (!target) return null;
  return asUnitView(units.find((unit) => unitId(unit) === target));
}
