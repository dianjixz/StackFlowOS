/**
 * main.js — boot the operator console.
 */

import { focusComposer, initChat, rerenderChat } from './chat.js';
import { initAgentChat } from './agentchat.js';
import { initPanel, rerenderPanel } from './panel.js';
import { closeSheet, initApps, openApp, rerenderAfterLanguageChange } from './apps.js';
import { connectSSE, loadState, on, state } from './store.js';
import { initI18n, t } from './i18n.js';
import { initSession } from './session.js';
import { initUnits, rerenderUnits } from './units.js';
import { fmtClock, toast } from './ui.js';

const APP_SHORTCUTS = ['projects', 'agents', 'units', 'approvals', 'events', 'settings'];

function initClock() {
  const node = document.getElementById('clock');
  const tick = () => {
    const now = Date.now();
    if (node) {
      node.textContent = fmtClock(now);
      node.setAttribute('datetime', new Date(now).toISOString());
      node.title = new Date(now).toLocaleString('zh-CN');
    }
    updateTitle();
  };
  tick();
  window.setInterval(tick, 10000);
}

function updateTitle() {
  const sys = state.sys;
  if (!sys) return;
  document.title = `${sys.name || 'StackFlowOS'} · ${sys.host || '操作台'}`;
}

function initConnectionIndicator() {
  const wrap = document.getElementById('sse-state');
  const label = document.getElementById('sse-label');
  const apply = (status) => {
    if (!wrap) return;
    wrap.dataset.state = status;
    if (label) {
      label.textContent = t(`sse.${status}`, {}, status);
    }
    wrap.title = `${t('sse.tip')}: ${label ? label.textContent : status} · lastSeq ${state.lastSeq}`;
  };
  on('sse', apply);
  apply(state.sse);
  return apply;
}

function initMobileTabs() {
  const workspace = document.getElementById('workspace');
  const tabs = [...document.querySelectorAll('[data-mobile-view]')];
  if (!workspace || !tabs.length) return;
  for (const tab of tabs) {
    tab.addEventListener('click', () => {
      const view = tab.dataset.mobileView;
      workspace.dataset.view = view;
      for (const item of tabs) {
        const active = item === tab;
        item.classList.toggle('is-active', active);
        item.setAttribute('aria-selected', String(active));
      }
    });
  }
}

function initDelegatedClicks() {
  document.addEventListener('click', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const opener = target.closest('[data-open-app]');
    if (opener) {
      event.preventDefault();
      openApp(opener.dataset.openApp);
    }
  });
  document.getElementById('topbar-approvals')?.addEventListener('click', () => openApp('approvals'));
}

function initShortcuts() {
  document.addEventListener('keydown', (event) => {
    const active = document.activeElement;
    const typing = active && (
      active.tagName === 'INPUT' ||
      active.tagName === 'TEXTAREA' ||
      active.tagName === 'SELECT' ||
      active.isContentEditable
    );

    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      focusComposer();
      return;
    }
    if (event.key === '/' && !typing && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      focusComposer();
      return;
    }
    if (event.altKey && !event.metaKey && !event.ctrlKey) {
      const index = Number(event.key);
      if (Number.isInteger(index) && index >= 1 && index <= APP_SHORTCUTS.length) {
        event.preventDefault();
        openApp(APP_SHORTCUTS[index - 1]);
        return;
      }
    }
    if (event.key === 'Escape' && !typing) {
      closeSheet();
    }
  });
}

function initErrorReporting() {
  window.addEventListener('error', (event) => {
    console.error('[ui] uncaught error', event.error || event.message);
  });
  window.addEventListener('unhandledrejection', (event) => {
    console.error('[ui] unhandled rejection', event.reason);
  });
}

async function boot() {
  initErrorReporting();
  initI18n();
  initChat();
  initAgentChat();
  initPanel();
  initApps();
  initSession();
  initUnits();
  initMobileTabs();
  initDelegatedClicks();
  initShortcuts();
  initClock();
  const applySse = initConnectionIndicator();

  on('lang', () => {
    rerenderChat();
    rerenderPanel();
    rerenderAfterLanguageChange();
    rerenderUnits();
    applySse(state.sse);
  });

  // `?no-sse=1` is a deterministic snapshot mode for headless visual checks:
  // it skips the long-lived EventSource so Chrome's virtual time can settle.
  const snapshotMode = new URLSearchParams(location.search).get('no-sse') === '1';

  try {
    await loadState();
  } catch (error) {
    toast(error.message || '无法连接后端', 'error', 8000);
    window.setTimeout(() => {
      loadState({ quiet: true }).catch(() => {});
    }, 4000);
  }
  if (snapshotMode) {
    state.sse = 'snapshot';
    applySse('snapshot');
  } else {
    connectSSE();
  }
}

boot();
