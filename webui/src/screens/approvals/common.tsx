/**
 * common.tsx — small, screen-local helpers for the approvals screen.
 *
 * Kept inside `screens/approvals/` on purpose: screens may not edit shared
 * files, so a helper that only this screen needs lives here.
 */

import { useEffect, useState, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import type { Tone } from '../../ui/format';
import { t } from '../../i18n';

export type BadgeVariant = 'neutral' | 'info' | 'success' | 'warning' | 'error';

/** `ui/format.ts` Tone → Astryx Badge variant (`danger` is spelled `error`). */
export function badgeVariant(tone: Tone): BadgeVariant {
  switch (tone) {
    case 'danger': return 'error';
    case 'info': return 'info';
    case 'success': return 'success';
    case 'warning': return 'warning';
    default: return 'neutral';
  }
}

export function riskLabel(risk: unknown): string {
  const key = String(risk || 'low').toLowerCase();
  return t(`approvals.risk.${key}`, {}, String(risk || 'low'));
}

/** Risk pill: colour plus literal risk text (colour alone never carries meaning). */
export function RiskBadge({ risk }: { risk: unknown }): ReactNode {
  const value = String(risk || 'low').toLowerCase();
  const variant: BadgeVariant = value === 'high' || value === 'critical' ? 'error'
    : value === 'medium' ? 'warning'
      : value === 'low' ? 'success' : 'neutral';
  return <Badge label={riskLabel(value)} variant={variant} />;
}

/** A live clock so TTL countdowns keep ticking without a per-row timer. */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return now;
}

/** JSON text for CodeBlock: strings stay as-is, structured previews are pretty-printed. */
export function previewText(value: unknown, fallback = ''): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export function scopeText(scope: unknown): string {
  if (!scope || typeof scope !== 'object' || Array.isArray(scope)) return '{}';
  try {
    return JSON.stringify(scope, null, 2);
  } catch {
    return String(scope);
  }
}

/** One-line scope summary for dense tables. */
export function scopeInline(scope: unknown): string {
  if (!scope || typeof scope !== 'object' || Array.isArray(scope)) return '—';
  const parts = Object.entries(scope as Record<string, unknown>)
    .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : String(value)}`);
  return parts.length ? parts.join(' · ') : '—';
}

export function modeLabel(mode: unknown): string {
  const key = String(mode || '');
  return key ? t(`approvals.mode.${key}`, {}, key) : '—';
}
