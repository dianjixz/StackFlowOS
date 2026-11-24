/**
 * client.ts — the only place that talks HTTP to the StackFlowOS backend.
 *
 * Every REST response is `{ok:true,data}` or `{ok:false,error:{code,message,detail?}}`.
 * We normalise both into `ApiError` so the UI can always show *why* something was
 * refused instead of a generic failure.
 *
 * The server authenticates via the HttpOnly `sfo_session` cookie set when the
 * operator opens the launch URL (`/?token=...`).  Never store or send a token
 * from JS.
 */

export class ApiError extends Error {
  code: string;
  status: number;
  detail: unknown;

  constructor(message: string, meta: { code?: string; status?: number; detail?: unknown } = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = meta.code || 'UNKNOWN';
    this.status = meta.status || 0;
    this.detail = meta.detail ?? null;
  }
}

export type Json = Record<string, unknown>;

interface RequestOptions {
  method?: string;
  body?: unknown;
}

export async function request<T = unknown>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body } = options;
  const write = method !== 'GET' && method !== 'HEAD';
  const headers: Record<string, string> = { Accept: 'application/json' };
  const init: RequestInit = { method, credentials: 'same-origin', headers, cache: 'no-store' };
  if (write) {
    // Admission requires both: a non-simple content type and this custom
    // header.  A cross-site page cannot send either without a CORS preflight,
    // and the server answers no preflight at all.
    headers['Content-Type'] = 'application/json';
    headers['X-SFO-Request'] = '1';
  }
  if (body !== undefined) init.body = JSON.stringify(body);

  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (cause) {
    throw new ApiError('无法连接 StackFlowOS 后端，请检查服务是否在运行。', {
      code: 'NETWORK',
      detail: cause instanceof Error ? cause.message : String(cause),
    });
  }

  if (response.status === 401) {
    // A single listener (session gate) owns the "please sign in again" screen.
    // Dispatch an event so every caller, including background refreshes, ends
    // up on the same path.
    if (typeof document !== 'undefined' && typeof CustomEvent === 'function') {
      document.dispatchEvent(new CustomEvent('sfo:unauthorized'));
    }
    throw new ApiError(
      '未认证或会话已过期：请用 StackFlowOS 启动时打印的登录 URL（含 ?token=...）重新打开控制台。',
      { code: 'UNAUTHORIZED', status: 401 },
    );
  }

  interface Envelope<U> { ok?: boolean; data?: U; error?: { code?: string; message?: string; detail?: unknown } }
  let payload: Envelope<T> | null = null;
  try {
    payload = (await response.json()) as Envelope<T>;
  } catch {
    /* non-JSON error page */
  }

  if (!payload || payload.ok !== true) {
    const error = (payload && payload.error) || {};
    throw new ApiError(error.message || `请求失败（HTTP ${response.status}）`, {
      code: error.code || `HTTP_${response.status}`,
      status: response.status,
      detail: error.detail,
    });
  }
  return payload.data as T;
}

const qs = (params: Record<string, unknown> = {}): string => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
};

const enc = (value: unknown): string => encodeURIComponent(String(value));

/**
 * True when the backend answered but does not implement this endpoint (or the
 * whole feature is switched off).  Panels use it to show a calm "未启用" note
 * instead of an error toast: an optional extension must not look like a bug.
 */
export function isUnsupported(error: unknown): boolean {
  if (error instanceof ApiError) {
    return error.status === 404 || error.status === 405 || error.status === 501
      || error.code === 'HTTP_404' || error.code === 'HTTP_405' || error.code === 'HTTP_501';
  }
  return false;
}

/** True when the answer was refused for permission reasons (403 / operator). */
export function isForbidden(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 403 || error.code === 'FORBIDDEN');
}

/** True when the endpoint exists but the request conflicts with current state. */
export function isConflict(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 409 || error.code === 'CONFLICT');
}

