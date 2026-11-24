/**
 * agentchat.js — the per-agent conversation surface.
 *
 * Opened from the Agent app (`#agent/<agent-id>`).  Each roster agent has a
 * durable thread on the backend; this module renders that thread, mirrors the
 * live SSE events (`agent.chat.*`) while a turn runs, and lets the human send
 * instructions, stop a turn, clear the thread or terminate the agent.
 *
 * Rendering rules follow the rest of the console: all dynamic text goes
 * through textContent, with no markup-injection API, and no build step.
 */

import { api } from './api.js';
import { loadState, on, state } from './store.js';
import {
  avatar,
  bar,
  clip,
  el,
  eventSummary,
  fmtDateTime,
  fmtTime,
  icon,
  initialsFor,
  richText,
  roleLabel,
  spinner,
  statusLabel,
  toast,
  toastError,
} from './ui.js';

const TERMINAL = new Set(['done', 'failed', 'dead']);
const ACTIVE = new Set(['spawning', 'idle', 'working', 'waiting']);

const ROLE_HINT = {
  lead: '负责拆解目标、派工和推进项目；可以建任务、派 Agent、看事件。',
  architect: '负责设计文档、接口和数据形状；在项目 workspace 内写设计稿。',
  coder: '负责写代码和实现文件；可以读文件、写文件、跑受限命令。',
  tester: '负责运行最小自检并写测试报告；可以执行受限命令。',
  reviewer: '负责对照验收标准审查产物，给出通过或打回结论。',
  researcher: '负责抓取公开资料并写调研笔记；只访问 http/https。',
  aggregator: '负责汇总项目状态、任务与事件，更新项目 brief。',
  assistant: '系统助手角色；可以查询状态、建任务、派 Agent。',
};

const ROLE_SUGGESTIONS = {
  lead: [
    '看看项目现在的进度，有哪些任务卡住了？',
    '把项目目标拆成任务并安排合适的角色。',
    '检查待审任务，安排评审。',
  ],
  architect: [
    '阅读项目目标和现有产物，写一份设计文档。',
    '解释当前实现的结构和主要风险。',
    '列出这个项目还缺哪些接口和数据形状。',
  ],
  coder: [
    '看看当前任务的产物还缺什么，然后补齐。',
    '列出 workspace 里的文件，并解释它们的用途。',
    '为当前任务写一份最小实现并做语法自检。',
  ],
  tester: [
    '对当前任务的产物跑一次最小自检。',
    '阅读现有测试报告，指出还没覆盖的路径。',
    '列出 workspace 里可测试的文件。',
  ],
  reviewer: [
    '读取当前任务的产物，对照验收标准给出结论。',
    '检查任务产物的路径和内容是否匹配任务描述。',
    '列出这个任务缺少的验收证据。',
  ],
  researcher: [
    '围绕当前任务做一次资料调研并写笔记。',
    '列出调研笔记里还需要确认的问题。',
    '阅读现有调研产物并总结结论。',
  ],
  aggregator: [
    '汇总项目当前任务状态和风险。',
    '读最近的工程事件，写一段项目简报。',
    '看看项目 brief 是否还准确。',
  ],
  assistant: [
    '现在有哪些项目在推进？',
    '看看有没有待审批的操作。',
    '介绍一下这个系统能做什么。',
  ],
};

const DEFAULT_SUGGESTIONS = [
  '现在的情况怎么样？',
  '你能帮我做什么？',
  '先看看项目里的文件。',
];

/** Module-wide active view; only one agent conversation is mounted at a time. */
let active = null;

export function initAgentChat() {
  on('event', handleEvent);
  on('state', () => {
    if (!active) return;
    syncFromState();
  });
  on('sse', () => {
    if (active) syncComposer();
  });
}

/** Mount (or reuse) the conversation view for one agent. */
export function mount({ agentId, agent = null }) {
  if (active && active.agentId === agentId && active.root.isConnected) {
    if (agent) {
      active.agent = agent;
      syncHeader();
      syncAside();
    }
    return active.root;
  }
  unmount();
  const view = createView(agentId, agent);
  active = view;
  mountInto(view);
  return view.root;
}

export function unmount() {
  if (!active) return;
  active.token += 1;
  active = null;
}

export function isMounted(agentId) {
  return Boolean(active && active.agentId === agentId && active.root.isConnected);
}

export function activeAgentId() {
  return active ? active.agentId : null;
}

/* ------------------------------------------------------------------ */
/* view construction                                                   */
/* ------------------------------------------------------------------ */

