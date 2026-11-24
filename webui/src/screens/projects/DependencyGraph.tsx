/**
 * DependencyGraph.tsx — a layered SVG of task prerequisites plus the textual
 * edge list above it.
 *
 * Layering is by dependency depth (a task sits one column right of its deepest
 * prerequisite); arrows point at the dependent task and turn red while an
 * upstream task is unfinished.  The graph is plain SVG built from React
 * elements: no chart library, no layout engine.
 */

import { useId, type ReactNode } from 'react';
import { Badge } from '@astryxdesign/core/Badge';
import { HStack, VStack } from '@astryxdesign/core/Layout';
import { List, ListItem } from '@astryxdesign/core/List';
import { Text } from '@astryxdesign/core/Text';
import { t } from '../../i18n';
import { statusLabel } from '../../ui/format';
import {
  layoutDependencies,
  openDepCount,
  taskDeps,
  taskKey,
  taskTitle,
} from './data';
import type { Task } from './types';
import { Hint, SectionHead, StatusBadge } from './ui';

const NODE_W = 168;
const NODE_H = 38;
const GAP_X = 54;
const GAP_Y = 16;
const PAD_X = 20;
const PAD_Y = 18;

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function toneColor(status: string): string {
  switch (status) {
    case 'done':
      return 'var(--color-success)';
    case 'blocked':
    case 'failed':
      return 'var(--color-error)';
    case 'review':
      return 'var(--color-warning)';
    case 'doing':
      return 'var(--color-accent)';
    default:
      return 'var(--color-text-secondary)';
  }
}

function DepGraph({
  tasks,
  selectedId,
  onSelect,
}: {
  tasks: Task[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}): ReactNode {
  const rawId = useId();
  const markerId = `prj-dep-arrow-${rawId.replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const { nodes, columns, byId } = layoutDependencies(tasks);
  if (!nodes.length) return <Hint>{t('deps.empty')}</Hint>;

  const maxColumn = Math.max(...[...columns.keys()], 0);
  const maxRows = Math.max(...[...columns.values()].map((list) => list.length), 1);
  const width = PAD_X * 2 + (maxColumn + 1) * NODE_W + maxColumn * GAP_X;
  const height = PAD_Y * 2 + maxRows * NODE_H + (maxRows - 1) * GAP_Y;

  const positions = new Map<string, { x: number; y: number }>();
  for (const column of [...columns.keys()].sort((a, b) => a - b)) {
    const list = columns.get(column) || [];
    list.forEach((task, index) => {
      positions.set(taskKey(task), {
        x: PAD_X + column * (NODE_W + GAP_X),
        y: PAD_Y + index * (NODE_H + GAP_Y),
      });
    });
  }

  return (
    <VStack className="prj-dep-graph-wrap" gap={0}>
      <svg
        className="prj-dep-graph"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={t('deps.graph.aria')}
      >
        <defs>
          <marker
            id={markerId}
            viewBox="0 0 10 10"
            refX="9"
            refY="5"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--color-text-secondary)" />
          </marker>
        </defs>

        {nodes.map((task) => {
          const target = positions.get(taskKey(task));
          if (!target) return null;
          return taskDeps(task).map((depId) => {
            const source = positions.get(depId);
            if (!source) return null;
            const dep = byId.get(depId);
            const isOpen = !dep || dep.status !== 'done';
            const x1 = source.x + NODE_W;
            const y1 = source.y + NODE_H / 2;
            const x2 = target.x - 8;
            const y2 = target.y + NODE_H / 2;
            const curve = Math.max(24, (x2 - x1) / 2);
            return (
              <path
                key={`${depId}->${taskKey(task)}`}
                d={`M ${x1} ${y1} C ${x1 + curve} ${y1}, ${x2 - curve} ${y2}, ${x2} ${y2}`}
                fill="none"
                stroke={isOpen ? 'var(--color-error)' : 'var(--color-border-emphasized)'}
                strokeWidth={isOpen ? 1.5 : 1.1}
                strokeDasharray={dep ? undefined : '4 4'}
                markerEnd={`url(#${markerId})`}
                opacity={isOpen ? 0.85 : 0.55}
              />
            );
          });
        })}

        {nodes.map((task) => {
          const key = taskKey(task);
          const position = positions.get(key);
          if (!position) return null;
          const isSelected = key === selectedId;
          return (
            <g
              key={key}
              className="prj-dep-node"
              transform={`translate(${position.x} ${position.y})`}
              tabIndex={0}
              role="button"
              aria-label={`${taskTitle(task)} · ${statusLabel(task.status)}`}
              onClick={() => onSelect(key)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onSelect(key);
                }
              }}
            >
              <rect
                className="prj-dep-node-box"
                width={NODE_W}
                height={NODE_H}
                rx={10}
                fill={isSelected ? 'var(--color-background-muted)' : 'var(--color-background-card)'}
                stroke={isSelected ? 'var(--color-accent)' : 'var(--color-border)'}
                strokeWidth={isSelected ? 1.8 : 1}
              />
              <circle cx={14} cy={NODE_H / 2} r={4} fill={toneColor(task.status)} />
              <text
                x={26}
                y={17}
                fill="var(--color-text-primary)"
                fontSize="10.5"
                fontWeight="600"
                fontFamily="var(--font-family-body)"
              >
                {truncate(taskTitle(task), 20)}
              </text>
              <text
                x={26}
                y={30}
                fill="var(--color-text-secondary)"
                fontSize="8.5"
                fontFamily="var(--font-family-code)"
              >
                {`${key} · ${task.status}`}
              </text>
            </g>
          );
        })}
      </svg>
    </VStack>
  );
}

export function DependencyGraphSection({
  tasks,
  selectedId,
  onSelect,
}: {
  tasks: Task[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}): ReactNode {
  const byId = new Map(tasks.map((task) => [task.id, task]));
  const withDeps = tasks.filter((task) => taskDeps(task).length > 0);

  return (
    <VStack gap={4}>
      <SectionHead level={2} title={t('deps.title')} hint={t('deps.hint')} />
      {withDeps.length === 0 ? (
        <Hint>{t('deps.empty')}</Hint>
      ) : (
        <>
          <List density="compact" hasDividers>
            {withDeps.map((task) => {
              const deps = taskDeps(task);
              const open = openDepCount(task, tasks);
              return (
                <ListItem
                  key={task.id}
                  onClick={() => onSelect(task.id)}
                  label={(
                    <HStack gap={2} align="center" wrap="wrap">
                      <Text type="body" weight="semibold">{taskTitle(task)}</Text>
                      <StatusBadge status={task.status} />
                      {open > 0 ? (
                        <Badge variant="error" label={t('deps.open', { n: open })} />
                      ) : null}
                    </HStack>
                  )}
                  description={(
                    <HStack gap={2} align="center" wrap="wrap">
                      <Text size="xsm" color="secondary">{t('deps.depends_on')}</Text>
                      {deps.map((depId) => {
                        const dep = byId.get(depId);
                        return (
                          <Badge
                            key={depId}
                            variant={dep && dep.status === 'done' ? 'success' : 'error'}
                            label={dep ? taskTitle(dep) : `${depId} · ${t('prj.deps.missing')}`}
                          />
                        );
                      })}
                    </HStack>
                  )}
                />
              );
            })}
          </List>
          <Text size="sm" weight="semibold">{t('deps.graph.title')}</Text>
          <DepGraph tasks={tasks} selectedId={selectedId} onSelect={onSelect} />
        </>
      )}
    </VStack>
  );
}
