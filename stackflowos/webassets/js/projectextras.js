/**
 * projectextras.js — project detail extras: task dependency graph and the
 * workspace git panel (branch / commits / diff / rollback).
 *
 * Dependency data comes from the real task model (`deps`, a list of task ids);
 * older cards that expose `depends_on` / `blocked_by` are accepted too.
 * The graph is plain SVG created with createElementNS — no library, no build
 * step. It is intentionally small: layered columns by dependency depth and
 * arrows from prerequisite → dependent task, with a text list underneath.
 */

import { api, isForbidden, isUnsupported } from './api.js';
import { canWrite } from './session.js';
import { t } from './i18n.js';
import {
  appNote,
  clip,
  el,
  icon,
  input,
  loadingInline,
  statusPill,
  toast,
  toastError,
  withBusy,
} from './ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';

/* ================================================================== */
/* task dependencies                                                   */
/* ================================================================== */

/** Normalise the several spellings of a task's prerequisite list. */
export function taskDeps(task) {
  if (!task || typeof task !== 'object') return [];
  const raw = task.deps ?? task.depends_on ?? task.blocked_by ?? task.blockedBy ?? [];
  if (Array.isArray(raw)) return raw.map(String).filter(Boolean);
  if (typeof raw === 'string') return raw.split(',').map((value) => value.trim()).filter(Boolean);
  return [];
}

function depKey(task) {
  return String(task?.id || task?.task_id || '');
}

function taskTitle(task) {
  return String(task?.title || task?.name || depKey(task));
}

/** Number of prerequisites that are not `done` yet. */
export function openDepCount(task, tasks = []) {
  const byId = new Map(tasks.map((item) => [depKey(item), item]));
  return taskDeps(task).filter((id) => {
    const dep = byId.get(id);
    return !dep || dep.status !== 'done';
  }).length;
}

/** Badge text for a task card, or '' when it has no dependencies. */
export function depBadgeText(task, tasks = []) {
  const deps = taskDeps(task);
  if (!deps.length) return '';
  const open = openDepCount(task, tasks);
  return open ? `${t('deps.count', { n: deps.length })} · ${t('deps.open', { n: open })}`
    : t('deps.count', { n: deps.length });
}

export function isDependencyBlocked(task, tasks = []) {
  return openDepCount(task, tasks) > 0;
}

function taskTone(task) {
  if (!task) return 'failed';
  return task.status === 'done' ? 'ready'
    : task.status === 'blocked' ? 'failed'
      : task.status === 'review' ? 'degraded' : 'working';
}

function depthMap(tasks) {
  const byId = new Map(tasks.map((task) => [depKey(task), task]));
  const depth = new Map();
  const visiting = new Set();

  const compute = (task) => {
    const key = depKey(task);
    if (depth.has(key)) return depth.get(key);
    if (visiting.has(key)) return 0; // cycle: store validation rejects these
    visiting.add(key);
    let value = 0;
    for (const depId of taskDeps(task)) {
      const dep = byId.get(depId);
      if (dep) value = Math.max(value, compute(dep) + 1);
    }
    visiting.delete(key);
    depth.set(key, value);
    return value;
  };
  for (const task of tasks) compute(task);
  return { depth, byId };
}

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, String(value));
  }
  return node;
}

