/**
 * chat.js — the conversation surface.
 *
 * Responsibilities: render history, optimistic user messages, the assistant
 * streaming bubble / typing indicator, quick suggestions and the composer.
 * Scrolling only follows when the operator is already near the bottom, so
 * reading history is never yanked away.
 */

import { api } from './api.js';
import { openApp } from './apps.js';
import {
  addLocalChat,
  on,
  removeChatMessage,
  resetStream,
  state,
} from './store.js';
import { t } from './i18n.js';
import {
  el,
  fmtDateTime,
  fmtTime,
  icon,
  richText,
  spinner,
  statusLabel,
  toastError,
} from './ui.js';

const SUGGESTIONS = [
  { label: '创建一个项目：命令行贪吃蛇', text: '创建一个项目：做一个命令行贪吃蛇游戏' },
  { label: '现在进度怎么样？', text: '现在的进度怎么样？' },
  { label: '有哪些待审批？', text: '有哪些待审批？' },
  { label: '你能做什么？', text: '介绍一下你能做什么' },
];

/** @type {Record<string, HTMLElement>} */
const els = {};
/** @type {Map<string, HTMLElement>} */
const rendered = new Map();
let initialRenderDone = false;
let sending = false;
let typingNode = null;
let streamNode = null;
let streamBubble = null;

export function initChat() {
  els.log = document.getElementById('chat-log');
  els.scroll = document.getElementById('chat-scroll');
  els.loading = document.getElementById('chat-loading');
  els.form = document.getElementById('composer-form');
  els.input = document.getElementById('composer-input');
  els.send = document.getElementById('send-btn');
  els.suggestions = document.getElementById('suggestions');
  els.scrollBottom = document.getElementById('scroll-bottom');
  els.sub = document.getElementById('chat-sub');
  els.context = document.getElementById('chat-context');
  if (!els.log || !els.form) return;

  renderSuggestions();
  els.form.addEventListener('submit', (event) => {
    event.preventDefault();
    sendMessage(els.input.value);
  });
  els.input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      els.form.requestSubmit();
    }
  });
  els.input.addEventListener('input', autoGrow);
  els.scroll.addEventListener('scroll', onScroll, { passive: true });
  els.scrollBottom.addEventListener('click', () => scrollToBottom('smooth'));

  on('state', onState);
  on('chat', onChat);
  on('sse', updateConnectionLabel);
  updateConnectionLabel();
}

/* ------------------------------------------------------------------ */
/* state & messages                                                    */
/* ------------------------------------------------------------------ */

function onState() {
  if (!state.loaded) return;
  hideLoading();
  if (!initialRenderDone) {
    initialRenderDone = true;
    renderAll();
    renderEmptyIfNeeded();
  } else {
    syncMessages();
    renderEmptyIfNeeded();
  }
  updateSub();
  updateContext();
}

/**
 * Fold messages that arrived through a full snapshot (reconnect, polling) into
 * the existing DOM without re-rendering history.  This is what keeps a final
 * assistant message after a dropped SSE connection from being lost, and what
 * de-duplicates it when the durable `chat.message` shows up as well.
 */
function syncMessages() {
  const messages = [...state.chat].sort((a, b) => (a.ts || 0) - (b.ts || 0));
  let appended = false;
  for (const message of messages) {
    if (!message || !message.id) continue;
    const existing = rendered.get(message.id);
    if (!existing) {
      const node = buildMessage(message, { instant: true });
      rendered.set(message.id, node);
      els.log.querySelector('.chat-empty-wrap')?.remove();
      els.log.append(node);
      appended = true;
      continue;
    }
    if (message.text && existing.dataset.text !== message.text) {
      const bubble = existing.querySelector('.bubble');
      if (bubble) {
        bubble.replaceChildren(richText(message.text));
        existing.dataset.text = message.text;
      }
    }
  }
  if (state.stream.active) {
    const started = state.stream.startedAt || 0;
    const final = messages.some((message) => message.role === 'assistant'
      && Number(message.ts || 0) >= started);
    if (final) {
      // The durable message is here; the ephemeral draft is now a duplicate.
      clearStream();
      resetStream();
    }
  }
  if (appended && isNearBottom()) scrollToBottom('auto');
}

