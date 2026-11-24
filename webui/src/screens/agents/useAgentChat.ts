/**
 * useAgentChat.ts — the conversation projection for one agent.
 *
 * Why this exists: `state/store.ts` only folds the *system* assistant's
 * `chat.*` events into the global `stream`; `agent.chat.*` events are kept in
 * the generic `events` array.  So the Agent screen owns its own projection:
 *
 *  1. The authoritative thread comes from `GET /api/agents/{id}/chat`
 *     (messages + live busy flag + live draft snapshot).
 *  2. Live updates are folded from the store's `events` array, filtered to
 *     `agent.chat.{message,delta,tool,state,thread}` for this agent id — that
 *     needs no second EventSource and stays in sync with reconnects.
 *  3. Reliability net: while `busy` is true the thread is reconciled from the
 *     endpoint every 2.5s, and once more when a turn ends or the SSE stream
 *     reconnects.  A dropped delta can therefore only be late, never lost.
 *
 * If a delta/tool event names a message that is already complete, it is
 * ignored — that makes the replay/refetch overlap harmless.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../../api/client';
import {
  getState,
  loadState,
  useStore,
  type Agent,
  type Approval,
  type Project,
  type SfoEvent,
  type SfoState,
} from '../../state/store';
import { t } from '../../i18n';
import { toast, toastError } from '../../ui/toast';
import {
  normalizeMessage,
  normalizePayload,
  normalizeTasks,
  normalizeTool,
  replacePendingUser,
  upsertMessages,
  type AgentChatMessage,
  type AgentTask,
  type DraftState,
} from './types';

const POLL_MS = 2500;

/* stable selectors — never recreate them per render */
const selEvents = (s: SfoState): SfoEvent[] => s.events;
const selSse = (s: SfoState): SfoState['sse'] => s.sse;
const selAgents = (s: SfoState): Agent[] => s.agents;
const selProjects = (s: SfoState): Project[] => s.projects;
const selApprovals = (s: SfoState): Approval[] => s.approvals;

interface ThreadState {
  loading: boolean;
  refreshing: boolean;
  error: string;
  agent: Agent | null;
  project: Project | null;
  model: string;
  provider: string;
  messages: AgentChatMessage[];
  busy: boolean;
  typing: boolean;
  draft: DraftState | null;
}

const EMPTY: ThreadState = {
  loading: true,
  refreshing: false,
  error: '',
  agent: null,
  project: null,
  model: '',
  provider: '',
  messages: [],
  busy: false,
  typing: false,
  draft: null,
};

export interface AgentChatController extends ThreadState {
  /** Fresh roster row from the store, when the agent is still listed. */
  liveAgent: Agent | null;
  /** Fresh project card from the store. */
  liveProject: Project | null;
  tasks: AgentTask[];
  pendingApprovals: Approval[];
  /** Newest agent-scoped events, already trimmed and reversed. */
  activity: SfoEvent[];
  reload: () => Promise<void>;
  send: (text: string) => Promise<boolean>;
  cancel: () => Promise<void>;
  clear: () => Promise<boolean>;
  kill: () => Promise<boolean>;
}

function emptyDraft(id: string | null): DraftState {
  return { id, text: '', tools: [], typing: true };
}

function reduceEvent(prev: ThreadState, event: SfoEvent, agentId: string): ThreadState {
  const type = String(event.type || '');
  const data = (event.data || {}) as Record<string, unknown>;
  const owner = data.agent_id === undefined || data.agent_id === null ? '' : String(data.agent_id);
  if (owner && owner !== agentId) return prev;

  if (type === 'agent.chat.message') {
    const message = normalizeMessage(data);
    if (!message) return prev;
    const messages = upsertMessages(
      message.role === 'user' ? replacePendingUser(prev.messages, message.text) : prev.messages,
      message,
    );
    let draft = prev.draft;
    if (message.role === 'assistant' && (!draft || !draft.id || draft.id === message.id)) {
      draft = null;
    } else if (message.role === 'system') {
      draft = null;
    }
    return { ...prev, messages, draft, typing: false, error: '' };
  }

  if (type === 'agent.chat.delta') {
    const messageId = data.message_id ? String(data.message_id) : null;
    // A chunk for an already persisted message is a replay, not new text.
    if (messageId && prev.messages.some((item) => item.id === messageId)) return prev;
    if (data.typing) {
      const draft = prev.draft || emptyDraft(messageId);
      return {
        ...prev,
        typing: true,
        draft: { ...draft, id: draft.id || messageId, typing: true },
      };
    }
    if (!data.delta) return prev;
    const draft = prev.draft || emptyDraft(messageId);
    return {
      ...prev,
      typing: false,
      draft: {
        ...draft,
        id: draft.id || messageId,
        text: draft.text + String(data.delta),
        typing: false,
      },
    };
  }

  if (type === 'agent.chat.tool') {
    const messageId = data.message_id ? String(data.message_id) : null;
    if (messageId && prev.messages.some((item) => item.id === messageId)) return prev;
    const tool = normalizeTool(data);
    if (!tool) return prev;
    const draft = prev.draft || emptyDraft(messageId);
    const tools = draft.tools.slice();
    const index = tools.findIndex((item) => item.id && item.id === tool.id);
    if (index >= 0) tools[index] = { ...tools[index], ...tool };
    else tools.push(tool);
    return {
      ...prev,
      typing: false,
      draft: { ...draft, id: draft.id || messageId, tools, typing: false },
    };
  }

  if (type === 'agent.chat.state') {
    const busy = Boolean(data.busy);
    let draft = prev.draft;
    if (!busy && draft) {
      // Freeze partial content instead of leaving a spinner that never updates.
      draft = draft.text || draft.tools.length ? { ...draft, typing: false } : null;
    }
    return {
      ...prev,
      busy,
      typing: busy ? prev.typing : false,
      draft,
      // A turn ending means the authoritative final message is available.
      error: '',
    };
  }

  if (type === 'agent.chat.thread' && data.cleared) {
    return { ...prev, messages: [], draft: null, busy: false, typing: false };
  }

  return prev;
}

