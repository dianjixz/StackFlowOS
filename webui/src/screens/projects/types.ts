/**
 * types.ts — the wire shapes the Projects screen reads.
 *
 * The shared store already types the project card; this module adds the
 * fields only the detail screen needs (external workspace mode, milestones,
 * project-scoped leases) plus the task / git / health payloads that are
 * fetched on demand.
 */

import type { Agent, Lease, Project, ProjectCounts } from '../../state/store';

export interface ProjectCard extends Project {
  external?: boolean;
  workspace_mode?: string;
  milestones?: number | unknown[];
  counts?: ProjectCounts;
  agents?: Agent[];
  leases?: Lease[];
  pending_approvals?: number;
}

export interface TaskCheck {
  id: string;
  text?: string;
  done?: boolean;
  note?: string;
}

export interface TaskNote {
  ts?: number;
  author?: string;
  text?: string;
}

export interface TaskArtifact {
  type?: string;
  path?: string;
  title?: string;
  ts?: number;
}

export interface Task {
  id: string;
  project_id?: string;
  title?: string;
  description?: string;
  status: string;
  priority?: number;
  role?: string | null;
  assignee?: string | null;
  created_by?: string | null;
  deps?: string[] | string;
  depends_on?: string[] | string;
  blocked_by?: string[] | string;
  checks?: TaskCheck[];
  notes?: TaskNote[];
  artifacts?: TaskArtifact[];
  created_at?: number;
  updated_at?: number;
  started_at?: number;
  finished_at?: number;
  error?: string | null;
}

export interface GitCommit {
  commit?: string;
  sha?: string;
  hash?: string;
  id?: string;
  subject?: string;
  message?: string;
  title?: string;
  ts?: number | string;
  time?: number | string;
  date?: string;
}

export interface GitChange {
  status?: string;
  path?: string;
  line?: string;
  additions?: number;
  deletions?: number;
}

export interface GitState {
  enabled: boolean;
  repo: boolean;
  reason: string;
  branch: string;
  head: string;
  dirty: boolean;
  commits: GitCommit[];
  changes: GitChange[];
  stat: string;
  /** `diff` plus the changed-file lines, already joined for display. */
  diff: string;
  workspace: string;
  rollback: boolean;
}

export interface HealthCheck {
  id?: string;
  label?: string;
  status?: string;
  message?: string;
  fix?: string;
  checked?: boolean;
}

export interface HealthSummary {
  ok?: number;
  warn?: number;
  fail?: number;
  unchecked?: number;
}

export interface HealthReport {
  ready?: boolean;
  status?: string;
  summary?: HealthSummary;
  checks?: HealthCheck[];
  not_ready_reasons?: string[];
  next_actions?: string[];
}
