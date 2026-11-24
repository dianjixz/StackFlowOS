/**
 * common.tsx — small view helpers shared by the roster and the conversation:
 * status → semantic colour, agent display name, avatar, and the mapping from
 * the backend's tool-record shape to Astryx `ChatToolCallItem`s.
 */

import type { ReactNode } from 'react';
import { Avatar, AvatarStatusDot } from '@astryxdesign/core/Avatar';
import { Badge } from '@astryxdesign/core/Badge';
import { CodeBlock } from '@astryxdesign/core/CodeBlock';
import { Text } from '@astryxdesign/core/Text';
import type { ChatToolCallItem, ChatToolCallStatus } from '@astryxdesign/core/Chat';
import { t } from '../../i18n';
import { clip, statusLabel, statusTone } from '../../ui/format';
import type { Agent } from '../../state/store';
import type { AgentChatTool } from './types';

export type BadgeVariant =
  | 'neutral'
  | 'info'
  | 'success'
  | 'warning'
  | 'error'
  | 'blue'
  | 'cyan'
  | 'green'
  | 'orange'
  | 'pink'
  | 'purple'
  | 'red'
  | 'teal'
  | 'yellow';

export type DotVariant = 'success' | 'warning' | 'error' | 'accent' | 'neutral';

/** Statuses after which an agent's process is gone and only its role remains. */
export const TERMINAL_STATUSES: ReadonlySet<string> = new Set(['done', 'blocked', 'failed', 'dead']);

/** Dispatchable roles, in the order the old console listed them. */
export const ROLE_ORDER: readonly string[] = [
  'lead',
  'architect',
  'coder',
  'tester',
  'reviewer',
  'researcher',
  'aggregator',
  'assistant',
];

export function isTerminal(status: unknown): boolean {
  return TERMINAL_STATUSES.has(String(status || ''));
}

export function statusBadgeVariant(status: unknown): BadgeVariant {
  switch (statusTone(status)) {
    case 'success':
      return 'success';
    case 'warning':
      return 'warning';
    case 'danger':
      return 'error';
    case 'info':
      return 'info';
    default:
      return 'neutral';
  }
}

export function statusDotVariant(status: unknown): DotVariant {
  switch (statusTone(status)) {
    case 'success':
      return 'success';
    case 'warning':
      return 'warning';
    case 'danger':
      return 'error';
    case 'info':
      return 'accent';
    default:
      return 'neutral';
  }
}

export function agentName(agent: Pick<Agent, 'id' | 'display_name'> | null | undefined): string {
  return String(agent?.display_name || agent?.id || '—');
}

/** AvatarStatusDot only has three variants; the text badge carries the rest. */
function avatarDotVariant(status: unknown): 'success' | 'neutral' | 'error' {
  switch (statusTone(status)) {
    case 'success':
    case 'info':
      return 'success';
    case 'danger':
      return 'error';
    default:
      return 'neutral';
  }
}

export function AgentAvatar({
  agent,
  size = 'md',
}: {
  agent: Pick<Agent, 'id' | 'role' | 'display_name' | 'status'> | null | undefined;
  size?: 'sm' | 'md' | 'lg' | 'xl';
}): ReactNode {
  const name = agentName(agent);
  return (
    <Avatar
      name={name}
      size={size}
      status={<AvatarStatusDot variant={avatarDotVariant(agent?.status)} label={statusLabel(agent?.status)} />}
    />
  );
}

export function StatusBadge({ status }: { status: unknown }): ReactNode {
  return <Badge label={statusLabel(status)} variant={statusBadgeVariant(status)} />;
}

/* ------------------------------------------------------------------ */
/* tool calls                                                         */
/* ------------------------------------------------------------------ */

/** `key=value` pairs, clipped so a row stays one line. */
export function argsSummary(args: Record<string, unknown> | undefined): string {
  if (!args) return '';
  return Object.entries(args)
    .filter(([, value]) => value !== undefined && value !== null && String(value) !== '')
    .map(([key, value]) => `${key}=${clip(String(value), 52)}`)
    .join('  ');
}

export function toolStateLabel(state: unknown): string {
  const key = String(state || 'running');
  return t(`agents.chat.tool.${key}`, {}, key);
}

function toolStatus(state: unknown): ChatToolCallStatus {
  switch (String(state || 'running')) {
    case 'running':
      return 'running';
    case 'pending':
      return 'pending';
    case 'failed':
    case 'denied':
      return 'error';
    default:
      return 'complete';
  }
}

/** Map one backend tool record to the design-system's tool-call item. */
export function toolCallItem(tool: AgentChatTool, index: number): ChatToolCallItem {
  const state = String(tool.state || 'running');
  const detail = tool.error || tool.output || tool.preview || '';
  const item: ChatToolCallItem = {
    name: tool.tool || 'tool',
    status: toolStatus(state),
    key: tool.id || `${tool.tool || 'tool'}-${index}`,
  };
  const target = tool.preview || argsSummary(tool.args);
  if (target) item.target = target;
  if (tool.error) item.errorMessage = tool.error;
  if (typeof tool.duration_ms === 'number' && tool.duration_ms > 0) {
    item.duration = tool.duration_ms >= 1000
      ? `${(tool.duration_ms / 1000).toFixed(1)}s`
      : `${tool.duration_ms}ms`;
  }
  if (detail) {
    item.resultDetail = tool.error ? (
      <Text type="supporting" color="primary">{detail}</Text>
    ) : (
      <CodeBlock
        code={clip(detail, 2000)}
        language="plaintext"
        hasLanguageLabel={false}
        hasLineNumbers={false}
        maxHeight={220}
        size="sm"
        width="100%"
      />
    );
  }
  return item;
}

export function toolCallItems(tools: AgentChatTool[] | undefined): ChatToolCallItem[] {
  return (tools || []).map(toolCallItem);
}
