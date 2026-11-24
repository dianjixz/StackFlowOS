/**
 * ui.tsx — small presentation helpers shared by the Projects screens.
 *
 * These wrap Astryx primitives so the list, board, graph and panels agree on
 * how a status, a section heading and a hint look.
 */

import type { ReactNode } from 'react';
import { Badge, type BadgeVariant } from '@astryxdesign/core/Badge';
import { StatusDot } from '@astryxdesign/core/StatusDot';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Heading, Text } from '@astryxdesign/core/Text';
import { statusLabel, statusTone, type Tone } from '../../ui/format';

export type DotVariant = 'success' | 'warning' | 'error' | 'accent' | 'neutral';

const TONE_BADGE: Record<Tone, BadgeVariant> = {
  neutral: 'neutral',
  info: 'info',
  success: 'success',
  warning: 'warning',
  danger: 'error',
};

const TONE_DOT: Record<Tone, DotVariant> = {
  neutral: 'neutral',
  info: 'accent',
  success: 'success',
  warning: 'warning',
  danger: 'error',
};

export function badgeVariantForTone(tone: Tone): BadgeVariant {
  return TONE_BADGE[tone] || 'neutral';
}

export function dotVariantForTone(tone: Tone): DotVariant {
  return TONE_DOT[tone] || 'neutral';
}

export function badgeVariantForStatus(status: unknown): BadgeVariant {
  return badgeVariantForTone(statusTone(status));
}

export function dotVariantForStatus(status: unknown): DotVariant {
  return dotVariantForTone(statusTone(status));
}

/** A status pill: label in the active language, colour from the shared tone map. */
export function StatusBadge({ status, label }: { status: unknown; label?: string }): ReactNode {
  const key = String(status || '');
  return <Badge variant={badgeVariantForStatus(key)} label={label ?? statusLabel(key)} />;
}

/** Status dot + label, safe for dark and light themes. */
export function StatusDotLabel({
  status,
  label,
  isPulsing,
}: {
  status: unknown;
  label?: string;
  isPulsing?: boolean;
}): ReactNode {
  const text = label ?? statusLabel(status);
  return (
    <HStack gap={1.5} align="center">
      <StatusDot variant={dotVariantForStatus(status)} label={text} isPulsing={isPulsing} />
      <Text size="xsm" color="secondary">{text}</Text>
    </HStack>
  );
}

/** Section heading + optional one-line hint + optional trailing action. */
export function SectionHead({
  title,
  hint,
  action,
  level = 3,
}: {
  title: ReactNode;
  hint?: ReactNode;
  action?: ReactNode;
  level?: 2 | 3 | 4;
}): ReactNode {
  return (
    <HStack gap={3} align="center" justify="between" wrap="wrap">
      <VStack gap={0.5}>
        <Heading level={level}>{title}</Heading>
        {hint ? <Text size="xsm" color="secondary">{hint}</Text> : null}
      </VStack>
      {action ? <HStack gap={2} align="center" wrap="wrap">{action}</HStack> : null}
    </HStack>
  );
}

/** Calm inline note (unsupported endpoint, empty panel, viewer hint). */
export function Hint({ children, isWarning = false }: { children: ReactNode; isWarning?: boolean }): ReactNode {
  return (
    <Text size="sm" color={isWarning ? 'primary' : 'secondary'} display="block">
      {children}
    </Text>
  );
}

/** One label/value pair in a horizontal stat strip. */
export function Stat({
  label,
  value,
  tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  tone?: Tone;
}): ReactNode {
  const color = tone === 'neutral' ? 'primary' : 'accent';
  return (
    <VStack gap={0.5}>
      <Text size="2xs" color="secondary">{label}</Text>
      <Heading level={4} color={color}>{value}</Heading>
    </VStack>
  );
}
