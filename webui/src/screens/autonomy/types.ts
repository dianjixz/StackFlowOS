/**
 * types.ts — the autonomy status payload.
 *
 * Mirrors `AutonomyService.status()` (`GET /api/autonomy/status`) and
 * `AutonomyCfg` for the policy object.  The records are kept loose on purpose:
 * the backend's patrol report and the fixture describe the same concepts with
 * slightly different field names, so the screen normalises both shapes in
 * `common.tsx` instead of trusting one spelling.
 */

export type Finding = {
  check?: string;
  kind?: string;
  severity: string;
  title: string;
  detail?: string;
  fix?: string;
  fingerprint?: string;
  project_id?: string | null;
  task_id?: string | null;
  agent_id?: string | null;
  unit_id?: string | null;
  dispatchable?: boolean;
  dispatch?: { kind?: string; role?: string; title?: string } | null;
  detected_at?: number;
  evidence?: Record<string, unknown>;
  suggested?: string;
  [key: string]: unknown;
};

export type DispatchRecord = {
  action?: string;
  kind?: string;
  status?: string;
  ok?: boolean;
  fingerprint?: string;
  finding_id?: string;
  project_id?: string | null;
  task_id?: string | null;
  agent_id?: string | null;
  role?: string;
  reason?: string;
  ts?: number;
  [key: string]: unknown;
};

export type GuardianRecord = {
  decision?: string;
  request_id?: string;
  approval_id?: string;
  capability?: string;
  principal?: string;
  risk?: string;
  project_id?: string | null;
  reason?: string;
  ts?: number;
  [key: string]: unknown;
};

export type PatrolReport = {
  seq?: number;
  id?: string;
  trigger?: string;
  dry_run?: boolean;
  started_at?: number;
  finished_at?: number;
  duration_ms?: number;
  findings?: Finding[];
  summary?: { critical?: number; warn?: number; info?: number; total?: number; findings?: number; dispatched?: number; approved?: number };
  headline?: string;
  dispatched?: DispatchRecord[];
  approvals?: GuardianRecord[];
  actions?: DispatchRecord[];
  error?: string | null;
  [key: string]: unknown;
};

export type AutonomyPolicy = {
  enabled?: boolean;
  interval_s?: number;
  dispatch_mode?: string;
  approve_mode?: string;
  approve_risk_max?: string;
  approve_capabilities?: string[];
  approve_projects?: string[];
  approve_roles?: string[];
  approve_max_per_hour?: number;
  dispatch_max_agents?: number;
  dispatch_max_tasks_per_hour?: number;
  dispatch_max_spawns_per_hour?: number;
  approve_shell?: boolean;
  approve_shell_patterns?: string[];
  approve_domains?: string[];
  [key: string]: unknown;
};

export type Guardrail = {
  spawns_last_hour?: number;
  tasks_last_hour?: number;
  approvals_last_hour?: number;
  limits?: {
    max_spawns_per_hour?: number;
    max_tasks_per_hour?: number;
    max_approvals_per_hour?: number;
    max_agents?: number;
  };
  [key: string]: unknown;
};

export type AutonomyStatus = {
  enabled: boolean;
  running: boolean;
  last_run?: number;
  next_run_in_s?: number | null;
  interval_s?: number;
  policy: AutonomyPolicy;
  counts?: Record<string, unknown>;
  findings?: Finding[];
  report?: PatrolReport | null;
  history?: PatrolReport[];
  dispatches?: DispatchRecord[];
  approvals?: GuardianRecord[];
  guardrail?: Guardrail;
  [key: string]: unknown;
};

/** The subset of `AutonomyCfg` this screen edits (partial PATCH only). */
export type PolicyDraft = {
  enabled: boolean;
  interval_s: number;
  dispatch_mode: string;
  approve_mode: string;
  approve_risk_max: string;
  approve_capabilities: string[];
  approve_shell: boolean;
  approve_shell_patterns: string[];
  approve_domains: string[];
  dispatch_max_agents: number;
  dispatch_max_tasks_per_hour: number;
  dispatch_max_spawns_per_hour: number;
  approve_max_per_hour: number;
};

export const DISPATCH_MODES = ['off', 'tasks', 'agents'] as const;
export const APPROVE_MODES = ['off', 'observe', 'enforce'] as const;
export const RISK_LEVELS = ['low', 'medium', 'high'] as const;
export const CAPABILITIES = [
  'fs.read', 'fs.list', 'fs.write', 'shell.exec', 'net.fetch',
  'llm.chat', 'task.manage', 'agent.spawn', 'data.read', 'data.write',
] as const;