function truncate(text, max) {
  const value = String(text);
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/**
 * Render the dependency edges as a layered SVG.
 * @param {any[]} tasks
 * @param {{onSelect?:(taskId:string)=>void, selectedId?:string|null}} [options]
 */
export function buildTaskGraph(tasks, { onSelect = null, selectedId = null } = {}) {
  const relevant = tasks.filter((task) => taskDeps(task).length
    || tasks.some((other) => taskDeps(other).includes(depKey(task))));
  if (!relevant.length) {
    return el('p', { class: 'field-hint' }, t('deps.empty'));
  }
  const { depth, byId } = depthMap(relevant);
  const columns = new Map();
  for (const task of relevant) {
    const column = depth.get(depKey(task)) || 0;
    if (!columns.has(column)) columns.set(column, []);
    columns.get(column).push(task);
  }
  const maxColumn = Math.max(...columns.keys(), 0);
  const maxRows = Math.max(...[...columns.values()].map((list) => list.length), 1);
  const nodeW = 168;
  const nodeH = 38;
  const gapX = 54;
  const gapY = 16;
  const width = 40 + (maxColumn + 1) * (nodeW + gapX);
  const height = 30 + maxRows * (nodeH + gapY);
  const seq = buildTaskGraph.seq = (buildTaskGraph.seq || 0) + 1;
  const markerId = `task-dep-arrow-${seq}`;

  const svg = svgEl('svg', {
    class: 'task-dep-graph',
    viewBox: `0 0 ${width} ${height}`,
    role: 'img',
    'aria-label': t('deps.graph.aria'),
    style: `max-width:${width}px`,
  });
  const defs = svgEl('defs');
  const marker = svgEl('marker', {
    id: markerId, viewBox: '0 0 10 10', refX: '9', refY: '5',
    markerWidth: '6', markerHeight: '6', orient: 'auto-start-reverse',
  });
  marker.append(svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: 'var(--text-3)' }));
  defs.append(marker);
  svg.append(defs);

  const positions = new Map();
  const ordered = [...columns.keys()].sort((a, b) => a - b);
  for (const column of ordered) {
    const list = columns.get(column);
    list.forEach((task, index) => {
      const x = 20 + column * (nodeW + gapX);
      const y = 18 + index * (nodeH + gapY);
      positions.set(depKey(task), { x, y });
    });
  }

  // edges below nodes
  for (const task of relevant) {
    const target = positions.get(depKey(task));
    for (const depId of taskDeps(task)) {
      const dep = byId.get(depId);
      const source = positions.get(depId);
      if (!source || !target) continue;
      const open = !dep || dep.status !== 'done';
      const x1 = source.x + nodeW;
      const y1 = source.y + nodeH / 2;
      const x2 = target.x - 8;
      const y2 = target.y + nodeH / 2;
      const curve = Math.max(24, (x2 - x1) / 2);
      svg.append(svgEl('path', {
        d: `M ${x1} ${y1} C ${x1 + curve} ${y1}, ${x2 - curve} ${y2}, ${x2} ${y2}`,
        fill: 'none',
        stroke: open ? 'var(--danger)' : 'var(--text-4)',
        'stroke-width': open ? '1.5' : '1.1',
        'stroke-dasharray': dep ? null : '4 4',
        'marker-end': `url(#${markerId})`,
        opacity: open ? '0.85' : '0.5',
      }));
    }
  }

  for (const task of relevant) {
    const { x, y } = positions.get(depKey(task));
    const key = depKey(task);
    const selected = key === selectedId;
    const group = svgEl('g', {
      class: `task-dep-node${selected ? ' is-selected' : ''}`,
      transform: `translate(${x} ${y})`,
      tabindex: '0',
      role: 'button',
      'aria-label': `${taskTitle(task)} (${task.status || 'todo'})`,
    });
    group.append(svgEl('rect', {
      width: nodeW, height: nodeH, rx: 9,
      fill: selected ? 'rgba(124,92,255,0.22)' : 'rgba(255,255,255,0.045)',
      stroke: selected ? 'var(--accent)' : 'rgba(255,255,255,0.16)',
      'stroke-width': selected ? '1.6' : '1',
    }));
    group.append(svgEl('circle', {
      cx: 14, cy: nodeH / 2, r: 4,
      fill: taskTone(task) === 'ready' ? 'var(--ok)'
        : taskTone(task) === 'failed' ? 'var(--danger)'
          : taskTone(task) === 'degraded' ? 'var(--warn)' : 'var(--accent-bright)',
    }));
    const title = svgEl('text', {
      x: 26, y: 17, fill: 'var(--text)', 'font-size': '10.5', 'font-weight': '600',
    });
    title.textContent = truncate(taskTitle(task), 19);
    const meta = svgEl('text', {
      x: 26, y: 30, fill: 'var(--text-4)', 'font-size': '8.5', 'font-family': 'var(--mono)',
    });
    meta.textContent = `${key} · ${task.status || 'todo'}`;
    group.append(title, meta);
    if (onSelect) {
      group.addEventListener('click', () => onSelect(key));
      group.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onSelect(key);
        }
      });
    }
    svg.append(group);
  }
  return svg;
}

/**
 * The section appended to project detail: textual edge list + graph.
 * @param {any[]} tasks
 * @param {{onSelect?:(taskId:string)=>void, selectedId?:string|null}} [options]
 */