function onChat(detail) {
  if (!detail) return;
  if (detail.kind === 'typing') {
    showTyping(Boolean(state.typing));
    return;
  }
  if (detail.kind === 'stream') {
    showTyping(false);
    updateStream();
    return;
  }
  if (detail.kind === 'stream-reset') {
    showTyping(false);
    clearStream();
    return;
  }
  if (detail.kind === 'final') {
    showTyping(false);
    clearStream();
    const near = isNearBottom();
    appendMessage(detail.message, { near });
    if (near) scrollToBottom('smooth');
    return;
  }
  if (detail.kind === 'message') {
    const message = detail.message;
    const near = isNearBottom();
    if (message.role === 'assistant' && state.stream.active) clearStream();
    appendMessage(message, { near });
    if (message.role === 'user' || near) scrollToBottom('smooth');
  }
}

function renderAll() {
  rendered.clear();
  removeTyping();
  clearStream();
  els.log.replaceChildren();
  const messages = [...state.chat].sort((a, b) => (a.ts || 0) - (b.ts || 0));
  const fragment = document.createDocumentFragment();
  for (const message of messages) {
    const node = buildMessage(message, { instant: true });
    rendered.set(message.id, node);
    fragment.append(node);
  }
  els.log.append(fragment);
  // A refresh must land at the bottom instantly. Do not let any CSS
  // scroll-behavior (or a future style) animate the whole history past the
  // operator's eyes.
  const previousBehavior = els.scroll.style.scrollBehavior;
  els.scroll.style.scrollBehavior = 'auto';
  els.scroll.scrollTop = els.scroll.scrollHeight;
  els.scroll.style.scrollBehavior = previousBehavior;
  window.requestAnimationFrame(() => {
    const behavior = els.scroll.style.scrollBehavior;
    els.scroll.style.scrollBehavior = 'auto';
    els.scroll.scrollTop = els.scroll.scrollHeight;
    els.scroll.style.scrollBehavior = behavior;
    updateScrollButton();
  });
}

function renderEmptyIfNeeded() {
  const existing = els.log.querySelector('.chat-empty-wrap');
  if (state.chat.length > 0) {
    existing?.remove();
    return;
  }
  if (!state.loaded || existing) return;

  const prompts = SUGGESTIONS.slice(0, 3).map((suggestion) =>
    el('button', {
      class: 'suggestion-chip',
      type: 'button',
      onclick: () => sendPrompt(suggestion.text),
    }, suggestion.label),
  );
  const node = el('li', { class: 'chat-empty-wrap' },
    el('div', { class: 'chat-empty' },
      el('div', { class: 'empty-glyph' }, icon('chat')),
      el('h3', {}, t('chat.empty.title')),
      el('p', {}, t('chat.empty.text')),
      el('div', { class: 'suggestion-row' }, ...prompts),
    ),
  );
  els.log.append(node);
}

function appendMessage(message, { near = true } = {}) {
  if (!message || !message.id) return;
  if (rendered.has(message.id)) {
    const existing = rendered.get(message.id);
    if (existing && message.text && existing.dataset.text !== message.text) {
      const bubble = existing.querySelector('.bubble');
      if (bubble) {
        bubble.replaceChildren(richText(message.text));
        existing.dataset.text = message.text;
      }
    }
    return;
  }
  const node = buildMessage(message);
  rendered.set(message.id, node);
  els.log.querySelector('.chat-empty-wrap')?.remove();
  els.log.append(node);
  if (!near) updateScrollButton();
}

function buildMessage(message, { instant = false } = {}) {
  const isUser = message.role === 'user';
  const avatar = el('span', { class: 'msg-avatar', 'aria-hidden': 'true' },
    isUser ? '我' : icon('brand', 'ico ico-sm'));

  const bubble = el('div', { class: 'bubble' });
  bubble.append(richText(message.text || ''));

  const meta = el('div', { class: 'msg-meta' },
    el('span', {}, fmtTime(message.ts)),
    message.agent_id ? el('span', { class: 'mini-tag', title: message.agent_id }, 'SYSTEM') : null,
  );

  const node = el('li', {
    class: `msg ${isUser ? 'is-user' : 'is-assistant'}${instant ? ' is-instant' : ''}`,
    dataset: { text: message.text || '' },
  },
  avatar,
  el('div', { class: 'msg-body' }, bubble, meta),
  );
  node.title = fmtDateTime(message.ts);
  return node;
}

