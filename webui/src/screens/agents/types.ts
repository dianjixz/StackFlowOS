/**
 * types.ts — the wire shapes of the per-agent conversation API
 * (`GET /api/agents/{id}/chat`) plus small normalisers.
 *
 * The store only keeps `agent.chat.*` events in its generic `events` array
 * (the global `stream` belongs to the system assistant), so this screen owns
 * the conversation projection.  Everything is normalised once here so the
 * hook and the view never have to guess at `unknown` payloads.
 */

import type { Agent, Project } from '../../state/store';

export interface AgentChatTool {
  id?: string;
  tool?: string;
  state?: string;
  args?: Record<string, unknown>;
  preview?: string;
  output?: string;
  error?: string;
  /** `result` / `duration_ms` are the fixture's spelling of output/duration. */
  duration_ms?: number;
  ts?: number;
}

export interface AgentChatMeta {
  tools?: AgentChatTool[];
  reason?: string;
  error?: string;
  from?: string;
}

export interface AgentChatMessage {
  id: string;
  role: string;
  text: string;
  ts: number;
  agent_id?: string | null;
  meta?: AgentChatMeta;
  /** Set only for the optimistic user bubble that the server has not echoed yet. */
  pending?: boolean;
}

export interface AgentChatDraft {
  message_id?: string | null;
  typing?: boolean;
  text?: string;
  tools?: AgentChatTool[];
}

/** The live, in-memory draft of an agent reply. */
export interface DraftState {
  id: string | null;
  text: string;
  tools: AgentChatTool[];
  typing: boolean;
}

export interface AgentChatPayload {
  agent?: Agent | null;
  project?: Project | null;
  model?: string;
  provider?: string;
  messages?: AgentChatMessage[];
  busy?: boolean;
  draft?: DraftState | null;
}

export interface AgentTask {
  id: string;
  title?: string;
  status?: string;
  priority?: number;
  role?: string;
  assignee?: string | null;
  [key: string]: unknown;
}

const asRecord = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

export function normalizeTool(value: unknown): AgentChatTool | null {
  return asTool(value);
}

function asTool(value: unknown): AgentChatTool | null {
  const row = asRecord(value);
  if (!row) return null;
  const tool: AgentChatTool = {};
  if (row.id !== undefined) tool.id = String(row.id);
  if (row.tool !== undefined) tool.tool = String(row.tool);
  if (row.state !== undefined) tool.state = String(row.state);
  if (row.preview !== undefined) tool.preview = String(row.preview);
  if (row.output !== undefined) tool.output = String(row.output);
  if (row.result !== undefined && row.output === undefined) tool.output = String(row.result);
  if (row.error !== undefined) tool.error = String(row.error);
  if (row.duration_ms !== undefined) tool.duration_ms = Number(row.duration_ms) || 0;
  if (row.ts !== undefined) tool.ts = Number(row.ts) || 0;
  const args = asRecord(row.args);
  if (args) tool.args = args;
  return tool;
}

function asToolList(value: unknown): AgentChatTool[] {
  if (!Array.isArray(value)) return [];
  return value.map(asTool).filter((item): item is AgentChatTool => item !== null);
}

/** Normalise one persisted/streamed message. */
export function normalizeMessage(value: unknown): AgentChatMessage | null {
  const row = asRecord(value);
  if (!row) return null;
  const id = row.id === undefined || row.id === null ? '' : String(row.id);
  if (!id) return null;
  const role = String(row.role || 'assistant');
  const text = row.text === undefined || row.text === null ? '' : String(row.text);
  const ts = Number(row.ts) || Date.now();
  const meta = asRecord(row.meta);
  const message: AgentChatMessage = { id, role, text, ts };
  if (row.agent_id !== undefined) message.agent_id = row.agent_id === null ? null : String(row.agent_id);
  if (meta) {
    const normalized: AgentChatMeta = {};
    if (meta.reason !== undefined) normalized.reason = String(meta.reason);
    if (meta.error !== undefined) normalized.error = String(meta.error);
    if (meta.from !== undefined) normalized.from = String(meta.from);
    const tools = asToolList(meta.tools);
    if (tools.length) normalized.tools = tools;
    message.meta = normalized;
  }
  return message;
}

export function normalizeMessages(value: unknown): AgentChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value
    .map(normalizeMessage)
    .filter((item): item is AgentChatMessage => item !== null)
    .sort((a, b) => a.ts - b.ts);
}

export function normalizeDraft(value: unknown): DraftState | null {
  const row = asRecord(value);
  if (!row) return null;
  const text = row.text === undefined || row.text === null ? '' : String(row.text);
  const tools = asToolList(row.tools);
  const typing = Boolean(row.typing);
  if (!text && !tools.length && !typing) return null;
  return {
    id: row.message_id === undefined || row.message_id === null ? null : String(row.message_id),
    text,
    tools,
    typing,
  };
}

export function normalizePayload(value: unknown): AgentChatPayload {
  const row = asRecord(value);
  if (!row) return {};
  return {
    agent: (asRecord(row.agent) as Agent | null) || null,
    project: (asRecord(row.project) as Project | null) || null,
    model: row.model === undefined ? '' : String(row.model),
    provider: row.provider === undefined ? '' : String(row.provider),
    messages: normalizeMessages(row.messages),
    busy: Boolean(row.busy),
    draft: normalizeDraft(row.draft),
  };
}

/** Insert or replace by id, then keep the thread ordered by timestamp. */
export function upsertMessages(
  messages: AgentChatMessage[],
  incoming: AgentChatMessage | AgentChatMessage[],
): AgentChatMessage[] {
  const list = Array.isArray(incoming) ? incoming : [incoming];
  if (!list.length) return messages;
  const byId = new Map(messages.map((message) => [message.id, message]));
  for (const message of list) byId.set(message.id, { ...message, pending: false });
  return [...byId.values()].sort((a, b) => a.ts - b.ts);
}

/** Drop one optimistic user bubble once the server echoes it. */
export function replacePendingUser(
  messages: AgentChatMessage[],
  text: string,
): AgentChatMessage[] {
  return messages.filter(
    (message) => !(message.pending && message.role === 'user' && message.text === text),
  );
}

/** Resolve `task_id` → title from a project's task list. */
export function findTask(tasks: AgentTask[], taskId: string | null | undefined): AgentTask | null {
  if (!taskId) return null;
  return tasks.find((task) => task.id === taskId) || null;
}

/** Normalise `GET /api/projects/{id}/tasks` into rows the sidebar can read. */
export function normalizeTasks(value: unknown): AgentTask[] {
  const payload = value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
  const rows = Array.isArray(payload.tasks) ? payload.tasks : [];
  return rows.flatMap((row): AgentTask[] => {
    if (!row || typeof row !== 'object') return [];
    const task = row as Record<string, unknown>;
    const id = task.id === undefined ? '' : String(task.id);
    if (!id) return [];
    return [{
      ...(task as AgentTask),
      id,
      title: task.title === undefined ? undefined : String(task.title),
      status: task.status === undefined ? undefined : String(task.status),
    }];
  });
}
