/**
 * api.js — the only place that talks HTTP to the StackFlowOS backend.
 *
 * Every REST response is `{ok:true,data}` or `{ok:false,error:{code,message,detail?}}`.
 * We normalise both into `ApiError` with a human-readable Chinese message so the
 * UI can always show *why* something was refused instead of a generic failure.
 */

export class ApiError extends Error {
  /**
   * @param {string} message
   * @param {{code?:string,status?:number,detail?:unknown}} [meta]
   */
  constructor(message, meta = {}) {
    super(message);
    this.name = 'ApiError';
    this.code = meta.code || 'UNKNOWN';
    this.status = meta.status || 0;
    this.detail = meta.detail ?? null;
  }
}

/** @param {string} path */
async function requestJson(path, options = {}) {
  const { method = 'GET', body } = options;
  /** @type {RequestInit} */
  const init = {
    method,
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path, init);
  } catch (cause) {
    throw new ApiError('无法连接 StackFlowOS 后端，请检查服务是否在运行。', {
      code: 'NETWORK',
      detail: cause instanceof Error ? cause.message : String(cause),
    });
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    /* non-JSON error page */
  }

  if (!payload || payload.ok !== true) {
    const error = (payload && payload.error) || {};
    throw new ApiError(
      error.message || `请求失败（HTTP ${response.status}）`,
      { code: error.code || `HTTP_${response.status}`, status: response.status, detail: error.detail },
    );
  }
  return payload.data;
}

const qs = (params) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === '') continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
};

const enc = (value) => encodeURIComponent(String(value));

export const api = {
  /* ---- system ---- */
  health: () => requestJson('/api/health'),
  getState: () => requestJson('/api/state'),
  getConsole: ({ types = [], projectId = '', limit = 200 } = {}) =>
    requestJson('/api/console' + qs({ types: types.join(','), project_id: projectId, limit })),

  /* ---- chat ---- */
  getChat: (limit = 100) => requestJson('/api/chat' + qs({ limit })),
  sendChat: (text) => requestJson('/api/chat', { method: 'POST', body: { text } }),

  /* ---- projects & tasks ---- */
  getProjects: () => requestJson('/api/projects'),
  createProject: (name, goal) => requestJson('/api/projects', { method: 'POST', body: { name, goal } }),
  getProject: (id) => requestJson(`/api/projects/${enc(id)}`),
  patchProject: (id, patch) => requestJson(`/api/projects/${enc(id)}`, { method: 'PATCH', body: { patch } }),
  archiveProject: (id) => requestJson(`/api/projects/${enc(id)}/archive`, { method: 'POST', body: {} }),
  completeProject: (id) => requestJson(`/api/projects/${enc(id)}/complete`, { method: 'POST', body: {} }),
  getTasks: (projectId) => requestJson(`/api/projects/${enc(projectId)}/tasks`),
  createTask: (projectId, task) => requestJson(`/api/projects/${enc(projectId)}/tasks`, { method: 'POST', body: task }),
  patchTask: (id, patch) => requestJson(`/api/tasks/${enc(id)}`, { method: 'PATCH', body: { patch } }),
  addTaskNote: (id, text) => requestJson(`/api/tasks/${enc(id)}/note`, { method: 'POST', body: { text } }),
  checkTask: (id, payload) => requestJson(`/api/tasks/${enc(id)}/check`, { method: 'POST', body: payload }),

  /* ---- approvals & rules ---- */
  getApprovals: (status = 'pending') => requestJson('/api/approvals' + qs({ status })),
  decideApproval: (id, decision, mode = 'once', note = '') =>
    requestJson(`/api/approvals/${enc(id)}/decide`, { method: 'POST', body: { decision, mode, note } }),
  getRules: () => requestJson('/api/rules'),
  createRule: (rule) => requestJson('/api/rules', { method: 'POST', body: rule }),
  deleteRule: (id) => requestJson(`/api/rules/${enc(id)}`, { method: 'DELETE' }),

  /* ---- units & agents ---- */
  getUnits: () => requestJson('/api/units'),
  killUnit: (workId) => requestJson(`/api/units/${enc(workId)}/kill`, { method: 'POST', body: {} }),
  getAgents: (projectId = '') => requestJson('/api/agents' + qs({ project_id: projectId })),
  spawnAgent: (role, projectId, taskId = null) =>
    requestJson('/api/agents', { method: 'POST', body: { role, project_id: projectId, task_id: taskId } }),
  killAgent: (id) => requestJson(`/api/agents/${enc(id)}`, { method: 'DELETE' }),

  /* ---- config & leases ---- */
  getConfig: () => requestJson('/api/config'),
  patchConfig: (patch) => requestJson('/api/config', { method: 'PATCH', body: { patch } }),
  getLeases: () => requestJson('/api/leases'),
};
