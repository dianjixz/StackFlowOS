/**
 * DependencyGraph.tsx — a small, deterministic requires/wants graph.
 *
 * Deliberately not a layout engine: the unit sits on the inline start, one
 * node per declared dependency on the inline end, solid arrows for `requires`
 * and dashed arrows for `wants`. The same information is repeated below the
 * SVG as tokens so the graph is never the only way to read the data.
 */

import { useId, type ReactNode } from 'react';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { Text } from '@astryxdesign/core/Text';
import { Token } from '@astryxdesign/core/Token';
import { t } from '../../i18n';
import { clip } from '../../ui/format';
import type { Unit } from '../../state/store';
import { unitId, type UnitView } from './model';

interface Dependency {
  id: string;
  kind: 'requires' | 'wants';
}

const WIDTH = 560;
const ROW_HEIGHT = 46;
const SELF_W = 168;
const SELF_H = 52;
const DEP_X = 330;
const DEP_W = 218;
const DEP_H = 34;
const TOP = 26;
const BOTTOM = 16;

function dependencyIds(unit: UnitView | null): Dependency[] {
  if (!unit) return [];
  const requires = Array.isArray(unit.requires) ? unit.requires.map(String).filter(Boolean) : [];
  const wants = Array.isArray(unit.wants) ? unit.wants.map(String).filter(Boolean) : [];
  if (requires.length || wants.length) {
    return [
      ...requires.map<Dependency>((id) => ({ id, kind: 'requires' })),
      ...wants.map<Dependency>((id) => ({ id, kind: 'wants' })),
    ];
  }
  // Detail payloads may only carry the resolved graph; fall back to its edges.
  const edges = Array.isArray(unit.dependency?.edges) ? unit.dependency.edges : [];
  const seen = new Set<string>();
  const deps: Dependency[] = [];
  for (const edge of edges) {
    const target = String(edge.to || '');
    const kind = edge.kind === 'wants' ? 'wants' : 'requires';
    const key = `${kind}:${target}`;
    if (!target || seen.has(key)) continue;
    seen.add(key);
    deps.push({ id: target, kind });
  }
  return deps;
}

function resolves(id: string, units: Unit[]): boolean {
  if (id.includes('*')) {
    const prefix = id.slice(0, id.indexOf('*'));
    return units.some((unit) => unitId(unit).startsWith(prefix));
  }
  return units.some((unit) => unitId(unit) === id);
}

function nodeLabel(id: string, unit: UnitView | null): string {
  const nodes = Array.isArray(unit?.dependency?.nodes) ? unit.dependency?.nodes : [];
  const match = nodes?.find((node) => String(node.id || '') === id);
  return String(match?.label || id);
}