function createView(agentId, agent) {
  const view = {
    agentId,
    agent: agent || fallbackAgent(agentId),
    project: null,
    model: '',
    provider: '',
    messages: [],
    tasks: [],
    seen: new Map(),
    busy: false,
    loading: true,
    error: '',
    draft: null,
    pendingUser: null,
    token: 0,
    asideFrame: null,
    tasksTimer: null,
    els: {},
  };

  const root = el('div', { class: 'agent-chat' });
  const back = el('button', {
    class: 'back-btn ac-back',
    type: 'button',
    title: '返回 Agent 花名册',
    onclick: () => { location.hash = '#agents'; },
  }, icon('back', 'ico ico-sm'), '花名册');

  const status = el('span', {
    class: 'pill',
    dataset: { status: view.agent.status || 'idle' },
  }, statusLabel(view.agent.status));

  const clearBtn = el('button', {
    class: 'btn btn-ghost btn-sm',
    type: 'button',
    title: '清空这个 Agent 的对话记录',
    onclick: () => clearThread(view),
  }, icon('trash', 'ico ico-sm'), '清空');

  const refreshBtn = el('button', {
    class: 'icon-btn',
    type: 'button',
    title: '重新载入对话',
    'aria-label': '重新载入对话',
    onclick: () => load(view),
  }, icon('refresh', 'ico ico-sm'));

  const killBtn = el('button', {
    class: 'btn btn-danger btn-sm',
    type: 'button',
    title: '终止这个 Agent 的进程',
    onclick: () => killAgent(view),
  }, icon('close', 'ico ico-sm'), '终止');

  // The generic sheet header is hidden for the conversation (it duplicates
  // the identity below); the close button moves into this toolbar.
  const closeBtn = el('button', {
    class: 'icon-btn ac-close',
    type: 'button',
    title: '关闭对话',
    'aria-label': '关闭对话',
    onclick: () => document.getElementById('sheet-close')?.click(),
  }, icon('close', 'ico ico-sm'));

  const nameEl = el('span', { class: 'ac-id-name' },
    view.agent.display_name || view.agent.id);
  const metaEl = el('span', { class: 'ac-id-meta' },
    `${roleLabel(view.agent.role)} · ${view.agent.id}`);
  const topbar = el('div', { class: 'ac-topbar' },
    back,
    el('div', { class: 'ac-identity' },
      avatar(view.agent, { large: true }),
      el('div', { class: 'ac-identity-text' }, nameEl, metaEl),
    ),
    el('div', { class: 'ac-head-actions' },
      status, refreshBtn, clearBtn, killBtn, closeBtn),
  );

  const banner = el('div', { class: 'ac-banner', hidden: true });
  const log = el('ol', {
    class: 'chat-log ac-log',
    'aria-live': 'polite',
    'aria-relevant': 'additions text',
  });
  const loading = el('div', { class: 'chat-loading', hidden: true },
    spinner(), '正在载入对话…');
  const scrollBottom = el('button', {
    class: 'scroll-bottom',
    type: 'button',
    'aria-label': '回到底部',
    hidden: true,
    onclick: () => scrollToBottom('smooth'),
  }, icon('arrow-down'));
  const scroll = el('div', { class: 'chat-scroll ac-scroll' }, log, loading, scrollBottom);

  const suggestions = el('div', { class: 'suggestions ac-suggestions' });
  const input = el('textarea', {
    class: 'composer-input',
    rows: '1',
    maxlength: '8000',
    placeholder: '对这个 Agent 说点什么…（Enter 发送）',
    'aria-label': '消息',
  });
  const stopBtn = el('button', {
    class: 'send-btn ac-stop',
    type: 'button',
    title: '停止本轮回复',
    'aria-label': '停止本轮回复',
    hidden: true,
    onclick: () => cancelTurn(view),
  }, el('span', { class: 'ac-stop-glyph', 'aria-hidden': 'true' }));
  const sendBtn = el('button', {
    class: 'send-btn',
    type: 'submit',
    'aria-label': '发送消息',
  }, icon('send'));
  const form = el('form', { class: 'composer ac-composer' }, input, stopBtn, sendBtn);
  const hint = el('p', { class: 'composer-hint ac-hint' },
    'Enter 发送 · Shift+Enter 换行 · 高风险动作仍需审批');
  const composer = el('div', { class: 'composer-wrap ac-compose' },
    suggestions, form, hint);

  const aside = el('aside', { class: 'ac-aside', 'aria-label': 'Agent 上下文' });
  const conversation = el('section', { class: 'ac-conversation' }, scroll, composer);
  const body = el('div', { class: 'ac-body' }, conversation, aside);
  root.append(topbar, banner, body);

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    sendText(view, input.value);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener('input', () => autosize(input));
  scroll.addEventListener('scroll', () => updateScrollButton(view), { passive: true });

  view.root = root;
  view.els = {
    topbar, back, status, name: nameEl, meta: metaEl,
    clearBtn, refreshBtn, killBtn, banner,
    scroll, log, loading, scrollBottom, suggestions, input, stopBtn, sendBtn,
    form, hint, composer, aside, conversation, body,
  };
  return view;
}

function mountInto(view) {
  syncHeader();
  syncComposer();
  renderSuggestions(view);
  renderEmptyState(view);
  syncAside();
  load(view);
}

function fallbackAgent(agentId) {
  return {
    id: agentId,
    role: 'agent',
    display_name: agentId,
    status: 'unknown',
    project_id: null,
    task_id: null,
  };
}

/* ------------------------------------------------------------------ */
/* loading                                                             */
/* ------------------------------------------------------------------ */

