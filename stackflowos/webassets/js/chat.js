/**
 * chat.js — the conversation surface.
 *
 * Responsibilities: render history, optimistic user messages, the assistant
 * streaming bubble / typing indicator, quick suggestions and the composer.
 * Scrolling only follows when the operator is already near the bottom, so
 * reading history is never yanked away.
 */

import { api } from './api.js';
import {
  addLocalChat,
  on,
  removeChatMessage,
  state,
} from './store.js';
import {
  el,
  fmtDateTime,
  fmtTime,
  icon,
  richText,
  spinner,
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
    renderEmptyIfNeeded();
  }
  updateSub();
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
  els.scroll.scrollTop = els.scroll.scrollHeight;
  window.requestAnimationFrame(() => scrollToBottom('auto'));
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
      el('h3', {}, '从一句话开始'),
      el('p', {}, '告诉系统要做什么，它会创建项目、派工并实时汇报。'),
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
        el('span', {}, '正在思考'),
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
    els.sub.textContent = '系统助手 · 发送中';
    return;
  }
  const config = state.config;
  const provider = config?.llm?.provider;
  if (provider === 'openai') {
    const model = config?.llm?.model || 'OpenAI 兼容';
    els.sub.textContent = `系统助手 · ${model}`;
  } else if (provider === 'mock') {
    els.sub.textContent = '系统助手 · 离线模型';
  } else {
    els.sub.textContent = '系统助手 · 待命';
  }
}

function updateConnectionLabel(status = state.sse) {
  if (!els.sub || state.loaded === false) return;
  if (status === 'reconnecting') els.sub.textContent = '系统助手 · 事件流重连中';
  else if (status === 'live') updateSub();
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
    els.scroll.scrollTo({ top: els.scroll.scrollHeight, behavior });
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

function hideLoading() {
  if (!els.loading) return;
  els.loading.hidden = true;
}

function showLoading() {
  if (!els.loading) return;
  els.loading.hidden = false;
  els.loading.replaceChildren(spinner(), el('span', {}, '正在载入会话…'));
}
