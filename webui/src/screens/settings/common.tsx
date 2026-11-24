/**
 * common.tsx — shared building blocks for the Settings screen.
 *
 * The screen is split into four panes; each pane loads its own data and
 * degrades on its own (missing endpoint → "not enabled", 403 → "forbidden",
 * anything else → inline error with retry).  Everything visual comes from
 * Astryx primitives and theme tokens — no raw layout divs, no hex colors.
 */

import type { ReactNode } from 'react';
import { Banner } from '@astryxdesign/core/Banner';
import { Button } from '@astryxdesign/core/Button';
import { Heading, Text } from '@astryxdesign/core/Text';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Section } from '@astryxdesign/core/Section';
import { Spinner } from '@astryxdesign/core/Spinner';
import { api, isForbidden, isUnsupported, type Json } from '../../api/client';
import { t, useT } from '../../i18n';
import { applyConfig, useStore, type SfoConfig, type SfoState } from '../../state/store';

/* ------------------------------------------------------------------ */
/* store selectors (module-level: never recreate per render)           */
/* ------------------------------------------------------------------ */

export const selConfig = (s: SfoState): SfoConfig | null => s.config;
export const selSys = (s: SfoState): SfoState['sys'] => s.sys;
export const selStats = (s: SfoState): SfoState['stats'] => s.stats;

export function useConfig(): SfoConfig | null {
  return useStore(selConfig);
}

/* ------------------------------------------------------------------ */
/* errors                                                             */
/* ------------------------------------------------------------------ */

export type ErrorKind = 'unavailable' | 'forbidden' | 'error';

export function errorKind(error: unknown): ErrorKind {
  if (isUnsupported(error)) return 'unavailable';
  if (isForbidden(error)) return 'forbidden';
  return 'error';
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error || t('settings.unknown'));
}

interface LoadErrorProps {
  error: unknown;
  unavailableKey: string;
  forbiddenKey?: string;
  errorKey: string;
  onRetry?: () => void;
}

/** One calm, translated notice for any failed panel load. */
export function LoadError({ error, unavailableKey, forbiddenKey, errorKey, onRetry }: LoadErrorProps): ReactNode {
  const tr = useT();
  const kind = errorKind(error);
  if (kind === 'unavailable') {
    return <Banner status="info" title={tr(unavailableKey)} />;
  }
  if (kind === 'forbidden') {
    return <Banner status="warning" title={tr(forbiddenKey || 'settings.forbidden')} />;
  }
  return (
    <Banner
      status="error"
      title={tr(errorKey, { message: errorMessage(error) })}
      endContent={onRetry ? (
        <Button label={tr('settings.retry')} size="sm" variant="secondary" onClick={onRetry} />
      ) : undefined}
    />
  );
}

export function LoadingLine({ label }: { label: string }): ReactNode {
  return (
    <HStack gap={2} align="center">
      <Spinner size="sm" />
      <Text size="sm" color="secondary">{label}</Text>
    </HStack>
  );
}

/* ------------------------------------------------------------------ */
/* sections                                                            */
/* ------------------------------------------------------------------ */

interface PaneSectionProps {
  title: string;
  hint?: string;
  actions?: ReactNode;
  children: ReactNode;
}

/** Standard settings section: title + hint + optional actions + content. */
export function PaneSection({ title, hint, actions, children }: PaneSectionProps): ReactNode {
  return (
    <Section padding={4}>
      <VStack gap={3}>
        <HStack justify="between" align="start" gap={3} wrap="wrap">
          <VStack gap={0.5}>
            <Heading level={3}>{title}</Heading>
            {hint ? <Text size="sm" color="secondary">{hint}</Text> : null}
          </VStack>
          {actions ? <HStack gap={2} align="center" wrap="wrap">{actions}</HStack> : null}
        </HStack>
        {children}
      </VStack>
    </Section>
  );
}

export function ViewerNote(): ReactNode {
  const tr = useT();
  return <Banner status="info" title={tr('settings.viewer_hint')} />;
}

/* ------------------------------------------------------------------ */
/* config helpers                                                     */
/* ------------------------------------------------------------------ */

export interface SaveResult {
  restartRequired: string[];
}

/**
 * Persist one config patch.  The server answers with the new public config;
 * when an older backend omits it we refetch `/api/config` so the store never
 * drifts away from the server.
 */
export async function submitConfigPatch(patch: Json): Promise<SaveResult> {
  const raw = await api.patchConfig(patch);
  const data = (raw && typeof raw === 'object' ? raw : {}) as {
    config?: SfoConfig;
    restart_required?: string[];
  };
  if (data.config && typeof data.config === 'object') {
    applyConfig(data.config);
  } else {
    applyConfig((await api.getConfig()) as SfoConfig);
  }
  return { restartRequired: Array.isArray(data.restart_required) ? data.restart_required : [] };
}

/** Read `llm.routes` / `llm.fallback` style JSON arrays as editable text. */
export function jsonText(value: unknown): string {
  if (value === undefined || value === null) return '[]';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return '[]';
  }
}

/** Sentinel thrown when a JSON document parses but is not an array. */
export const ARRAY_EXPECTED = 'SFO_ARRAY_EXPECTED';

/** Parse a JSON-array textarea; callers translate `ARRAY_EXPECTED`. */
export function parseJsonArray(source: string): unknown[] {
  const text = source.trim();
  if (!text) return [];
  const parsed: unknown = JSON.parse(text);
  if (!Array.isArray(parsed)) throw new Error(ARRAY_EXPECTED);
  return parsed;
}

export function sameJson(a: unknown, b: unknown): boolean {
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}

/* ------------------------------------------------------------------ */
/* formatting                                                         */
/* ------------------------------------------------------------------ */

export function fmtInt(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('en-US');
}

export function fmtCost(value: unknown): string {
  if (value === undefined || value === null || value === '') return '—';
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  if (n === 0) return '$0.00';
  const digits = n < 0.01 ? 5 : n < 1 ? 3 : 2;
  return `$${n.toFixed(digits)}`;
}

/** Human label for a restart-required config path. */
export function restartFieldLabel(path: string): string {
  switch (path) {
    case 'transport.mode':
      return `${t('settings.general.transport')} · mode`;
    case 'web.port':
      return `${t('settings.security.web.title')} · port`;
    default:
      return path;
  }
}
