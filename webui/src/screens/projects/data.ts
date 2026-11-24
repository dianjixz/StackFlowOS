/**
 * data.ts — pure helpers for the Projects screen: normalising the payloads the
 * REST endpoints send, and the small derivations the list, board, graph and
 * git panel need.
 *
 * Nothing here renders; keeping it pure makes the quirks of the backend
 * contract (spellings, optional fields) testable in one place.
 */

import { t } from '../../i18n';
import type {
  GitChange,
  GitCommit,
  GitState,
  HealthReport,
  ProjectCard,
  Task,
} from './types';

export const TASK_STATUSES = ['todo', 'doing', 'review', 'done', 'blocked'] as const;
export type TaskStatus = (typeof TASK_STATUSES)[number];

/** Order the list groups projects in: work first, archive last. */
export const GROUP_ORDER = ['active', 'degraded', 'review', 'planning', 'done', 'archived'] as const;

const GROUP_RANK: Record<string, number> = {
  active: 0,
  degraded: 1,
  review: 2,
  planning: 3,
  done: 4,
  archived: 5,
};

/** Effective status: a degraded project is flagged even when its status is `active`. */
export function projectStatus(project: ProjectCard | null | undefined): string {
  if (!project) return 'planning';
  return project.degraded ? 'degraded' : String(project.status || 'planning');
}

export function milestoneCount(project: ProjectCard): number {
  if (Array.isArray(project.milestones)) return project.milestones.length;
  const value = Number(project.milestones);
  return Number.isFinite(value) ? value : 0;
}

export interface Counts {
  todo: number;
  doing: number;
  review: number;
  done: number;
  blocked: number;
  total: number;
}

export function projectCounts(project: ProjectCard): Counts {
  const raw = project.counts || {};
  const num = (value: unknown): number => {
    const n = Number(value);
    return Number.isFinite(n) && n > 0 ? n : 0;
  };
  const todo = num(raw.todo);
  const doing = num(raw.doing);
  const review = num(raw.review);
  const done = num(raw.done);
  const blocked = num(raw.blocked);
  const total = Number(project.total_tasks);
  return {
    todo,
    doing,
    review,
    done,
    blocked,
    total: Number.isFinite(total) && total > 0 ? total : todo + doing + review + done + blocked,
  };
}

export function sortProjects(projects: ProjectCard[]): ProjectCard[] {
  return [...projects].sort((a, b) => {
    const ra = GROUP_RANK[projectStatus(a)] ?? 9;
    const rb = GROUP_RANK[projectStatus(b)] ?? 9;
    if (ra !== rb) return ra - rb;
    return (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0);
  });
}

export interface ProjectGroup {
  status: string;
  projects: ProjectCard[];
}

/** Bucket projects by effective status, in display order, skipping empty groups. */
export function groupProjects(projects: ProjectCard[]): ProjectGroup[] {
  const buckets = new Map<string, ProjectCard[]>();
  for (const project of sortProjects(projects)) {
    const status = projectStatus(project);
    const list = buckets.get(status);
    if (list) list.push(project);
    else buckets.set(status, [project]);
  }
  const ordered: string[] = GROUP_ORDER.filter((status) => buckets.has(status));
  // Unknown statuses keep their data rather than disappearing.
  for (const status of buckets.keys()) {
    if (!ordered.includes(status)) ordered.push(status);
  }
  return ordered.map((status) => ({ status, projects: buckets.get(status) || [] }));
}

/** Statuses offered in the filter strip (only those that exist, plus All). */
export function filterOptions(projects: ProjectCard[]): string[] {
  const present = new Set(projects.map(projectStatus));
  return GROUP_ORDER.filter((status) => present.has(status));
}

/* ------------------------------------------------------------------ */
/* tasks                                                               */
/* ------------------------------------------------------------------ */

export function taskKey(task: Task | null | undefined): string {
  return String(task?.id || '');
}

export function taskTitle(task: Task | null | undefined): string {
  return String(task?.title || task?.id || '');
}

/** The several spellings the task model has used for prerequisites. */
export function taskDeps(task: Task | null | undefined): string[] {
  if (!task || typeof task !== 'object') return [];
  const raw = task.deps ?? task.depends_on ?? task.blocked_by ?? [];
  if (Array.isArray(raw)) return raw.map(String).filter(Boolean);
  if (typeof raw === 'string') {
    return raw.split(',').map((value) => value.trim()).filter(Boolean);
  }
  return [];
}

export function openDepCount(task: Task, tasks: Task[]): number {
  const byId = new Map(tasks.map((item) => [taskKey(item), item]));
  return taskDeps(task).filter((id) => {
    const dep = byId.get(id);
    return !dep || dep.status !== 'done';
  }).length;
}

export function isDependencyBlocked(task: Task, tasks: Task[]): boolean {
  return openDepCount(task, tasks) > 0;
}

export function checkProgress(task: Task): { done: number; total: number } {
  const checks = Array.isArray(task.checks) ? task.checks : [];
  const total = checks.length;
  const done = checks.filter((check) => check && check.done).length;
  return { done, total };
}