export function renderTaskDependencySection(tasks = [], options = {}) {
  const withDeps = tasks.filter((task) => taskDeps(task).length);
  const section = el('section', { class: 'task-deps-section' },
    el('div', { class: 'section-head' },
      el('h3', {}, t('deps.title')),
      el('span', { class: 'section-hint' }, t('deps.hint')),
    ),
  );
  if (!withDeps.length) {
    section.append(el('p', { class: 'field-hint' }, t('deps.empty')));
    return section;
  }
  const byId = new Map(tasks.map((task) => [depKey(task), task]));
  const list = el('div', { class: 'task-dep-rows' });
  for (const task of withDeps) {
    const deps = taskDeps(task);
    const open = deps.filter((id) => !byId.get(id) || byId.get(id).status !== 'done');
    const row = el('button', {
      class: `task-dep-row${open.length ? ' is-open' : ''}`,
      type: 'button',
      onclick: () => options.onSelect?.(depKey(task)),
    },
    el('div', { class: 'tdr-main' },
      el('span', { class: 'tdr-title' }, taskTitle(task)),
      el('span', { class: 'mono tdr-id' }, depKey(task)),
      statusPill(task.status || 'todo'),
    ),
    el('div', { class: 'tdr-deps' },
      el('span', { class: 'tdr-kind' }, t('deps.depends_on')),
      ...deps.map((id) => {
        const dep = byId.get(id);
        return el('span', {
          class: `dep-chip${dep && dep.status === 'done' ? '' : ' is-missing'}`,
          title: dep ? `${depKey(dep)} · ${dep.status}` : `${id} (${t('units.deps.missing')})`,
        }, dep ? taskTitle(dep) : id);
      }),
    ),
    );
    list.append(row);
  }
  section.append(list);
  section.append(el('div', { class: 'section-head task-dep-graph-head' },
    el('h4', {}, t('deps.graph.title'))));
  section.append(el('div', { class: 'task-dep-graph-wrap' },
    buildTaskGraph(tasks, options)));
  return section;
}

/* ================================================================== */
/* workspace git                                                       */
/* ================================================================== */

const gitCache = new Map(); // projectId -> {at:number, data:any}
const GIT_TTL = 12000;