async function load(view) {
  const token = ++view.token;
  // A refresh re-fetches the authoritative thread and draft; drop the current
  // (possibly detached) draft so restoreDraft builds fresh nodes.
  clearDraft(view);
  view.pendingUser = null;
  view.loading = true;
  view.els.loading.hidden = false;
  view.els.loading.replaceChildren(spinner(), el('span', {}, '正在载入对话…'));
  syncComposer();
  try {
    const data = await api.getAgentChat(view.agentId, 200);
    if (active !== view || token !== view.token) return;
    if (data && data.agent) view.agent = data.agent;
    view.project = data && data.project ? data.project : null;
    view.model = (data && data.model) || view.model;
    view.provider = (data && data.provider) || view.provider;
    view.messages = [];
    view.seen.clear();
    view.els.log.replaceChildren();
    for (const message of (data && data.messages) || []) upsertMessage(view, message, { instant: true });
    view.busy = Boolean(data && data.busy);
    view.error = '';
    syncHeader();
    syncAside();
    syncComposer();
    if (data && data.draft && data.busy) restoreDraft(view, data.draft);
    if (view.busy && !view.draft) showTyping(view);
    renderEmptyIfNeeded(view);
    scrollToBottom('auto');
    if (view.project) refreshTasks(view);
  } catch (error) {
    if (active !== view || token !== view.token) return;
    view.error = error.message || String(error);
    renderLoadError(view);
    toastError(error);
  } finally {
    if (active === view && token === view.token) {
      view.loading = false;
      view.els.loading.hidden = true;
      syncComposer();
      syncSuggestions(view);
      renderEmptyIfNeeded(view);
    }
  }
}

function renderLoadError(view) {
  view.els.log.replaceChildren(el('li', { class: 'ac-load-error' },
    el('div', { class: 'empty-glyph' }, icon('alert')),
    el('h3', {}, '对话载入失败'),
    el('p', {}, view.error || '未知错误'),
    el('button', {
      class: 'btn btn-ghost btn-sm',
      type: 'button',
      onclick: () => load(view),
    }, icon('refresh', 'ico ico-sm'), '重试'),
  ));
}

/* ------------------------------------------------------------------ */
/* messages                                                            */
/* ------------------------------------------------------------------ */

function upsertMessage(view, message, { instant = false } = {}) {
  if (!message || !message.id) return;
  const existing = view.seen.get(message.id);
  if (existing) {
    const bubble = existing.querySelector('.bubble');
    if (bubble && message.text && existing.dataset.text !== message.text) {
      bubble.replaceChildren(renderText(message.text));
      existing.dataset.text = message.text;
    }
    return;
  }
  // A real user message replaces the optimistic bubble with the same text.
  if (message.role === 'user' && view.pendingUser
      && view.pendingUser.id !== message.id
      && view.pendingUser.text === message.text) {
    removePendingUser(view);
  }
  if (message.role === 'assistant' && view.draft
      && (!view.draft.id || view.draft.id === message.id)) {
    clearDraft(view);
  }
  if (message.role === 'system' && message.meta
      && message.meta.reason === 'cancelled') {
    stopDraft(view, '本轮已停止');
  }
  const node = buildMessage(view, message, { instant });
  view.seen.set(message.id, node);
  view.messages.push(message);
  view.els.log.querySelector('.chat-empty-wrap')?.remove();
  view.els.log.append(node);
  if (!instant && isNearBottom(view)) scrollToBottom('smooth');
  updateScrollButton(view);
  syncSuggestions(view);
  scheduleAside(view);
}

function buildMessage(view, message, { instant = false } = {}) {
  const role = String(message.role || 'assistant');
  if (role === 'system') {
    return el('li', { class: 'ac-system-note', dataset: { messageId: message.id } },
      message.text || '');
  }
  const isUser = role === 'user';
  const body = el('div', { class: 'msg-body' });
  const tools = !isUser && message.meta && Array.isArray(message.meta.tools)
    ? message.meta.tools
    : [];
  if (tools.length) body.append(buildToolList(tools));
  const bubble = el('div', { class: 'bubble' });
  bubble.append(renderText(message.text || ''));
  body.append(bubble);
  const meta = el('div', { class: 'msg-meta' },
    el('span', {}, fmtTime(message.ts)));
  if (!isUser) {
    if (view.model) meta.append(el('span', { class: 'mini-tag', title: view.model },
      clip(view.model, 20)));
    if (message.agent_id) meta.append(el('span', { class: 'mini-tag' },
      clip(message.agent_id, 24)));
  }
  body.append(meta);
  const av = isUser
    ? el('span', { class: 'msg-avatar', 'aria-hidden': 'true' }, '我')
    : el('span', {
      class: 'msg-avatar',
      'aria-hidden': 'true',
      style: { '--av-color': colorOf(view.agent) },
      title: view.agent.display_name || view.agent.id,
    }, initialsFor(view.agent));
  const node = el('li', {
    class: `msg ${isUser ? 'is-user' : 'is-assistant'}${instant ? ' is-instant' : ''}`,
    dataset: { messageId: message.id, text: message.text || '' },
  }, av, body);
  node.title = fmtDateTime(message.ts);
  return node;
}

function renderText(source) {
  const fragment = document.createDocumentFragment();
  const text = String(source ?? '');
  const fence = /```([a-zA-Z0-9_+-]*)\r?\n([\s\S]*?)```/g;
  let last = 0;
  let match;
  while ((match = fence.exec(text)) !== null) {
    if (match.index > last) fragment.append(richText(text.slice(last, match.index)));
    const code = match[2].replace(/\r?\n$/, '');
    fragment.append(el('pre', {
      class: 'ac-code-block',
      dataset: { lang: match[1] || 'text' },
    }, el('code', {}, code)));
    last = match.index + match[0].length;
  }
  if (last < text.length) fragment.append(richText(text.slice(last)));
  return fragment;
}