export function useAgentChat(agentId: string): AgentChatController {
  const [state, setState] = useState<ThreadState>({ ...EMPTY });
  const [tasks, setTasks] = useState<AgentTask[]>([]);

  const events = useStore(selEvents);
  const sse = useStore(selSse);
  const agents = useStore(selAgents);
  const projects = useStore(selProjects);
  const approvals = useStore(selApprovals);

  const cursorRef = useRef(0);
  const requestRef = useRef(0);
  const mountedRef = useRef(true);
  const prevBusyRef = useRef(false);
  const prevSseRef = useRef(sse);

  /* ---------------------------------------------------------------- */
  /* authoritative load / reconcile                                    */
  /* ---------------------------------------------------------------- */

  const fetchThread = useCallback(async (mode: 'initial' | 'manual' | 'silent'): Promise<void> => {
    const request = requestRef.current + 1;
    requestRef.current = request;
    if (mode === 'initial') {
      setState((prev) => ({ ...prev, loading: true }));
    } else if (mode === 'manual') {
      setState((prev) => ({ ...prev, refreshing: true }));
    }
    try {
      const data = await api.getAgentChat(agentId, 200);
      if (!mountedRef.current || request !== requestRef.current) return;
      const payload = normalizePayload(data);
      setState((prev) => {
        const messages = payload.messages || [];
        const pending = prev.messages.filter(
          (message) => message.pending
            && !messages.some((fresh) => fresh.role === 'user' && fresh.text === message.text),
        );
        return {
          ...prev,
          loading: false,
          refreshing: false,
          error: '',
          agent: payload.agent || prev.agent,
          project: payload.project || prev.project,
          model: payload.model || prev.model,
          provider: payload.provider || prev.provider,
          messages: upsertMessages(messages, pending),
          busy: Boolean(payload.busy),
          draft: payload.draft || (payload.busy ? prev.draft : null),
          typing: Boolean(payload.draft?.typing),
        };
      });
    } catch (error) {
      if (!mountedRef.current || request !== requestRef.current) return;
      const message = error instanceof Error ? error.message : String(error);
      if (mode === 'silent') {
        console.warn('[agents] conversation reconcile failed', error);
        return;
      }
      setState((prev) => ({ ...prev, loading: false, refreshing: false, error: message }));
      toastError(error);
    }
  }, [agentId]);

  /* reset + first load on agent change */
  useEffect(() => {
    mountedRef.current = true;
    // Everything that happened before this open is already inside the
    // fetched thread; folding it again would replay deltas into fresh drafts.
    cursorRef.current = getState().lastSeq;
    prevBusyRef.current = false;
    requestRef.current += 1; // invalidate any in-flight request for the old id
    setState({ ...EMPTY });
    setTasks([]);
    void fetchThread('initial');
    return () => {
      mountedRef.current = false;
      requestRef.current += 1;
    };
  }, [fetchThread]);

  /* ---------------------------------------------------------------- */
  /* live events                                                       */
  /* ---------------------------------------------------------------- */

  useEffect(() => {
    if (!events.length) return;
    const fresh: SfoEvent[] = [];
    for (const event of events) {
      const seq = Number(event.seq) || 0;
      if (seq > cursorRef.current) fresh.push(event);
    }
    if (!fresh.length) return;
    cursorRef.current = fresh.reduce((max, event) => Math.max(max, Number(event.seq) || 0), cursorRef.current);
    if (!fresh.some((event) => String(event.type || '').startsWith('agent.chat.'))) return;
    setState((prev) => {
      let next = prev;
      for (const event of fresh) next = reduceEvent(next, event, agentId);
      return next;
    });
  }, [events, agentId]);

  /* reconcile when a turn ends (final message may have arrived while away) */
  useEffect(() => {
    if (prevBusyRef.current && !state.busy) void fetchThread('silent');
    prevBusyRef.current = state.busy;
  }, [state.busy, fetchThread]);

  /* poll only while a turn is running: covers dropped SSE frames */
  useEffect(() => {
    if (!state.busy) return undefined;
    const timer = window.setInterval(() => { void fetchThread('silent'); }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [state.busy, fetchThread]);

  /* a reconnect may have skipped ephemeral deltas */
  useEffect(() => {
    if (prevSseRef.current !== 'live' && sse === 'live') {
      cursorRef.current = getState().lastSeq;
      void fetchThread('silent');
    }
    prevSseRef.current = sse;
  }, [sse, fetchThread]);

  /* ---------------------------------------------------------------- */
  /* tasks for the context sidebar                                     */
  /* ---------------------------------------------------------------- */

  const liveAgent = useMemo(
    () => agents.find((agent) => agent.id === agentId) || state.agent,
    [agents, agentId, state.agent],
  );
  const liveProject = useMemo(() => {
    const projectId = liveAgent?.project_id || state.project?.id || '';
    if (!projectId) return state.project;
    return projects.find((project) => project.id === projectId) || state.project;
  }, [projects, liveAgent, state.project]);
  const projectId = liveProject?.id || '';
  const taskId = liveAgent?.task_id || null;

  useEffect(() => {
    if (!projectId) {
      setTasks([]);
      return undefined;
    }
    let alive = true;
    api.getTasks(projectId)
      .then((data) => {
        if (alive) setTasks(normalizeTasks(data));
      })
      .catch((error) => {
        console.warn('[agents] could not load project tasks', error);
      });
    return () => { alive = false; };
  }, [projectId, taskId]);

  /* ---------------------------------------------------------------- */
  /* actions                                                           */
  /* ---------------------------------------------------------------- */

  const reload = useCallback(() => fetchThread('manual'), [fetchThread]);

  const send = useCallback(async (text: string): Promise<boolean> => {
    const value = text.trim();
    if (!value) return false;
    const optimistic: AgentChatMessage = {
      id: `local-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`,
      role: 'user',
      text: value,
      ts: Date.now(),
      agent_id: agentId,
      pending: true,
    };
    setState((prev) => ({
      ...prev,
      error: '',
      busy: true,
      typing: true,
      messages: upsertMessages(prev.messages, optimistic),
      draft: emptyDraft(null),
    }));
    try {
      const data = await api.sendAgentChat(agentId, value) as { message?: unknown; busy?: boolean } | null;
      const message = data ? normalizeMessage(data.message) : null;
      const busy = data?.busy !== false;
      setState((prev) => ({
        ...prev,
        messages: upsertMessages(replacePendingUser(prev.messages, value), message ? [message] : []),
        busy,
        draft: busy ? prev.draft : null,
      }));
      return true;
    } catch (error) {
      setState((prev) => ({
        ...prev,
        messages: prev.messages.filter((message) => message.id !== optimistic.id),
        busy: false,
        typing: false,
        draft: null,
      }));
      toastError(error);
      return false;
    }
  }, [agentId]);

  const cancel = useCallback(async (): Promise<void> => {
    try {
      const data = await api.cancelAgentChat(agentId) as { canceled?: boolean } | null;
      toast(data?.canceled ? t('agents.chat.cancel.requested') : t('agents.chat.cancel.none'), 'info');
    } catch (error) {
      toastError(error);
    }
  }, [agentId]);

  const clear = useCallback(async (): Promise<boolean> => {
    try {
      await api.clearAgentChat(agentId);
      setState((prev) => ({ ...prev, messages: [], draft: null, busy: false, typing: false, error: '' }));
      toast(t('agents.chat.clear.success'), 'info');
      return true;
    } catch (error) {
      toastError(error);
      return false;
    }
  }, [agentId]);

  const kill = useCallback(async (): Promise<boolean> => {
    try {
      await api.killAgent(agentId);
      toast(t('agents.kill.success'), 'info');
      await loadState({ quiet: true }).catch(() => null);
      await fetchThread('silent');
      return true;
    } catch (error) {
      toastError(error);
      return false;
    }
  }, [agentId, fetchThread]);

  /* ---------------------------------------------------------------- */
  /* derived                                                           */
  /* ---------------------------------------------------------------- */

  const pendingApprovals = useMemo(
    () => approvals.filter((item) => item.status === 'pending' && item.principal === agentId),
    [approvals, agentId],
  );

  const activity = useMemo(() => {
    const source = `agent:${agentId}`;
    return events
      .filter((event) => {
        if (!String(event.type || '').startsWith('agent.chat.')) return false;
        if (event.type === 'agent.chat.delta' || event.type === 'agent.chat.state') return false;
        const data = event.data || {};
        return data.agent_id === agentId || event.source === source;
      })
      .slice(-6)
      .reverse();
  }, [events, agentId]);

  const hasDraft = state.busy || Boolean(
    state.draft && (state.draft.text || state.draft.tools.length || state.draft.typing),
  );

  return {
    ...state,
    // A frozen draft with neither text nor tools is only a typing bubble; keep
    // it out of the message log once the turn is over.
    draft: hasDraft ? state.draft : null,
    liveAgent,
    liveProject,
    tasks,
    pendingApprovals,
    activity,
    reload,
    send,
    cancel,
    clear,
    kill,
  };
}