function num(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function normalizeGit(data) {
  const payload = data && typeof data === 'object' ? data : {};
  const commits = Array.isArray(payload.commits) ? payload.commits
    : Array.isArray(payload.recent_commits) ? payload.recent_commits
      : Array.isArray(payload.log) ? payload.log : [];
  const branch = payload.branch || payload.current_branch || payload.head || '—';
  const dirty = Boolean(payload.dirty ?? payload.is_dirty ?? payload.uncommitted);
  const changes = Array.isArray(payload.changes) ? payload.changes : [];
  const stat = payload.stat || payload.diff_stat || '';
  const diff = payload.diff || payload.diff_summary || payload.uncommitted_diff || stat;
  const changeText = changes
    .map((change) => (typeof change === 'string' ? change : change?.line || change?.path || ''))
    .filter(Boolean)
    .join('\n');
  return {
    branch,
    dirty,
    commits,
    commit: payload.head || payload.commit || '',
    diff: [String(diff || ''), changeText].filter(Boolean).join('\n'),
    enabled: payload.enabled !== false,
    repo: payload.repo !== false,
    reason: payload.reason || payload.error || '',
    rollback: payload.rollback_available !== false && payload.rollback !== false,
    workspace: payload.workspace || payload.path || '',
  };
}

/**
 * Mount the project git card. Loads asynchronously and keeps a short cache so
 * the live project-detail re-renders do not hammer `git log`.
 */
export function mountGitSection(projectId, { projectName = '' } = {}) {
  const section = el('section', { class: 'git-section' },
    el('div', { class: 'section-head' },
      el('h3', {}, icon('terminal', 'ico ico-sm'), t('git.title')),
      el('span', { class: 'section-hint' }, t('git.hint')),
    ),
  );
  const body = el('div', { class: 'git-body' });
  section.append(body);

  const cached = gitCache.get(projectId);
  if (cached && Date.now() - cached.at < GIT_TTL) {
    body.replaceChildren(renderGit(cached.data, projectId, projectName));
    return section;
  }
  body.append(loadingInline(t('git.loading')));
  api.getProjectGit(projectId)
    .then((data) => {
      gitCache.set(projectId, { at: Date.now(), data });
      body.replaceChildren(renderGit(data, projectId, projectName));
    })
    .catch((error) => {
      if (isUnsupported(error)) {
        body.replaceChildren(appNote(t('git.unavailable'), { iconName: 'alert' }));
      } else if (isForbidden(error)) {
        body.replaceChildren(appNote(t('git.forbidden'), { warn: true }));
      } else {
        body.replaceChildren(appNote(t('git.error', { message: error?.message || String(error) }), { warn: true }));
      }
    });
  return section;
}

function renderGit(data, projectId, projectName) {
  const git = normalizeGit(data);
  if (!git.enabled) {
    return appNote(t('git.unavailable'), { iconName: 'alert' });
  }
  if (!git.repo) {
    return el('div', { class: 'git-card' },
      appNote(git.reason ? t('git.not_repo_reason', { reason: git.reason }) : t('git.not_repo'),
        { iconName: 'alert' }));
  }
  const wrap = el('div', { class: 'git-card' });
  wrap.append(el('div', { class: 'git-head' },
    el('span', { class: 'git-branch' }, icon('projects', 'ico ico-sm'),
      el('span', { class: 'mono' }, String(git.branch))),
    el('span', { class: `git-state ${git.dirty ? 'is-dirty' : 'is-clean'}` },
      git.dirty ? t('git.dirty') : t('git.clean')),
    git.workspace ? el('span', { class: 'git-path mono', title: git.workspace }, clip(git.workspace, 70)) : null,
  ));

  const commits = el('div', { class: 'git-commits' }, el('h5', {}, t('git.commits')));
  if (!git.commits.length) {
    commits.append(el('p', { class: 'field-hint' }, t('git.commits.empty')));
  } else {
    for (const commit of git.commits.slice(0, 12)) {
      const sha = String(commit.sha || commit.commit || commit.hash || commit.id || '');
      const when = commit.ts || commit.time || commit.timestamp || commit.date;
      commits.append(el('div', { class: 'git-commit-row' },
        el('span', { class: 'gc-sha mono', title: sha }, clip(sha, 10)),
        el('span', { class: 'gc-subject' }, commit.subject || commit.message || commit.title || '—'),
        when ? el('span', { class: 'gc-time mono' },
          typeof when === 'number' ? fmtDateTime(when) : String(when)) : null,
      ));
    }
  }
  wrap.append(commits);

  const diff = el('details', { class: 'git-diff' },
    el('summary', {}, t('git.diff')),
  );
  if (git.diff.trim()) {
    diff.append(el('pre', { class: 'git-diff-pre' }, clip(git.diff, 6000)));
  } else {
    diff.append(el('p', { class: 'field-hint' }, t('git.diff.empty')));
  }
  wrap.append(diff);

  if (git.rollback) {
    if (!canWrite()) {
      wrap.append(appNote(t('git.viewer_hint'), { iconName: 'alert' }));
    } else {
      const refInput = input({
        class: 'input mono git-ref',
        placeholder: t('git.rollback_placeholder'),
        value: defaultRef(git),
        'aria-label': t('git.rollback_ref'),
      });
      const rollback = el('button', { class: 'btn btn-sm btn-danger', type: 'button' },
        icon('refresh', 'ico ico-sm'), t('git.rollback'));
      rollback.addEventListener('click', () => {
        const ref = refInput.value.trim();
        if (!ref) {
          refInput.focus();
          toast(t('git.rollback.need_ref'), 'error');
          return;
        }
        if (!window.confirm(t('git.rollback.confirm', {
          name: projectName || projectId, ref,
        }))) return;
        withBusy(rollback, async () => {
          try {
            await api.rollbackProjectGit(projectId, ref);
            toast(t('git.rollback.done', { ref }), 'success');
            gitCache.delete(projectId);
            const fresh = await api.getProjectGit(projectId);
            gitCache.set(projectId, { at: Date.now(), data: fresh });
            wrap.replaceWith(renderGit(fresh, projectId, projectName));
          } catch (error) {
            toast(t('git.rollback.failed', { message: error?.message || String(error) }), 'error');
          }
        });
      });
      wrap.append(el('div', { class: 'git-rollback' },
        el('label', { class: 'field' },
          el('span', { class: 'git-rollback-label' }, t('git.rollback_ref')),
          refInput,
        ),
        rollback,
      ));
    }
  }
  return wrap;
}

function defaultRef(git) {
  const second = git.commits[1];
  const sha = second && (second.sha || second.hash || second.id);
  return sha ? String(sha) : 'HEAD~1';
}

/** Drop cached git data (e.g. after a rollback elsewhere). */
export function invalidateGit(projectId) {
  gitCache.delete(projectId);
}