function colorOf(agent) {
  // Local copy of the avatar palette seed rule; avoids exporting internals.
  const seed = String((agent && (agent.project_id || agent.id || agent.role)) || 'agent');
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) hash = (hash * 31 + seed.charCodeAt(i)) >>> 0;
  const palette = ['#9a80ff', '#5bc0ff', '#3ddc97', '#ffb454', '#ff7ea0', '#57d6d0', '#c19dff', '#7ea6ff'];
  return palette[hash % palette.length];
}

function renderEmptyIfNeeded(view) {
  if (view.messages.length || view.busy || view.loading || view.error || view.draft) return;
  if (view.els.log.querySelector('.chat-empty-wrap')) return;
  view.els.log.append(buildEmptyState(view));
}

function renderEmptyState(view) {
  if (view.messages.length || view.busy || view.draft) return;
  view.els.log.replaceChildren(buildEmptyState(view));
}

function buildEmptyState(view) {
  const role = String(view.agent.role || '');
  const terminal = TERMINAL.has(view.agent.status);
  const notes = [];
  if (terminal) {
    notes.push('这个 Agent 已结束；继续对话会以同一角色身份工作，动作仍走审批。');
  } else if (ACTIVE.has(view.agent.status) && view.agent.status !== 'idle') {
    notes.push('这个 Agent 正在执行任务；这里的对话不会中断它的进程。');
  }
  if (!view.project) notes.push('它还没有绑定项目，文件/命令工具不可用。');
  const prompts = (ROLE_SUGGESTIONS[role] || DEFAULT_SUGGESTIONS).slice(0, 3);
  return el('li', { class: 'chat-empty-wrap ac-empty-wrap' },
    el('div', { class: 'chat-empty ac-empty' },
      avatar(view.agent, { large: true }),
      el('h3', {}, `和 ${view.agent.display_name || view.agent.id} 开始工作`),
      el('p', {}, ROLE_HINT[role] || '把你的要求直接告诉它，它会用角色工具去做并汇报。'),
      notes.length ? el('p', { class: 'ac-empty-note' }, notes.join(' ')) : null,
      el('div', { class: 'suggestion-row' },
        ...prompts.map((text) => el('button', {
          class: 'suggestion-chip',
          type: 'button',
          title: text,
          onclick: () => sendText(view, text),
        }, clip(text, 34))),
      ),
    ),
  );
}

function renderSuggestions(view) {
  const role = String(view.agent.role || '');
  const prompts = (ROLE_SUGGESTIONS[role] || DEFAULT_SUGGESTIONS).slice(0, 2);
  view.els.suggestions.replaceChildren();
  for (const text of prompts) {
    view.els.suggestions.append(el('button', {
      class: 'suggestion-chip',
      type: 'button',
      title: text,
      onclick: () => sendText(view, text),
    }, icon('chat', 'ico ico-xs'), clip(text, 30)));
  }
  syncSuggestions(view);
}

function syncSuggestions(view) {
  if (!view || !view.els.suggestions) return;
  // The empty state already offers role-specific prompts; keep the composer
  // strip for follow-up turns so the two do not repeat the same chips.
  view.els.suggestions.hidden = view.messages.length === 0 || view.busy;
}

/* ------------------------------------------------------------------ */
/* streaming / typing / tools                                          */
/* ------------------------------------------------------------------ */

function ensureDraft(view, messageId = null) {
  if (view.draft) {
    if (messageId && !view.draft.id) view.draft.id = messageId;
    if (messageId && view.draft.id && view.draft.id !== messageId) {
      clearDraft(view);
    } else {
      return view.draft;
    }
  }
  const toolWrap = el('div', { class: 'ac-tools' });
  const bubble = el('div', { class: 'bubble typing-bubble' },
    el('span', {}, '正在思考'),
    el('span', { class: 'typing-dots', 'aria-hidden': 'true' },
      el('i'), el('i'), el('i')),
  );
  const body = el('div', { class: 'msg-body' }, toolWrap, bubble);
  const node = el('li', { class: 'msg is-assistant msg-stream ac-draft' },
    el('span', {
      class: 'msg-avatar',
      'aria-hidden': 'true',
      style: { '--av-color': colorOf(view.agent) },
    }, initialsFor(view.agent)),
    body,
  );
  view.els.log.querySelector('.chat-empty-wrap')?.remove();
  view.els.log.append(node);
  view.draft = {
    id: messageId || null,
    text: '',
    tools: [],
    typing: true,
    toolWrap,
    bubble,
    node,
  };
  return view.draft;
}

function restoreDraft(view, draft) {
  const node = ensureDraft(view, draft.message_id || null);
  node.text = String(draft.text || '');
  node.typing = Boolean(draft.typing) && !node.text;
  node.tools = Array.isArray(draft.tools) ? draft.tools.slice(0, 20) : [];
  renderDraft(view);
}

function showTyping(view, messageId = null) {
  ensureDraft(view, messageId);
  if (view.draft) view.draft.typing = true;
  renderDraft(view);
}

