/**
 * common.tsx — screen-local helpers for the autonomy console.
 *
 * Normalises the two record shapes in use (the hub's `autonomy/service.py`
 * payload and the UI fixture, which is a little looser), formats the live
 * countdown and builds the partial policy patch the backend expects.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import type { Json } from '../../api/client';
import { t } from '../../i18n';
import type {
  AutonomyPolicy,
  DispatchRecord,
  Finding,
  GuardianRecord,
  PatrolReport,
  PolicyDraft,
} from './types';

export type BadgeVariant = 'neutral' | 'info' | 'success' | 'warning' | 'error';
export type DotVariant = 'neutral' | 'accent' | 'success' | 'warning' | 'error';

/* ------------------------------------------------------------------ */
/* severity / decision → Astryx colour                                 */
/* ------------------------------------------------------------------ */

export function severityBadge(severity: unknown): BadgeVariant {
  switch (String(severity || 'info')) {
    case 'critical': return 'error';
    case 'warn': return 'warning';
    default: return 'info';
  }
}

export function severityDot(severity: unknown): DotVariant {
  switch (String(severity || 'info')) {
    case 'critical': return 'error';
    case 'warn': return 'warning';
    default: return 'accent';
  }
}

export function SeverityDot({ severity }: { severity: unknown }): ReactNode {
  const key = String(severity || 'info');
  return (
    <StatusDot
      variant={severityDot(key)}
      label={t(`autonomy.severity.${key}`, {}, key)}
      tooltip={t(`autonomy.severity.${key}`, {}, key)}
    />
  );
}

export function SeverityBadge({ severity }: { severity: unknown }): ReactNode {
  const key = String(severity || 'info');
  return (
    <Badge
      label={t(`autonomy.severity.${key}`, {}, key)}
      variant={severityBadge(key)}
    />
  );
}

export function riskBadgeVariant(risk: unknown): BadgeVariant {
  switch (String(risk || 'low').toLowerCase()) {
    case 'high':
    case 'critical': return 'error';
    case 'medium': return 'warning';
    case 'low': return 'success';
    default: return 'neutral';
  }
}

export function RiskBadge({ risk }: { risk: unknown }): ReactNode {
  const key = String(risk || 'low').toLowerCase();
  return <Badge label={t(`autonomy.risk.${key}`, {}, key)} variant={riskBadgeVariant(key)} />;
}

const DECISION_VARIANTS: Record<string, BadgeVariant> = {
  allow: 'success',
  observe: 'warning',
  skip: 'neutral',
};

export function DecisionBadge({ decision }: { decision: unknown }): ReactNode {
  const key = String(decision || 'skip');
  return (
    <Badge
      label={t(`autonomy.approval.decision.${key}`, {}, key)}
      variant={DECISION_VARIANTS[key] || 'neutral'}
    />
  );
}

/* ------------------------------------------------------------------ */
/* normalisers (hub payload + fixture payload)                         */
/* ------------------------------------------------------------------ */

export function findingCheck(finding: Finding): string {
  return String(finding.check || finding.kind || finding.fingerprint || '—');
}

export function findingFingerprint(finding: Finding): string {
  return String(finding.fingerprint || finding.id || '');
}

export function findingDispatchable(finding: Finding): boolean {
  if (finding.dispatchable) return true;
  if (finding.dispatch && finding.dispatch.kind) return true;
  return finding.suggested === 'dispatch';
}

export function dispatchAction(record: DispatchRecord): string {
  const action = String(record.action || '');
  if (action) return action;
  const kind = String(record.kind || '');
  if (kind === 'dispatch') return record.task_id ? 'task' : 'spawn';
  return kind || 'task';
}

export function dispatchStatus(record: DispatchRecord): string {
  const status = String(record.status || '');
  if (status) return status;
  if (record.ok === true) return 'ok';
  if (record.ok === false) return 'error';
  return 'skipped';
}

export function guardianRequestId(record: GuardianRecord): string {
  return String(record.request_id || record.approval_id || '—');
}

export function patrolCount(report: PatrolReport | null | undefined, counts: Record<string, unknown> | undefined): number {
  const fromCounts = Number(counts?.patrols);
  if (Number.isFinite(fromCounts) && fromCounts > 0) return fromCounts;
  return Number(report?.seq ?? 0) || 0;
}

/* ------------------------------------------------------------------ */
/* countdown                                                           */
/* ------------------------------------------------------------------ */