export function DependencyGraph({ unit, units }: { unit: UnitView | null; units: Unit[] }): ReactNode {
  const rawId = useId();
  const token = rawId.replace(/[^a-zA-Z0-9_-]/g, '');
  const reqMarker = `unit-dep-req-${token}`;
  const wantMarker = `unit-dep-want-${token}`;
  const reqMuted = `unit-dep-req-muted-${token}`;
  const wantMuted = `unit-dep-want-muted-${token}`;

  const deps = dependencyIds(unit);
  if (!deps.length) {
    return <Text size="sm" color="secondary">{t('units.deps.none')}</Text>;
  }

  const selfId = unitId(unit) || '?';
  const requires = deps.filter((dep) => dep.kind === 'requires');
  const wants = deps.filter((dep) => dep.kind === 'wants');
  const height = Math.max(150, TOP + deps.length * ROW_HEIGHT + BOTTOM);
  const selfX = 12;
  const selfY = height / 2 - SELF_H / 2;
  const edges: ReactNode[] = [];
  const nodes: ReactNode[] = [];

  deps.forEach((dep, index) => {
    const resolved = resolves(dep.id, units);
    const y = TOP + index * ROW_HEIGHT;
    const startX = selfX + SELF_W;
    const startY = selfY + SELF_H / 2;
    const endX = DEP_X - 12;
    const endY = y + DEP_H / 2;
    const requiresKind = dep.kind === 'requires';
    const color = !resolved
      ? 'var(--color-text-disabled)'
      : requiresKind
        ? 'var(--color-text-error)'
        : 'var(--color-text-warning)';
    const marker = !resolved
      ? requiresKind ? reqMuted : wantMuted
      : requiresKind ? reqMarker : wantMarker;

    edges.push(
      <path
        key={`edge-${dep.kind}-${dep.id}`}
        d={`M ${startX} ${startY} C ${startX + 96} ${startY}, ${endX - 96} ${endY}, ${endX} ${endY}`}
        fill="none"
        stroke={color}
        strokeWidth={1.4}
        strokeDasharray={requiresKind ? undefined : '5 4'}
        markerEnd={`url(#${marker})`}
        opacity={resolved ? 0.92 : 0.6}
      />,
    );

    nodes.push(
      <g key={`node-${dep.kind}-${dep.id}`}>
        <rect
          x={DEP_X}
          y={y}
          width={DEP_W}
          height={DEP_H}
          rx={8}
          fill={resolved ? 'var(--color-background-muted)' : 'var(--color-background-card)'}
          stroke={resolved ? 'var(--color-border-emphasized)' : 'var(--color-border)'}
          strokeWidth={1}
          strokeDasharray={resolved ? undefined : '4 4'}
        />
        <text
          x={DEP_X + 10}
          y={y + 15}
          fill={resolved ? 'var(--color-text-primary)' : 'var(--color-text-disabled)'}
          fontSize={10.5}
          fontFamily="var(--font-family-code)"
        >
          {clip(nodeLabel(dep.id, unit), 26)}
        </text>
        <text
          x={DEP_X + 10}
          y={y + 27}
          fill="var(--color-text-secondary)"
          fontSize={9}
        >
          {requiresKind ? t('units.deps.requires') : t('units.deps.wants')}
        </text>
        {resolved ? null : (
          <text
            x={DEP_X + DEP_W - 58}
            y={y + 15}
            fill="var(--color-text-warning)"
            fontSize={9}
          >
            {t('units.deps.missing')}
          </text>
        )}
      </g>,
    );
  });

  return (
    <VStack gap={2}>
      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${WIDTH} ${height}`}
        role="img"
        aria-label={t('units.deps.graph_aria', { deps: deps.map((dep) => dep.id).join(', ') })}
        preserveAspectRatio="xMidYMid meet"
      >
        <defs>
          <marker id={reqMarker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-text-error)" />
          </marker>
          <marker id={wantMarker} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-text-warning)" />
          </marker>
          <marker id={reqMuted} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-text-disabled)" />
          </marker>
          <marker id={wantMuted} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-text-disabled)" />
          </marker>
        </defs>

        {edges}
        {nodes}

        <rect
          x={selfX}
          y={selfY}
          width={SELF_W}
          height={SELF_H}
          rx={10}
          fill="var(--color-accent-muted)"
          stroke="var(--color-accent)"
          strokeWidth={1.4}
        />
        <text
          x={selfX + 12}
          y={selfY + 22}
          fill="var(--color-text-primary)"
          fontSize={11}
          fontFamily="var(--font-family-code)"
        >
          {clip(selfId, 22)}
        </text>
        <text x={selfX + 12} y={selfY + 38} fill="var(--color-text-secondary)" fontSize={9.5}>
          {clip(unit?.label || unit?.kind || t('units.deps.self'), 24)}
        </text>
      </svg>

      <HStack gap={3} wrap="wrap" align="center">
        {requires.length ? (
          <HStack gap={1} wrap="wrap" align="center">
            <Text size="xsm" color="secondary">{t('units.deps.requires')}</Text>
            {requires.map((dep) => (
              <Token
                key={`req-${dep.id}`}
                size="sm"
                color={resolves(dep.id, units) ? 'red' : 'gray'}
                label={dep.id}
                description={resolves(dep.id, units) ? undefined : t('units.deps.missing')}
              />
            ))}
          </HStack>
        ) : null}
        {wants.length ? (
          <HStack gap={1} wrap="wrap" align="center">
            <Text size="xsm" color="secondary">{t('units.deps.wants')}</Text>
            {wants.map((dep) => (
              <Token
                key={`want-${dep.id}`}
                size="sm"
                color={resolves(dep.id, units) ? 'yellow' : 'gray'}
                label={dep.id}
                description={resolves(dep.id, units) ? undefined : t('units.deps.missing')}
              />
            ))}
          </HStack>
        ) : null}
      </HStack>
    </VStack>
  );
}