function renderDraft(view) {
  const draft = view.draft;
  if (!draft) return;
  if (draft.text) {
    draft.bubble.className = 'bubble is-streaming';
    draft.bubble.replaceChildren(renderText(draft.text));
  } else if (draft.typing) {
    draft.bubble.className = 'bubble typing-bubble';
    draft.bubble.replaceChildren(
      el('span', {}, '正在思考'),
      el('span', { class: 'typing-dots', 'aria-hidden': 'true' },
        el('i'), el('i'), el('i')),
    );
  } else {
    draft.bubble.className = 'bubble';
    draft.bubble.replaceChildren();
  }
  draft.bubble.hidden = !draft.text && !draft.typing;
  renderDraftTools(view);
}

function renderDraftTools(view) {
  const draft = view.draft;
  if (!draft) return;
  draft.toolWrap.replaceChildren();
  if (!draft.tools.length) return;
  for (const tool of draft.tools) draft.toolWrap.append(buildToolCard(tool));
}

function clearDraft(view) {
  if (!view.draft) return;
  view.draft.node.remove();
  view.draft = null;
}

function stopDraft(view, reason) {
  const draft = view.draft;
  if (!draft) return;
  for (const tool of draft.tools) {
    if (tool.state === 'running') {
      tool.state = 'failed';
      tool.error = tool.error || reason;
    }
  }
  draft.typing = false;
  renderDraft(view);
  draft.bubble.classList.remove('is-streaming');
  draft.node.classList.add('is-stopped');
  view.draft = null;
}

function updateTool(view, payload) {
  if (!payload || !payload.tool) return;
  const draft = ensureDraft(view, payload.message_id || null);
  const callId = payload.id || payload.call_id || '';
  const index = draft.tools.findIndex((item) => item.id === callId);
  const record = {
    id: callId || `call-${draft.tools.length + 1}`,
    tool: payload.tool,
    state: payload.state || 'running',
    args: payload.args || {},
    preview: payload.preview || '',
    output: payload.output || '',
    error: payload.error || '',
    ts: payload.ts || Date.now(),
  };
  if (index >= 0) draft.tools[index] = { ...draft.tools[index], ...record };
  else draft.tools.push(record);
  draft.typing = false;
  renderDraft(view);
  if (isNearBottom(view)) scrollToBottom('auto');
  scheduleAside(view);
}

function buildToolList(tools) {
  const wrap = el('div', { class: 'ac-tools' });
  for (const tool of tools) {
    if (tool && typeof tool === 'object') wrap.append(buildToolCard(tool));
  }
  return wrap;
}

function buildToolCard(tool) {
  const stateName = String(tool.state || 'running');
  const labels = {
    running: '执行中',
    ok: '完成',
    failed: '失败',
    denied: '已拒绝',
  };
  const iconName = stateName === 'ok' ? 'check'
    : stateName === 'running' ? null : 'alert';
  const head = el('div', { class: 'ac-tool-head' },
    stateName === 'running'
      ? spinner()
      : el('span', { class: 'ac-tool-ico' }, icon(iconName || 'terminal', 'ico ico-xs')),
    el('code', { class: 'ac-tool-name' }, tool.tool || 'tool'),
    el('span', { class: 'ac-tool-state', dataset: { state: stateName } },
      labels[stateName] || stateName),
  );
  const card = el('div', {
    class: 'ac-tool',
    dataset: { state: stateName },
  }, head);
  const args = tool.args && typeof tool.args === 'object' ? tool.args : {};
  const argText = Object.entries(args)
    .filter(([, value]) => value !== undefined && value !== null && String(value) !== '')
    .map(([key, value]) => `${key}=${clip(String(value), 52)}`)
    .join('  ');
  if (argText) card.append(el('div', { class: 'ac-tool-args' }, argText));
  if (tool.error) {
    card.append(el('div', { class: 'ac-tool-out is-error' }, String(tool.error)));
  } else {
    const detail = String(tool.output || tool.preview || '');
    if (detail) {
      card.append(el('details', { class: 'ac-tool-details' },
        el('summary', {}, '查看输出'),
        el('pre', {}, clip(detail, 2000)),
      ));
    }
  }
  return card;
}

/* ------------------------------------------------------------------ */
/* composer                                                            */
/* ------------------------------------------------------------------ */

function sendText(view, rawText) {
  if (!view || active !== view) return;
  const text = String(rawText || '').trim();
  if (!text) return;
  if (view.busy) {
    toast('这个 Agent 正在回复，先等它说完或点停止。', 'info');
    return;
  }
  const temp = {
    id: `tmp-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`,
    role: 'user',
    text,
    ts: Date.now(),
    agent_id: view.agentId,
  };
  view.pendingUser = temp;
  upsertMessage(view, temp);
  view.els.input.value = '';
  autosize(view.els.input);
  setBusy(view, true);
  scrollToBottom('smooth');
  api.sendAgentChat(view.agentId, text).then((data) => {
    if (active !== view) return;
    if (data && data.message) upsertMessage(view, data.message);
    if (view.pendingUser) removePendingUser(view);
    if (data && data.busy === false) setBusy(view, false);
  }).catch((error) => {
    if (active !== view) return;
    if (view.pendingUser) removePendingUser(view);
    setBusy(view, false);
    view.els.input.value = text;
    autosize(view.els.input);
    toastError(error);
  });
}

