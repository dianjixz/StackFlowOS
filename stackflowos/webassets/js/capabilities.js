/**
 * capabilities.js — skills, plugins and MCP servers inside Settings → Capabilities.
 *
 * Every section loads independently and degrades on its own:
 *   * endpoint missing (404/405/501) → calm "未启用" note;
 *   * permission refusal (403)      → "无权查看" note;
 *   * anything else                 → inline error note + retry button.
 * Write controls (enable/disable, load/unload, connect/disconnect, call) are
 * hidden for a known `viewer` session.
 */

import { api, isForbidden, isUnsupported } from './api.js';
import { canWrite } from './session.js';
import { t } from './i18n.js';
import {
  appNote,
  clip,
  el,
  icon,
  loadingInline,
  select,
  spinner,
  statusPill,
  textarea,
  toast,
  toastError,
  withBusy,
} from './ui.js';
import { state } from './store.js';

const view = {
  skills: null,
  plugins: null,
  servers: null,
  loading: false,
};

function rowsOf(data, ...keys) {
  if (Array.isArray(data)) return data;
  if (!data || typeof data !== 'object') return [];
  for (const key of keys) {
    if (Array.isArray(data[key])) return data[key];
  }
  return [];
}

function errorKind(error) {
  if (isUnsupported(error)) return 'unavailable';
  if (isForbidden(error)) return 'forbidden';
  return 'error';
}

function noteFor(kind, unavailableKey, error) {
  if (kind === 'unavailable') return appNote(t(unavailableKey), { iconName: 'alert' });
  if (kind === 'forbidden') return appNote(t('session.forbidden'), { warn: true });
  return appNote(error?.message || String(error), { warn: true });
}

/* ================================================================== */
/* pane                                                                */
/* ================================================================== */

export function renderCapabilitiesPane() {
  const wrap = el('div', { class: 'capability-pane' });
  const toolbar = el('div', { class: 'app-toolbar' },
    el('div', { class: 'toolbar-left' },
      el('span', { class: 'app-count' }, t('cap.title'))),
  );
  const refresh = el('button', { class: 'btn btn-sm btn-ghost', type: 'button' },
    icon('refresh', 'ico ico-sm'), t('common.refresh'));
  toolbar.querySelector('.toolbar-left').after(el('div', { class: 'toolbar-right' }, refresh));
  wrap.append(toolbar);
  wrap.append(appNote(t('cap.hint'), { iconName: 'check' }));

  const skills = capabilityCard(text('cap.skills.title'), t('cap.skills.hint'));
  const plugins = capabilityCard(t('cap.plugins.title'), t('cap.plugins.hint'));
  const servers = capabilityCard(t('cap.mcp.title'), t('cap.mcp.hint'));
  wrap.append(skills.card, plugins.card, servers.card);

  const loadAll = async ({ force = false } = {}) => {
    await Promise.all([
      loadSkills(skills, { force }),
      loadPlugins(plugins, { force }),
      loadMcp(servers, { force }),
    ]);
  };
  refresh.addEventListener('click', () => withBusy(refresh, () => loadAll({ force: true })));
  loadAll();
  return wrap;
}

function text(key) {
  return t(key);
}

function capabilityCard(title, hint) {
  const body = el('div', { class: 'cap-body' });
  const count = el('span', { class: 'cap-count mono' });
  const card = el('section', { class: 'form-card cap-card' },
    el('div', { class: 'cap-head' },
      el('h3', {}, title),
      count,
    ),
    el('p', { class: 'form-hint' }, hint),
    body,
  );
  const setBody = (...children) => {
    body.replaceChildren(...children);
  };
  const setCount = (value) => { count.textContent = value == null ? '' : String(value); };
  const start = (message) => setBody(loadingInline(message));
  return { card, body, setBody, setCount, start };
}

/* ================================================================== */
/* skills                                                              */
/* ================================================================== */

const PROJECTS = () => [{ value: '', label: t('cap.skills.global') },
  ...state.projects.map((project) => ({ value: project.id, label: project.name || project.id }))];

async function loadSkills(card, { force = false } = {}) {
  if (view.skills && !force) {
    renderSkills(card);
    return;
  }
  card.start(t('cap.loading'));
  try {
    const data = await api.getSkills('');
    view.skills = rowsOf(data, 'skills', 'items');
    card.setCount(view.skills.length);
    renderSkills(card);
  } catch (error) {
    const kind = errorKind(error);
    view.skills = null;
    card.setCount('');
    if (kind === 'unavailable') card.setBody(appNote(t('cap.skills.unavailable'), { iconName: 'alert' }));
    else if (kind === 'forbidden') card.setBody(noteFor(kind, 'cap.skills.unavailable', error));
    else card.setBody(el('div', {}, noteFor(kind, 'cap.skills.unavailable', error), retry(() => loadSkills(card, { force: true }))));
  }
}