/* ------------------------------------------------------------------ */
/* typing + streaming                                                  */
/* ------------------------------------------------------------------ */

function showTyping(show) {
  if (!show) {
    removeTyping();
    return;
  }
  if (typingNode) return;
  const near = isNearBottom();
  typingNode = el('li', { class: 'msg is-assistant', 'aria-label': '助手正在思考' },
    el('span', { class: 'msg-avatar', 'aria-hidden': 'true' }, icon('brand', 'ico ico-sm')),
    el('div', { class: 'msg-body' },
      el('div', { class: 'bubble typing-bubble' },
        el('span', {}, t('chat.typing')),
        el('span', { class: 'typing-dots', 'aria-hidden': 'true' }, el('i'), el('i'), el('i')),
      ),
    ),
  );
  els.log.append(typingNode);
  if (near) scrollToBottom('smooth');
}

function removeTyping() {
  if (typingNode) {
    typingNode.remove();
    typingNode = null;
  }
}

function updateStream() {
  if (!state.stream.active) return;
  if (!streamNode) {
    const near = isNearBottom();
    streamBubble = el('div', { class: 'bubble is-streaming' });
    streamNode = el('li', { class: 'msg is-assistant msg-stream' },
      el('span', { class: 'msg-avatar', 'aria-hidden': 'true' }, icon('brand', 'ico ico-sm')),
      el('div', { class: 'msg-body' }, streamBubble),
    );
    els.log.append(streamNode);
    if (near) scrollToBottom('auto');
  }
  streamBubble.replaceChildren(richText(state.stream.text));
  if (isNearBottom()) scrollToBottom('auto');
}

function clearStream() {
  if (streamNode) {
    streamNode.remove();
    streamNode = null;
    streamBubble = null;
  }
}

/* ------------------------------------------------------------------ */
/* composer                                                            */
/* ------------------------------------------------------------------ */

function renderSuggestions() {
  if (!els.suggestions) return;
  els.suggestions.replaceChildren();
  for (const suggestion of SUGGESTIONS.slice(0, 3)) {
    els.suggestions.append(el('button', {
      class: 'suggestion-chip',
      type: 'button',
      title: suggestion.text,
      onclick: () => sendPrompt(suggestion.text),
    }, icon('chat', 'ico ico-xs'), suggestion.label));
  }
}

function autoGrow() {
  if (!els.input) return;
  els.input.style.height = 'auto';
  els.input.style.height = `${Math.min(els.input.scrollHeight, 132)}px`;
}

export function focusComposer() {
  if (!els.input) return;
  els.input.focus();
  const length = els.input.value.length;
  els.input.setSelectionRange(length, length);
}

/** Send a quick suggestion straight from the panel/empty state. */
export function sendPrompt(text) {
  if (!els.input) return;
  els.input.value = String(text || '');
  autoGrow();
  els.form.requestSubmit();
}

async function sendMessage(rawText) {
  const text = String(rawText || '').trim();
  if (!text || sending) return;
  sending = true;
  if (els.send) els.send.disabled = true;

  const tempId = `tmp-${Date.now()}-${Math.random().toString(16).slice(2, 6)}`;
  const temp = { id: tempId, role: 'user', text, ts: Date.now() };
  addLocalChat(temp);
  els.input.value = '';
  autoGrow();
  scrollToBottom('smooth');
  updateSub();

  try {
    const data = await api.sendChat(text);
    removeChatMessage(tempId);
    rendered.get(tempId)?.remove();
    rendered.delete(tempId);
    if (data && data.message) addLocalChat(data.message);
    else if (data && data.message_id) addLocalChat({ id: data.message_id, role: 'user', text, ts: Date.now() });
  } catch (error) {
    removeChatMessage(tempId);
    rendered.get(tempId)?.remove();
    rendered.delete(tempId);
    toastError(error);
  } finally {
    sending = false;
    if (els.send) els.send.disabled = false;
    updateSub();
    focusComposer();
  }
}