export function formatRemaining(seconds: number): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const pad = (value: number): string => String(value).padStart(2, '0');
  return hours > 0 ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${pad(minutes)}:${pad(secs)}`;
}

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** Epoch-ms of the next patrol, derived from the relative server answer. */
export function nextRunAt(status: { enabled?: boolean; next_run_in_s?: number | null } | null, loadedAt: number): number {
  if (!status || !status.enabled) return 0;
  const seconds = Number(status.next_run_in_s);
  if (!Number.isFinite(seconds)) return 0;
  return loadedAt + Math.max(0, seconds) * 1000;
}

/* ------------------------------------------------------------------ */
/* policy draft / patch                                                */
/* ------------------------------------------------------------------ */

export function policyDraft(policy: AutonomyPolicy | undefined | null): PolicyDraft {
  const source = policy || {};
  return {
    enabled: Boolean(source.enabled),
    interval_s: Number(source.interval_s ?? 120),
    dispatch_mode: String(source.dispatch_mode || 'off'),
    approve_mode: String(source.approve_mode || 'off'),
    approve_risk_max: String(source.approve_risk_max || 'medium'),
    approve_capabilities: Array.isArray(source.approve_capabilities)
      ? source.approve_capabilities.map(String) : [],
    approve_shell: Boolean(source.approve_shell),
    approve_shell_patterns: Array.isArray(source.approve_shell_patterns)
      ? source.approve_shell_patterns.map(String) : [],
    approve_domains: Array.isArray(source.approve_domains)
      ? source.approve_domains.map(String) : [],
    dispatch_max_agents: Number(source.dispatch_max_agents ?? 2),
    dispatch_max_tasks_per_hour: Number(source.dispatch_max_tasks_per_hour ?? 10),
    dispatch_max_spawns_per_hour: Number(source.dispatch_max_spawns_per_hour ?? 4),
    approve_max_per_hour: Number(source.approve_max_per_hour ?? 20),
  };
}

function sameList(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((value, index) => value === b[index]);
}

/**
 * Build a partial autonomy patch: only fields whose draft differs from the
 * server policy.  Never round-trips the whole `AutonomyCfg`.
 */
export function policyPatch(draft: PolicyDraft, policy: AutonomyPolicy | undefined | null): Json {
  const base = policyDraft(policy);
  const patch: Json = {};
  if (draft.enabled !== base.enabled) patch.enabled = draft.enabled;
  if (draft.interval_s !== base.interval_s) patch.interval_s = draft.interval_s;
  if (draft.dispatch_mode !== base.dispatch_mode) patch.dispatch_mode = draft.dispatch_mode;
  if (draft.approve_mode !== base.approve_mode) patch.approve_mode = draft.approve_mode;
  if (draft.approve_risk_max !== base.approve_risk_max) patch.approve_risk_max = draft.approve_risk_max;
  if (!sameList(draft.approve_capabilities, base.approve_capabilities)) {
    patch.approve_capabilities = [...draft.approve_capabilities];
  }
  if (draft.approve_shell !== base.approve_shell) patch.approve_shell = draft.approve_shell;
  if (!sameList(draft.approve_shell_patterns, base.approve_shell_patterns)) {
    patch.approve_shell_patterns = [...draft.approve_shell_patterns];
  }
  if (!sameList(draft.approve_domains, base.approve_domains)) {
    patch.approve_domains = [...draft.approve_domains];
  }
  if (draft.dispatch_max_agents !== base.dispatch_max_agents) patch.dispatch_max_agents = draft.dispatch_max_agents;
  if (draft.dispatch_max_tasks_per_hour !== base.dispatch_max_tasks_per_hour) {
    patch.dispatch_max_tasks_per_hour = draft.dispatch_max_tasks_per_hour;
  }
  if (draft.dispatch_max_spawns_per_hour !== base.dispatch_max_spawns_per_hour) {
    patch.dispatch_max_spawns_per_hour = draft.dispatch_max_spawns_per_hour;
  }
  if (draft.approve_max_per_hour !== base.approve_max_per_hour) {
    patch.approve_max_per_hour = draft.approve_max_per_hour;
  }
  return patch;
}

export function hasPatch(patch: Json): boolean {
  return Object.keys(patch).length > 0;
}

export function modeLabel(value: unknown): string {
  const key = String(value || 'off');
  return t(`autonomy.mode.${key}`, {}, key);
}

export function riskMaxLabel(value: unknown): string {
  const key = String(value || 'medium');
  return t(`autonomy.risk.${key}`, {}, key);
}

export function capabilityLabel(capability: string): string {
  return `${t(`autonomy.cap.${capability}`, {}, capability)} · ${capability}`;
}

export function triggerLabel(trigger: unknown): string {
  const key = String(trigger || 'timer');
  return t(`autonomy.trigger.${key}`, {}, key);
}

/* ------------------------------------------------------------------ */
/* policy guards (fail with a human hint before the backend 400)       */
/* ------------------------------------------------------------------ */

export function policyGuard(draft: PolicyDraft, patch: Json): string {
  const capabilities = Array.isArray(patch.approve_capabilities)
    ? (patch.approve_capabilities as string[])
    : draft.approve_capabilities;
  const shell = Object.prototype.hasOwnProperty.call(patch, 'approve_shell')
    ? Boolean(patch.approve_shell)
    : draft.approve_shell;
  const patterns = Array.isArray(patch.approve_shell_patterns)
    ? (patch.approve_shell_patterns as string[])
    : draft.approve_shell_patterns;
  const domains = Array.isArray(patch.approve_domains)
    ? (patch.approve_domains as string[])
    : draft.approve_domains;
  const risk = Object.prototype.hasOwnProperty.call(patch, 'approve_risk_max')
    ? String(patch.approve_risk_max)
    : draft.approve_risk_max;

  const hasShell = capabilities.includes('shell.exec');
  const hasNet = capabilities.includes('net.fetch');
  if (hasShell) {
    if (!shell || patterns.length === 0 || risk !== 'high') return t('autonomy.policy.invalid_shell');
  }
  if (shell && !hasShell) return t('autonomy.policy.invalid_shell');
  if (hasNet && domains.length === 0) return t('autonomy.policy.invalid_net');
  return '';
}

/** True when the draft differs from the server policy. */
export function isDirty(draft: PolicyDraft, policy: AutonomyPolicy | undefined | null): boolean {
  return hasPatch(policyPatch(draft, policy));
}