export const api = {
  /* ---- system ---- */
  health: () => request('/api/health'),
  getState: () => request('/api/state'),
  getConsole: ({ types = [], projectId = '', limit = 200 }: { types?: string[]; projectId?: string; limit?: number } = {}) =>
    request('/api/console' + qs({ types: types.join(','), project_id: projectId, limit })),

  /* ---- chat ---- */
  getChat: (limit = 100) => request('/api/chat' + qs({ limit })),
  sendChat: (text: string) => request('/api/chat', { method: 'POST', body: { text } }),

  /* ---- projects & tasks ---- */
  getProjects: () => request('/api/projects'),
  createProject: (name: string, goal: string) =>
    request('/api/projects', { method: 'POST', body: { name, goal } }),
  attachProject: (payload: Json) => request('/api/projects/attach', { method: 'POST', body: payload }),
  getProject: (id: string) => request(`/api/projects/${enc(id)}`),
  getProjectHealth: (id: string) => request(`/api/projects/${enc(id)}/healthcheck`),
  onboardProject: (id: string) => request(`/api/projects/${enc(id)}/onboard`, { method: 'POST', body: {} }),
  patchProject: (id: string, patch: Json) =>
    request(`/api/projects/${enc(id)}`, { method: 'PATCH', body: { patch } }),
  archiveProject: (id: string) =>
    request(`/api/projects/${enc(id)}/archive`, { method: 'POST', body: {} }),
  setProjectAccess: (id: string, allowWrites: boolean, confirm = false) =>
    request(`/api/projects/${enc(id)}/access`, {
      method: 'POST',
      body: { allow_writes: Boolean(allowWrites), confirm },
    }),
  deleteProject: (id: string, { purgeWorkspace = true, confirm = '' }: { purgeWorkspace?: boolean; confirm?: string } = {}) =>
    request(`/api/projects/${enc(id)}`, {
      method: 'DELETE',
      body: { purge_workspace: purgeWorkspace, confirm },
    }),
  completeProject: (id: string) =>
    request(`/api/projects/${enc(id)}/complete`, { method: 'POST', body: {} }),
  getTasks: (projectId: string) => request(`/api/projects/${enc(projectId)}/tasks`),
  createTask: (projectId: string, task: Json) =>
    request(`/api/projects/${enc(projectId)}/tasks`, { method: 'POST', body: task }),
  patchTask: (id: string, patch: Json) => request(`/api/tasks/${enc(id)}`, { method: 'PATCH', body: { patch } }),
  addTaskNote: (id: string, text: string) =>
    request(`/api/tasks/${enc(id)}/note`, { method: 'POST', body: { text } }),
  checkTask: (id: string, payload: Json) =>
    request(`/api/tasks/${enc(id)}/check`, { method: 'POST', body: payload }),

  /* ---- approvals & rules ---- */
  getApprovals: (status = 'pending') => request('/api/approvals' + qs({ status })),
  getApproval: (id: string) => request(`/api/approvals/${enc(id)}`),
  decideApproval: (id: string, decision: string, mode = 'once', note = '') =>
    request(`/api/approvals/${enc(id)}/decide`, { method: 'POST', body: { decision, mode, note } }),
  getRules: () => request('/api/rules'),
  createRule: (rule: Json) => request('/api/rules', { method: 'POST', body: rule }),
  deleteRule: (id: string) => request(`/api/rules/${enc(id)}`, { method: 'DELETE' }),

  /* ---- autonomy loop ---- */
  getAutonomy: () => request('/api/autonomy/status'),
  runAutonomyPatrol: (dryRun = false) =>
    request('/api/autonomy/patrol', { method: 'POST', body: { dry_run: Boolean(dryRun) } }),
  patchAutonomyPolicy: (patch: Json) =>
    request('/api/autonomy/policy', { method: 'PATCH', body: { ...(patch || {}) } }),

  /* ---- units & agents ---- */
  getUnits: ({ tier = '', kind = '', state = '', projectId = '', status = '', include = '' }: Json = {}) =>
    request('/api/units' + qs({ tier, kind, state, project_id: projectId, status, include })),
  getUnitsSummary: () => request('/api/units/summary'),
  getUnit: (id: string) => request(`/api/units/${enc(id)}`),
  probeUnit: (id: string) => request(`/api/units/${enc(id)}/probe`, { method: 'POST', body: {} }),
  unitAction: (id: string, action: string) =>
    request(`/api/units/${enc(id)}/actions`, { method: 'POST', body: { action } }),
  killUnit: (workId: string) => request(`/api/units/${enc(workId)}/kill`, { method: 'POST', body: {} }),
  getAgents: (projectId = '') => request('/api/agents' + qs({ project_id: projectId })),
  spawnAgent: (role: string, projectId: string, taskId: string | null = null) =>
    request('/api/agents', { method: 'POST', body: { role, project_id: projectId, task_id: taskId } }),
  killAgent: (id: string) => request(`/api/agents/${enc(id)}`, { method: 'DELETE' }),
  getAgentChat: (id: string, limit = 200) => request(`/api/agents/${enc(id)}/chat` + qs({ limit })),
  sendAgentChat: (id: string, text: string) =>
    request(`/api/agents/${enc(id)}/chat`, { method: 'POST', body: { text } }),
  cancelAgentChat: (id: string) =>
    request(`/api/agents/${enc(id)}/chat/cancel`, { method: 'POST', body: {} }),
  clearAgentChat: (id: string) => request(`/api/agents/${enc(id)}/chat`, { method: 'DELETE' }),

  /* ---- capability catalog: skills / plugins / MCP ---- */
  getCapabilities: ({ type = '', projectId = '' }: { type?: string; projectId?: string } = {}) =>
    request('/api/capabilities' + qs({ type, project_id: projectId })),
  getSkills: (projectId = '') => request('/api/skills' + qs({ project_id: projectId })),
  setSkillEnabled: (id: string, enabled: boolean, projectId = '') =>
    request(`/api/skills/${enc(id)}/${enabled ? 'enable' : 'disable'}`, {
      method: 'POST',
      body: { project_id: projectId || null },
    }),
  getPlugins: () => request('/api/plugins'),
  loadPlugin: (id: string) => request(`/api/plugins/${enc(id)}/load`, { method: 'POST', body: {} }),
  unloadPlugin: (id: string) => request(`/api/plugins/${enc(id)}/unload`, { method: 'POST', body: {} }),
  getMcpServers: () => request('/api/mcp/servers'),
  connectMcp: (name: string) => request(`/api/mcp/servers/${enc(name)}/connect`, { method: 'POST', body: {} }),
  disconnectMcp: (name: string) =>
    request(`/api/mcp/servers/${enc(name)}/disconnect`, { method: 'POST', body: {} }),
  getMcpTools: (server = '') => request('/api/mcp/tools' + qs({ server })),
  callMcp: ({ server, tool, args }: { server: string; tool: string; args?: unknown }) =>
    request('/api/mcp/call', { method: 'POST', body: { server, tool, args } }),

  /* ---- llm usage / models ---- */
  getLlmUsage: (projectId = '') => request('/api/llm/usage' + qs({ project_id: projectId })),
  getLlmModels: () => request('/api/llm/models'),

  /* ---- sessions & auth ---- */
  getSessions: () => request('/api/sessions'),
  rotateSessions: () => request('/api/sessions/rotate', { method: 'POST', body: {} }),
  revokeSession: (id: string) => request(`/api/sessions/${enc(id)}`, { method: 'DELETE' }),
  logout: () => request('/api/logout', { method: 'POST', body: {} }),

  /* ---- project workspace git ---- */
  getProjectGit: (id: string) => request(`/api/projects/${enc(id)}/git`),
  rollbackProjectGit: (id: string, ref: string) =>
    request(`/api/projects/${enc(id)}/git/rollback`, { method: 'POST', body: { ref } }),

  /* ---- config & leases ---- */
  getConfig: () => request('/api/config'),
  patchConfig: (patch: Json) => request('/api/config', { method: 'PATCH', body: { patch } }),
  testLlm: () => request('/api/llm/test', { method: 'POST', body: {} }),
  getLeases: () => request('/api/leases'),
};

export type Api = typeof api;