function renderSkills(card) {
  const skills = view.skills || [];
  if (!skills.length) {
    card.setBody(el('p', { class: 'field-hint' }, t('cap.skills.empty')));
    return;
  }
  const writable = canWrite();
  const list = el('div', { class: 'cap-list' });
  for (const skill of skills) {
    const id = String(skill.id || skill.name || '');
    const invalid = skill.valid === false || skill.valid === 'invalid' || Boolean(skill.error);
    const scopes = skill.enabled_in || skill.enabled_projects || skill.projects || [];
    const enabled = skill.enabled === true || skill.enabled_global === true
      || (Array.isArray(scopes) && scopes.length > 0);
    const scopeSelect = select({ 'aria-label': t('cap.skills.scope'), class: 'select cap-scope' },
      PROJECTS());
    if (Array.isArray(scopes) && scopes.length) scopeSelect.value = String(scopes[0]);
    const enableBtn = el('button', {
      class: 'btn btn-xs btn-ok', type: 'button',
      hidden: enabled || invalid,
    }, t('cap.skills.enable'));
    const disableBtn = el('button', {
      class: 'btn btn-xs btn-ghost', type: 'button',
      hidden: !enabled || invalid,
    }, t('cap.skills.disable'));
    enableBtn.disabled = !writable;
    disableBtn.disabled = !writable;
    const apply = (nextEnabled) => withBusy(nextEnabled ? enableBtn : disableBtn, async () => {
      try {
        await api.setSkillEnabled(id, nextEnabled, scopeSelect.value);
        toast(`${t(nextEnabled ? 'cap.skills.enable' : 'cap.skills.disable')} · ${id}`, 'success');
        await loadSkills(card, { force: true });
      } catch (error) {
        toastError(error);
      }
    });
    enableBtn.addEventListener('click', () => apply(true));
    disableBtn.addEventListener('click', () => apply(false));

    list.append(el('div', { class: 'cap-row' },
      el('div', { class: 'cap-row-main' },
        el('div', { class: 'cap-row-title' },
          el('span', { class: 'cap-name' }, skill.name || id),
          el('span', { class: 'mono cap-id' }, id),
          invalid
            ? el('span', { class: 'pill', dataset: { status: 'failed' } }, t('cap.skills.invalid'))
            : el('span', { class: 'pill', dataset: { status: 'ready' } }, t('cap.skills.valid')),
          statusPill(enabled ? 'ready' : 'disabled'),
        ),
        skill.description ? el('div', { class: 'cap-desc' }, clip(skill.description, 200)) : null,
        skill.error ? el('div', { class: 'cap-error' }, String(skill.error)) : null,
        skill.path ? el('div', { class: 'cap-path mono' }, String(skill.path)) : null,
        Array.isArray(scopes) && scopes.length
          ? el('div', { class: 'cap-scopes' },
            t('cap.skills.enabled_in', { scopes: scopes.join(', ') }))
          : null,
      ),
      el('div', { class: 'cap-row-side' }, scopeSelect, enableBtn, disableBtn),
    ));
  }
  card.setBody(list);
}

/* ================================================================== */
/* plugins                                                             */
/* ================================================================== */

async function loadPlugins(card, { force = false } = {}) {
  if (view.plugins && !force) {
    renderPlugins(card);
    return;
  }
  card.start(t('cap.loading'));
  try {
    const data = await api.getPlugins();
    view.plugins = rowsOf(data, 'plugins', 'items');
    card.setCount(view.plugins.length);
    renderPlugins(card);
  } catch (error) {
    const kind = errorKind(error);
    view.plugins = null;
    card.setCount('');
    if (kind === 'unavailable') card.setBody(appNote(t('cap.plugins.unavailable'), { iconName: 'alert' }));
    else if (kind === 'forbidden') card.setBody(noteFor(kind, 'cap.plugins.unavailable', error));
    else card.setBody(el('div', {}, noteFor(kind, 'cap.plugins.unavailable', error), retry(() => loadPlugins(card, { force: true }))));
  }
}