export function normalizeTaskList(payload: unknown): Task[] {
  if (Array.isArray(payload)) return payload as Task[];
  if (payload && typeof payload === 'object') {
    const body = payload as { tasks?: unknown; items?: unknown };
    if (Array.isArray(body.tasks)) return body.tasks as Task[];
    if (Array.isArray(body.items)) return body.items as Task[];
  }
  return [];
}

export function priorityLabel(priority: number | undefined | null): string {
  const value = Math.max(0, Math.min(3, Math.round(Number(priority) || 0)));
  return t(`prj.priority.${value}`, {}, String(value));
}

/** Tasks with at least one edge in either direction, i.e. what the graph draws. */
export function tasksWithDeps(tasks: Task[]): Task[] {
  const referenced = new Set<string>();
  for (const task of tasks) {
    for (const dep of taskDeps(task)) referenced.add(dep);
  }
  return tasks.filter((task) => taskDeps(task).length > 0 || referenced.has(taskKey(task)));
}

export interface DependencyLayout {
  /** tasks that have (or are part of) a dependency edge */
  nodes: Task[];
  /** column index → tasks in that column, top to bottom */
  columns: Map<number, Task[]>;
  /** dependency depth per task id */
  depth: Map<string, number>;
  byId: Map<string, Task>;
}

/**
 * Layer the dependency graph: a task sits one column right of its deepest
 * prerequisite.  Cycles (the store rejects them) are broken by returning 0.
 */
export function layoutDependencies(tasks: Task[]): DependencyLayout {
  const nodes = tasksWithDeps(tasks);
  const byId = new Map(nodes.map((task) => [taskKey(task), task]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();

  const compute = (task: Task): number => {
    const key = taskKey(task);
    if (depth.has(key)) return depth.get(key) ?? 0;
    if (visiting.has(key)) return 0;
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
  for (const task of nodes) compute(task);

  const columns = new Map<number, Task[]>();
  for (const task of nodes) {
    const column = depth.get(taskKey(task)) ?? 0;
    const list = columns.get(column);
    if (list) list.push(task);
    else columns.set(column, [task]);
  }
  return { nodes, columns, depth, byId };
}

/* ------------------------------------------------------------------ */
/* workspace git                                                       */
/* ------------------------------------------------------------------ */

function text(value: unknown): string {
  return value === undefined || value === null ? '' : String(value);
}

export function normalizeGit(payload: unknown): GitState {
  const data = (payload && typeof payload === 'object' ? payload : {}) as Record<string, any>;
  const commits: GitCommit[] = Array.isArray(data.commits) ? data.commits
    : Array.isArray(data.recent_commits) ? data.recent_commits
      : Array.isArray(data.log) ? data.log : [];
  const rawChanges: unknown[] = Array.isArray(data.changes) ? data.changes : [];
  const changes: GitChange[] = rawChanges
    .map((change) => (typeof change === 'string' ? { line: change } : (change as GitChange)))
    .filter((change) => Boolean(change && (change.line || change.path)));
  const stat = text(data.stat || data.diff_stat);
  const changesText = changes
    .map((change) => {
      const counts = [
        Number(change.additions) ? `+${change.additions}` : '',
        Number(change.deletions) ? `-${change.deletions}` : '',
      ].filter(Boolean).join(' ');
      const label = [change.status, change.path || change.line].filter(Boolean).join(' ');
      return [label, counts].filter(Boolean).join('  ');
    })
    .filter(Boolean)
    .join('\n');
  // The backend reports a diff *summary* (`stat`) plus per-file changes; a
  // full patch is optional.  Fall back to the change list only when neither
  // exists, so the panel never repeats itself.
  const diff = text(data.diff || data.uncommitted_diff) || stat || changesText;
  return {
    enabled: data.enabled !== false,
    repo: data.repo !== false,
    reason: text(data.reason || data.error),
    branch: text(data.branch || data.current_branch || data.head) || '—',
    head: text(data.head || data.commit),
    dirty: Boolean(data.dirty ?? data.is_dirty ?? data.uncommitted ?? changes.length),
    commits,
    changes,
    stat,
    workspace: text(data.workspace || data.path),
    rollback: data.rollback_available !== false && data.rollback !== false,
    diff,
  };
}

export function gitCommitSha(commit: GitCommit): string {
  return text(commit.sha || commit.commit || commit.hash || commit.id);
}

export function gitCommitSubject(commit: GitCommit): string {
  return text(commit.subject || commit.message || commit.title) || '—';
}

/* ------------------------------------------------------------------ */
/* health report                                                       */
/* ------------------------------------------------------------------ */

export function normalizeHealth(payload: unknown): HealthReport {
  const data = (payload && typeof payload === 'object' ? payload : {}) as Record<string, any>;
  return {
    ready: Boolean(data.ready),
    status: text(data.status),
    summary: (data.summary && typeof data.summary === 'object' ? data.summary : {}),
    checks: Array.isArray(data.checks) ? data.checks : [],
    not_ready_reasons: Array.isArray(data.not_ready_reasons) ? data.not_ready_reasons.map(text) : [],
    next_actions: Array.isArray(data.next_actions) ? data.next_actions.map(text) : [],
  };
}