function removePendingUser(view) {
  const temp = view.pendingUser;
  view.pendingUser = null;
  if (!temp) return;
  const node = view.seen.get(temp.id);
  if (node) node.remove();
  view.seen.delete(temp.id);
  view.messages = view.messages.filter((message) => message.id !== temp.id);
}

async function cancelTurn(view) {
  if (!view || active !== view) return;
  try {
    const data = await api.cancelAgentChat(view.agentId);
    if (active !== view) return;
    if (data && data.canceled) toast('已请求停止本轮回复', 'info');
    else toast('这轮已经结束了', 'info');
  } catch (error) {
    toastError(error);
  }
}

function setBusy(view, busy) {
  view.busy = Boolean(busy);
  syncComposer();
  scheduleAside(view);
  if (view.busy) {
    if (!view.draft) showTyping(view);
  } else if (view.draft) {
    // The final message (or the stop note) normally arrives first.  If the
    // turn ended without one, freeze the partial card instead of leaving an
    // animated spinner that will never update again.
    if (!view.draft.text && !view.draft.tools.length) clearDraft(view);
    else stopDraft(view, '本轮已结束');
  }
}

function syncComposer() {
  if (!active) return;
  const view = active;
  const busy = view.busy;
  view.els.sendBtn.disabled = busy || view.loading;
  view.els.sendBtn.hidden = busy;
  view.els.stopBtn.hidden = !busy;
  view.els.input.placeholder = busy
    ? 'Agent 正在回复…（可点停止）'
    : '对这个 Agent 说点什么…（Enter 发送）';
  if (view.els.hint) {
    let text = 'Enter 发送 · Shift+Enter 换行 · 高风险动作仍需审批';
    if (state.sse === 'reconnecting') text = '事件流重连中 · 消息仍会发送';
    view.els.hint.textContent = text;
  }
}

function autosize(input) {
  if (!input) return;
  input.style.height = 'auto';
  input.style.height = `${Math.min(input.scrollHeight, 132)}px`;
}

/* ------------------------------------------------------------------ */
/* header / banner / aside                                             */
/* ------------------------------------------------------------------ */

function syncHeader() {
  if (!active) return;
  const agent = active.agent || fallbackAgent(active.agentId);
  const title = document.getElementById('sheet-title');
  const sub = document.getElementById('sheet-sub');
  if (title) title.textContent = agent.display_name || agent.id;
  if (sub) {
    const projectName = active.project ? (active.project.name || active.project.id) : '未绑定项目';
    sub.textContent = `${roleLabel(agent.role)} · ${statusLabel(agent.status)} · ${projectName}`;
  }
  if (active.els.status) {
    active.els.status.dataset.status = String(agent.status || '');
    active.els.status.textContent = statusLabel(agent.status);
  }
  if (active.els.name) active.els.name.textContent = agent.display_name || agent.id;
  if (active.els.meta) active.els.meta.textContent = `${roleLabel(agent.role)} · ${agent.id}`;
  if (active.els.killBtn) active.els.killBtn.hidden = TERMINAL.has(agent.status);
  syncBanner();
}

function syncBanner() {
  if (!active) return;
  const view = active;
  const banner = view.els.banner;
  if (!banner) return;
  const agent = view.agent || {};
  const pending = pendingApprovals(view);
  banner.replaceChildren();
  banner.hidden = false;
  if (pending.length) {
    banner.className = 'ac-banner is-warning';
    banner.append(icon('approvals', 'ico ico-sm'),
      el('span', {}, `${pending.length} 件审批在等这个 Agent：${pending
        .slice(0, 2).map((item) => item.capability).join('、')}${pending.length > 2 ? '…' : ''}`),
      el('button', {
        class: 'text-btn',
        type: 'button',
        onclick: () => { location.hash = '#approvals'; },
      }, '去审批', icon('chevron', 'ico ico-sm')));
    return;
  }
  let note = '';
  if (TERMINAL.has(agent.status)) {
    note = '这个 Agent 已结束；继续对话会以同一角色身份工作，动作仍走审批。';
  } else if (agent.status === 'working' || agent.status === 'waiting') {
    note = '这个 Agent 正在执行任务；这里的对话不会中断它的进程。';
  }
  if (!note && view.project) {
    banner.hidden = true;
    return;
  }
  if (!note && !view.project) note = '未绑定项目：文件/命令工具不可用，仍可以问它问题。';
  if (!note) {
    banner.hidden = true;
    return;
  }
  banner.className = 'ac-banner';
  banner.append(icon('agents', 'ico ico-sm'), el('span', {}, note));
}

function pendingApprovals(view) {
  return (state.approvals || []).filter((item) =>
    item && item.principal === view.agentId);
}

function syncFromState() {
  if (!active) return;
  const view = active;
  const fresh = (state.agents || []).find((agent) => agent.id === view.agentId);
  if (fresh) view.agent = fresh;
  if (!view.project && fresh && fresh.project_id) {
    view.project = (state.projects || []).find((project) => project.id === fresh.project_id) || null;
  } else if (view.project) {
    const updated = (state.projects || []).find((project) => project.id === view.project.id);
    if (updated) view.project = updated;
  }
  syncHeader();
  scheduleAside(view);
  if (fresh && fresh.task_id && view.project) scheduleTasks(view);
}