function renderPlugins(card) {
  const plugins = view.plugins || [];
  if (!plugins.length) {
    card.setBody(el('p', { class: 'field-hint' }, t('cap.plugins.empty')));
    return;
  }
  const writable = canWrite();
  const list = el('div', { class: 'cap-list' });
  for (const plugin of plugins) {
    const id = String(plugin.id || plugin.name || plugin.unit_id || '');
    const loaded = Boolean(plugin.loaded ?? plugin.active
      ?? plugin.status === 'loaded' ?? plugin.state === 'loaded');
    const trusted = plugin.trusted !== false && plugin.allow_untrusted !== true;
    const contributes = plugin.contributes || {};
    const contributeBits = [
      ...(contributes.tools || []).map((name) => `tool:${name}`),
      ...(contributes.skills || []).map((name) => `skill:${name}`),
    ];
    const toggle = el('button', {
      class: `btn btn-xs ${loaded ? 'btn-ghost' : 'btn-primary'}`,
      type: 'button',
    }, loaded ? t('cap.plugins.unload') : t('cap.plugins.load'));
    toggle.disabled = !writable;
    toggle.addEventListener('click', () => withBusy(toggle, async () => {
      try {
        if (loaded) await api.unloadPlugin(id);
        else await api.loadPlugin(id);
        toast(`${loaded ? t('cap.plugins.unload') : t('cap.plugins.load')} · ${id}`, 'success');
        await loadPlugins(card, { force: true });
      } catch (error) {
        toastError(error);
      }
    }));
    list.append(el('div', { class: 'cap-row' },
      el('div', { class: 'cap-row-main' },
        el('div', { class: 'cap-row-title' },
          el('span', { class: 'cap-name' }, plugin.name || id),
          el('span', { class: 'mono cap-id' }, id),
          plugin.version ? el('span', { class: 'tag' }, `v${plugin.version}`) : null,
          el('span', { class: 'pill', dataset: { status: loaded ? 'ready' : 'disabled' } },
            loaded ? t('cap.plugins.loaded') : t('cap.plugins.not_loaded')),
          el('span', { class: 'tag' }, trusted ? t('cap.plugins.trusted') : t('cap.plugins.untrusted')),
        ),
        plugin.description ? el('div', { class: 'cap-desc' }, clip(plugin.description, 200)) : null,
        plugin.error ? el('div', { class: 'cap-error' }, String(plugin.error)) : null,
        contributeBits.length
          ? el('div', { class: 'cap-chips' },
            ...contributeBits.map((value) => el('span', { class: 'ability-pill' }, value)))
          : null,
      ),
      el('div', { class: 'cap-row-side' }, toggle),
    ));
  }
  card.setBody(list);
}

/* ================================================================== */
/* MCP servers                                                         */
/* ================================================================== */

async function loadMcp(card, { force = false } = {}) {
  if (view.servers && !force) {
    renderMcp(card);
    return;
  }
  card.start(t('cap.loading'));
  try {
    const data = await api.getMcpServers();
    view.servers = rowsOf(data, 'servers', 'items');
    card.setCount(view.servers.length);
    renderMcp(card);
  } catch (error) {
    const kind = errorKind(error);
    view.servers = null;
    card.setCount('');
    if (kind === 'unavailable') card.setBody(appNote(t('cap.mcp.unavailable'), { iconName: 'alert' }));
    else if (kind === 'forbidden') card.setBody(noteFor(kind, 'cap.mcp.unavailable', error));
    else card.setBody(el('div', {}, noteFor(kind, 'cap.mcp.unavailable', error), retry(() => loadMcp(card, { force: true }))));
  }
}

function renderMcp(card) {
  const servers = view.servers || [];
  if (!servers.length) {
    card.setBody(el('p', { class: 'field-hint' }, t('cap.mcp.empty')));
    return;
  }
  const writable = canWrite();
  const list = el('div', { class: 'cap-list' });
  for (const server of servers) {
    const name = String(server.name || server.id || '');
    const connected = Boolean(server.connected === true
      || server.state === 'ready' || server.state === 'starting');
    const toggle = el('button', {
      class: `btn btn-xs ${connected ? 'btn-ghost' : 'btn-primary'}`,
      type: 'button',
    }, connected ? t('cap.mcp.disconnect') : t('cap.mcp.connect'));
    toggle.disabled = !writable;
    toggle.addEventListener('click', () => withBusy(toggle, async () => {
      try {
        if (connected) await api.disconnectMcp(name);
        else await api.connectMcp(name);
        toast(`${connected ? t('cap.mcp.disconnect') : t('cap.mcp.connect')} · ${name}`, 'success');
        await loadMcp(card, { force: true });
      } catch (error) {
        toastError(error);
      }
    }));

    const details = el('details', { class: 'cap-mcp-details' },
      el('summary', {}, t('cap.mcp.tools')),
    );
    details.addEventListener('toggle', () => {
      if (details.open && !details.dataset.loaded) {
        details.dataset.loaded = '1';
        loadMcpTools(server, details);
      }
    });

    const serverBits = [
      server.transport ? String(server.transport) : '',
      server.url || server.command || '',
    ].filter(Boolean).join(' · ');
    list.append(el('div', { class: 'cap-row cap-row-mcp' },
      el('div', { class: 'cap-row-main' },
        el('div', { class: 'cap-row-title' },
          el('span', { class: 'cap-name' }, server.name || name),
          serverBits ? el('span', { class: 'mono cap-id' }, clip(serverBits, 80)) : null,
          el('span', { class: 'pill', dataset: { status: connected ? 'ready' : 'disabled' } },
            connected ? t('cap.mcp.connected') : t('cap.mcp.disconnected')),
        ),
        (server.last_error || server.error)
          ? el('div', { class: 'cap-error' }, String(server.last_error || server.error))
          : null,
        details,
      ),
      el('div', { class: 'cap-row-side' }, toggle),
    ));
  }
  card.setBody(list);
}

