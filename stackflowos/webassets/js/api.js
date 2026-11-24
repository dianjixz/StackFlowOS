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
  const write = method !== 'GET' && method !== 'HEAD';
  /** @type {RequestInit} */
  const init = {
    method,
    // The server authenticates via the HttpOnly `sfo_session` cookie set when
    // the operator opens the launch URL (`/?token=...`).  Never store or send
    // a token from JS.
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
    cache: 'no-store',
  };
  if (write) {
    // Admission requires both: a non-simple content type and this custom
    // header.  A cross-site page cannot send either without a CORS preflight,
    // and the server answers no preflight at all.
    init.headers['Content-Type'] = 'application/json';
    init.headers['X-SFO-Request'] = '1';
  }
  if (body !== undefined) {
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

  if (response.status === 401) {
    throw new ApiError(
      '未认证或会话已过期：请用 StackFlowOS 启动时打印的登录 URL（含 ?token=...）重新打开控制台。',
      { code: 'UNAUTHORIZED', status: 401 },
    );
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
  attachProject: (payload) => requestJson('/api/projects/attach', { method: 'POST', body: payload }),
  getProject: (id) => requestJson(`/api/projects/${enc(id)}`),
  patchProject: (id, patch) => requestJson(`/api/projects/${enc(id)}`, { method: 'PATCH', body: { patch } }),
  archiveProject: (id) => requestJson(`/api/projects/${enc(id)}/archive`, { method: 'POST', body: {} }),
  setProjectAccess: (id, allowWrites, confirm = false) =>
    requestJson(`/api/projects/${enc(id)}/access`, {
      method: 'POST', body: { allow_writes: Boolean(allowWrites), confirm },
    }),
  deleteProject: (id, { purgeWorkspace = true, confirm = '' } = {}) =>
    requestJson(`/api/projects/${enc(id)}`, {
      method: 'DELETE',
      body: { purge_workspace: purgeWorkspace, confirm },
    }),
  completeProject: (id) => requestJson(`/api/projects/${enc(id)}/complete`, { method: 'POST', body: {} }),
  getTasks: (projectId) => requestJson(`/api/projects/${enc(projectId)}/tasks`),
  createTask: (projectId, task) => requestJson(`/api/projects/${enc(projectId)}/tasks`, { method: 'POST', body: task }),
  patchTask: (id, patch) => requestJson(`/api/tasks/${enc(id)}`, { method: 'PATCH', body: { patch } }),
  addTaskNote: (id, text) => requestJson(`/api/tasks/${enc(id)}/note`, { method: 'POST', body: { text } }),
  checkTask: (id, payload) => requestJson(`/api/tasks/${enc(id)}/check`, { method: 'POST', body: payload }),

  /* ---- approvals & rules ---- */
  getApprovals: (status = 'pending') => requestJson('/api/approvals' + qs({ status })),
  getApproval: (id) => requestJson(`/api/approvals/${enc(id)}`),
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
  getAgentChat: (id, limit = 200) =>
    requestJson(`/api/agents/${enc(id)}/chat` + qs({ limit })),
  sendAgentChat: (id, text) =>
    requestJson(`/api/agents/${enc(id)}/chat`, { method: 'POST', body: { text } }),
  cancelAgentChat: (id) =>
    requestJson(`/api/agents/${enc(id)}/chat/cancel`, { method: 'POST', body: {} }),
  clearAgentChat: (id) =>
    requestJson(`/api/agents/${enc(id)}/chat`, { method: 'DELETE' }),

  /* ---- config & leases ---- */
  getConfig: () => requestJson('/api/config'),
  patchConfig: (patch) => requestJson('/api/config', { method: 'PATCH', body: { patch } }),
  testLlm: () => requestJson('/api/llm/test', { method: 'POST', body: {} }),
  getLeases: () => requestJson('/api/leases'),
};