function scheduleAside(view) {
  if (view.asideFrame) return;
  view.asideFrame = window.requestAnimationFrame(() => {
    view.asideFrame = null;
    if (active === view) syncAside();
  });
}

function syncAside() {
  if (!active) return;
  const view = active;
  const aside = view.els.aside;
  aside.replaceChildren();
  const agent = view.agent || {};
  const project = view.project;

  /* project */
  if (project) {
    aside.append(el('section', { class: 'ac-side-card ac-project' },
      el('div', { class: 'ac-side-head' },
        el('h4', {}, '项目'),
        el('span', { class: 'pill', dataset: { status: project.status || '' } },
          statusLabel(project.status)),
      ),
      el('div', { class: 'ac-side-title', title: project.name || '' },
        project.name || project.id),
      el('div', { class: 'ac-side-sub mono' }, project.id || ''),
      el('div', { class: 'ac-progress' },
        bar(project.progress || 0, { tone: project.status === 'done' ? 'ok' : '' }),
        el('span', { class: 'mono' }, `${Math.round(project.progress || 0)}%`),
      ),
      project.goal ? el('p', { class: 'ac-side-goal' }, clip(project.goal, 180)) : null,
      el('div', { class: 'ac-side-stats' },
        sideStat('待办', project.counts?.todo),
        sideStat('进行', project.counts?.doing, 'is-doing'),
        sideStat('待审', project.counts?.review, 'is-review'),
        sideStat('完成', project.counts?.done, 'is-done'),
      ),
      el('button', {
        class: 'text-btn ac-link',
        type: 'button',
        onclick: () => { location.hash = `#project/${encodeURIComponent(project.id)}`; },
      }, '打开项目', icon('chevron', 'ico ico-sm')),
    ));
  } else {
    aside.append(el('section', { class: 'ac-side-card' },
      el('div', { class: 'ac-side-head' }, el('h4', {}, '项目')),
      el('p', { class: 'ac-side-goal' }, '这个 Agent 未绑定项目。'),
    ));
  }

  /* task */
  const task = view.tasks.find((item) => item.id === agent.task_id) || null;
  aside.append(el('section', { class: 'ac-side-card' },
    el('div', { class: 'ac-side-head' }, el('h4', {}, '当前任务')),
    task
      ? el('div', { class: 'ac-task-now' },
        el('span', { class: 'pill', dataset: { status: task.status || '' } },
          statusLabel(task.status)),
        el('span', { class: 'ac-task-title' }, task.title || task.id),
        el('span', { class: 'ac-side-sub mono' }, task.id),
      )
      : el('p', { class: 'ac-side-goal' },
        agent.task_id ? `任务 ${agent.task_id}（不在当前任务列表）` : '没有绑定任务；可以让它做 workspace 里的具体工作。'),
    view.tasks.length
      ? el('ul', { class: 'ac-task-list' },
        ...view.tasks.slice(0, 7).map((item) => el('li', { class: 'ac-task-row' },
          el('span', {
            class: `status-dot is-${item.status || 'todo'}`,
            'aria-hidden': 'true',
          }),
          el('span', { class: 'ac-task-row-title', title: item.title || '' },
            clip(item.title || item.id, 40)),
          el('span', { class: 'ac-task-row-state' }, statusLabel(item.status)),
        )),
      )
      : null,
  ));

  /* pending approvals */
  const pending = pendingApprovals(view);
  aside.append(el('section', { class: 'ac-side-card' },
    el('div', { class: 'ac-side-head' },
      el('h4', {}, '审批'),
      el('span', { class: 'mono ac-count' }, String(pending.length)),
    ),
    pending.length
      ? el('p', { class: 'ac-side-goal is-warning' },
        `有 ${pending.length} 件操作在等人工放行，Agent 会暂停在工具调用处。`)
      : el('p', { class: 'ac-side-goal' }, '没有待审批的操作。'),
    el('button', {
      class: 'text-btn ac-link',
      type: 'button',
      onclick: () => { location.hash = '#approvals'; },
    }, pending.length ? '去处理审批' : '打开审批中心', icon('chevron', 'ico ico-sm')),
  ));

  /* recent activity */
  const events = recentEvents(view);
  aside.append(el('section', { class: 'ac-side-card' },
    el('div', { class: 'ac-side-head' }, el('h4', {}, '最近活动')),
    events.length
      ? el('ul', { class: 'ac-activity' }, ...events.map((event) => el('li', { class: 'ac-activity-row' },
        el('span', { class: 'mono ac-activity-time' }, fmtTime(event.ts)),
        el('span', { class: 'ac-activity-type' }, event.type || ''),
        el('span', { class: 'ac-activity-msg' }, eventSummary(event)),
      )))
      : el('p', { class: 'ac-side-goal' }, '暂无活动。'),
  ));

  /* agent metadata */
  aside.append(el('section', { class: 'ac-side-card ac-agent-meta' },
    el('div', { class: 'ac-side-head' }, el('h4', {}, 'Agent')),
    metaRow('ID', agent.id),
    metaRow('角色', roleLabel(agent.role)),
    metaRow('状态', statusLabel(agent.status)),
    metaRow('模型', view.model || agent.model || '—'),
    metaRow('心跳', agent.last_seen ? fmtDateTime(agent.last_seen) : '—'),
    view.project?.workspace ? metaRow('工作区', view.project.workspace) : null,
  ));
}