async function loadMcpTools(server, details) {
  const name = String(server.name || server.id || '');
  const mount = el('div', { class: 'cap-mcp-tools' }, el('div', { class: 'loading-inline' }, spinner(), t('cap.loading')));
  details.append(mount);
  const embedded = Array.isArray(server.tools) ? server.tools : null;
  try {
    const data = await api.getMcpTools(name);
    const tools = rowsOf(data, 'tools', 'items');
    renderTools(server, mount, tools.length ? tools : embedded || []);
  } catch (error) {
    if (embedded) {
      renderTools(server, mount, embedded);
    } else if (isUnsupported(error)) mount.replaceChildren(el('p', { class: 'field-hint' }, t('cap.mcp.tools.unavailable')));
    else if (isForbidden(error)) mount.replaceChildren(el('p', { class: 'field-hint' }, t('session.forbidden')));
    else mount.replaceChildren(el('p', { class: 'field-hint' }, error?.message || String(error)));
  }
}

function renderTools(server, mount, tools) {
  mount.replaceChildren();
  if (!tools.length) {
    mount.append(el('p', { class: 'field-hint' }, t('cap.mcp.tools.empty')));
    return;
  }
  const writable = canWrite();
  const toolSelect = select({ 'aria-label': t('cap.mcp.tool'), class: 'select' },
    [{ value: '', label: t('cap.mcp.select_tool') },
      ...tools.map((tool) => ({
        value: String(tool.name || tool.id || tool),
        label: String(tool.name || tool.id || tool),
      }))]);
  const args = textarea({
    class: 'textarea mono',
    rows: '2',
    placeholder: '{ "key": "value" }',
    'aria-label': t('cap.mcp.args'),
  });
  args.value = '{}';
  const callBtn = el('button', { class: 'btn btn-sm', type: 'button' }, t('cap.mcp.call'));
  const resultBox = el('pre', { class: 'cap-mcp-result', hidden: true });
  callBtn.disabled = !writable;
  callBtn.addEventListener('click', () => withBusy(callBtn, async () => {
    const tool = toolSelect.value;
    if (!tool) {
      toolSelect.focus();
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(args.value || '{}');
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        throw new Error('JSON object expected');
      }
    } catch (error) {
      toast(t('cap.mcp.args.invalid', { message: error.message || String(error) }), 'error');
      args.focus();
      return;
    }
    try {
      const data = await api.callMcp({ server: String(server.name || server.id || ''), tool, args: parsed });
      resultBox.hidden = false;
      let body = '';
      try {
        body = typeof data === 'string' ? data : JSON.stringify(data, null, 2);
      } catch {
        body = String(data);
      }
      if (data && typeof data === 'object' && data.status === 'pending') {
        resultBox.textContent = `${t('cap.mcp.call.need_approval')}\n${body}`;
        toast(t('cap.mcp.call.need_approval'), 'info');
      } else {
        resultBox.textContent = body;
        toast(t('cap.mcp.call.ok'), 'success');
      }
    } catch (error) {
      resultBox.hidden = false;
      if (isForbidden(error) || /approval/i.test(String(error?.code || ''))) {
        resultBox.textContent = t('cap.mcp.call.need_approval');
        toast(t('cap.mcp.call.need_approval'), 'info');
      } else {
        resultBox.textContent = t('cap.mcp.call.failed', { message: error?.message || String(error) });
        toastError(error);
      }
    }
  }));

  const toolInfo = tools.map((tool) => (typeof tool === 'string' ? { name: tool } : tool));
  const toolHint = el('div', { class: 'cap-tool-hints' },
    ...toolInfo.slice(0, 12).map((tool) => el('span', {
      class: 'ability-pill',
      title: tool.description || '',
    }, tool.name || tool.id || 'tool')));
  mount.append(toolHint,
    el('div', { class: 'cap-call-form' },
      el('label', { class: 'cap-call-label' }, t('cap.mcp.tool'), toolSelect),
      el('label', { class: 'cap-call-label' }, t('cap.mcp.args'), args),
      callBtn,
    ),
    resultBox,
  );
}

/* ================================================================== */
/* helpers                                                             */
/* ================================================================== */

function retry(action) {
  const button = el('button', { class: 'btn btn-xs', type: 'button' }, t('common.retry'));
  button.addEventListener('click', () => withBusy(button, action));
  return button;
}