function updateSub() {
  if (!els.sub) return;
  if (sending) {
    els.sub.textContent = t('chat.sub.sending');
    return;
  }
  const config = state.config;
  const provider = config?.llm?.provider;
  if (provider === 'openai') {
    const model = config?.llm?.model || 'OpenAI';
    els.sub.textContent = t('chat.sub.openai', { model });
  } else if (provider === 'mock') {
    els.sub.textContent = t('chat.sub.mock');
  } else {
    els.sub.textContent = t('chat.sub.waiting');
  }
}

function updateConnectionLabel(status = state.sse) {
  if (!els.sub || state.loaded === false) return;
  if (status === 'reconnecting') els.sub.textContent = t('chat.sub.reconnecting');
  else if (status === 'live') updateSub();
}

function updateContext() {
  if (!els.context) return;
  if (!state.loaded) {
    els.context.hidden = true;
    return;
  }
  const openProjects = [...state.projects].filter((project) =>
    ['active', 'review', 'degraded'].includes(project.status));
  const project = openProjects[0];
  const online = state.units.filter((unit) =>
    ['online', 'suspect', 'ready', 'degraded', 'draining'].includes(unit.state || unit.status)).length;
  const activeAgents = project
    ? state.agents.filter((agent) => agent.project_id === project.id &&
        ['working', 'waiting', 'idle', 'spawning'].includes(agent.status)).length
    : 0;
  const nodes = [el('span', { class: 'ctx-label' }, t('chat.ctx.label'))];
  if (project) {
    nodes.push(el('button', {
      class: 'ctx-chip',
      type: 'button',
      title: `打开项目 ${project.name}`,
      onclick: () => openApp('project', project.id),
    },
    el('span', { class: 'ctx-dot', 'aria-hidden': 'true' }),
    `${project.name} · ${statusLabel(project.status)} · ${Math.round(project.progress || 0)}%`));
    const counts = project.counts || {};
    nodes.push(el('span', {}, t('chat.ctx.review_agents', {
      review: Number(counts.review) || 0, agents: activeAgents,
    })));
    if (openProjects.length > 1) {
      nodes.push(el('span', {}, t('chat.ctx.more_projects', { n: openProjects.length - 1 })));
    }
  } else {
    nodes.push(el('span', {}, t('chat.ctx.all')));
  }
  nodes.push(el('span', {}, t('chat.ctx.units', { online, total: state.units.length })));
  nodes.push(el('span', {}, t('chat.ctx.approvals', { n: state.approvals.length })));
  els.context.replaceChildren(...nodes);
  els.context.hidden = false;
}

/* ------------------------------------------------------------------ */
/* scroll                                                              */
/* ------------------------------------------------------------------ */

function distanceFromBottom() {
  if (!els.scroll) return 0;
  return els.scroll.scrollHeight - els.scroll.scrollTop - els.scroll.clientHeight;
}

function isNearBottom() {
  return distanceFromBottom() < 130;
}

function scrollToBottom(behavior = 'auto') {
  if (!els.scroll) return;
  window.requestAnimationFrame(() => {
    if (behavior === 'auto') {
      // Explicit instant scroll; CSS smooth scrolling is intentionally not
      // used for layout/refresh-driven jumps.
      const previous = els.scroll.style.scrollBehavior;
      els.scroll.style.scrollBehavior = 'auto';
      els.scroll.scrollTop = els.scroll.scrollHeight;
      els.scroll.style.scrollBehavior = previous;
    } else {
      els.scroll.scrollTo({ top: els.scroll.scrollHeight, behavior });
    }
    updateScrollButton();
  });
}

function onScroll() {
  updateScrollButton();
}

function updateScrollButton() {
  if (!els.scrollBottom) return;
  els.scrollBottom.hidden = distanceFromBottom() < 140;
}

/** Re-render the copy that lives outside the static markup (language change). */
export function rerenderChat() {
  renderSuggestions();
  updateSub();
  updateContext();
  els.log.querySelector('.chat-empty-wrap')?.remove();
  renderEmptyIfNeeded();
  if (typingNode) {
    removeTyping();
    if (state.typing) showTyping(true);
  }
}

function hideLoading() {
  if (!els.loading) return;
  els.loading.hidden = true;
}

function showLoading() {
  if (!els.loading) return;
  els.loading.hidden = false;
  els.loading.replaceChildren(spinner(), el('span', {}, '正在载入会话…'));
}