function sideStat(label, value, tone = '') {
  return el('div', { class: `ac-side-stat ${tone}` },
    el('b', {}, String(Number(value) || 0)),
    el('span', {}, label),
  );
}

function metaRow(label, value) {
  return el('div', { class: 'ac-meta-row' },
    el('span', { class: 'ac-meta-label' }, label),
    el('span', { class: 'ac-meta-value mono', title: String(value ?? '') },
      clip(String(value ?? '—'), 60)),
  );
}

function recentEvents(view) {
  const agentId = view.agentId;
  const source = `agent:${agentId}`;
  return (state.events || [])
    .filter((event) => {
      const type = String(event.type || '');
      // Streaming chunks and busy toggles are noise in a compact activity list.
      if (type === 'agent.chat.delta' || type === 'agent.chat.state') return false;
      const data = event.data || {};
      return data.agent_id === agentId || event.source === source;
    })
    .slice(-6)
    .reverse();
}

/* ------------------------------------------------------------------ */
/* tasks refresh                                                       */
/* ------------------------------------------------------------------ */

function scheduleTasks(view) {
  if (view.tasksTimer) return;
  view.tasksTimer = window.setTimeout(() => {
    view.tasksTimer = null;
    if (active === view) refreshTasks(view);
  }, 700);
}

async function refreshTasks(view) {
  const projectId = view.project && view.project.id;
  if (!projectId) return;
  try {
    const data = await api.getTasks(projectId);
    if (active !== view) return;
    view.tasks = Array.isArray(data.tasks) ? data.tasks : [];
    syncAside();
  } catch (error) {
    console.warn('[agentchat] task refresh failed', error);
  }
}

/* ------------------------------------------------------------------ */
/* thread actions                                                      */
/* ------------------------------------------------------------------ */

async function clearThread(view) {
  if (!window.confirm('清空这个 Agent 的对话记录？\n正在进行的回复会被停止，工作区文件不会受影响。')) {
    return;
  }
  try {
    await api.clearAgentChat(view.agentId);
    if (active !== view) return;
    view.messages = [];
    view.seen.clear();
    view.els.log.replaceChildren();
    clearDraft(view);
    view.busy = false;
    syncComposer();
    syncSuggestions(view);
    renderEmptyState(view);
    syncAside();
    toast('对话已清空', 'success');
  } catch (error) {
    toastError(error);
  }
}

async function killAgent(view) {
  const agent = view.agent || {};
  const label = agent.display_name || agent.id;
  if (!window.confirm(`终止 ${label}？\n它的进程会被要求退出；这个对话记录会保留。`)) return;
  try {
    await api.killAgent(view.agentId);
    toast('终止信号已发送', 'success');
    await loadState({ quiet: true }).catch(() => {});
    syncFromState();
  } catch (error) {
    toastError(error);
  }
}

/* ------------------------------------------------------------------ */
/* live events                                                         */
/* ------------------------------------------------------------------ */

function handleEvent(event) {
  if (!active || !event) return;
  const type = String(event.type || '');
  if (!type.startsWith('agent.chat.')) return;
  const data = event.data || {};
  if (data.agent_id && data.agent_id !== active.agentId) return;
  const view = active;
  if (type === 'agent.chat.message') {
    if (!data.id) return;
    upsertMessage(view, data);
    return;
  }
  if (type === 'agent.chat.delta') {
    if (data.typing) {
      showTyping(view, data.message_id);
      return;
    }
    if (!data.delta) return;
    const draft = ensureDraft(view, data.message_id || null);
    draft.text += String(data.delta);
    draft.typing = false;
    renderDraft(view);
    if (isNearBottom(view)) scrollToBottom('auto');
    return;
  }
  if (type === 'agent.chat.tool') {
    updateTool(view, data);
    return;
  }
  if (type === 'agent.chat.state') {
    if (typeof data.busy === 'boolean') setBusy(view, data.busy);
    return;
  }
  if (type === 'agent.chat.thread' && data.cleared) {
    view.messages = [];
    view.seen.clear();
    view.els.log.replaceChildren();
    clearDraft(view);
    renderEmptyState(view);
  }
}

/* ------------------------------------------------------------------ */
/* scrolling                                                           */
/* ------------------------------------------------------------------ */

function distanceFromBottom(view) {
  const scroll = view.els.scroll;
  return scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight;
}

function isNearBottom(view) {
  return distanceFromBottom(view) < 140;
}

function scrollToBottom(behavior = 'auto') {
  if (!active) return;
  const view = active;
  window.requestAnimationFrame(() => {
    const scroll = view.els.scroll;
    if (behavior === 'auto') {
      const previous = scroll.style.scrollBehavior;
      scroll.style.scrollBehavior = 'auto';
      scroll.scrollTop = scroll.scrollHeight;
      scroll.style.scrollBehavior = previous;
    } else {
      scroll.scrollTo({ top: scroll.scrollHeight, behavior });
    }
    updateScrollButton(view);
  });
}

function updateScrollButton(view) {
  if (!view || !view.els.scrollBottom) return;
  view.els.scrollBottom.hidden = distanceFromBottom(view) < 160;
}
