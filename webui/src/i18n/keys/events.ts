/**
 * events.ts — strings for the Events screen (`#/events`).
 *
 * zh is the source of truth; keep `en` in step. `i18n/index.ts` merges this
 * dictionary automatically.
 */

export const zh = {
  /* live header / actions */
  'events.live': '实时跟随',
  'events.paused': '已暂停',
  'events.pause': '暂停跟随',
  'events.resume': '继续跟随',
  'events.clear': '清屏',
  'events.cleared': '已清空本地视图，服务端事件不受影响。',
  'events.connection.live': '实时',
  'events.connection.connecting': '连接中',
  'events.connection.reconnecting': '重连中',
  'events.connection.offline': '离线',
  'events.count': '显示 {shown} / {total} 条',
  'events.last_seq': '最新 seq #{seq}',
  'events.new_since_pause': '暂停期间新增 {n} 条',
  'events.paused_hint': '暂停时列表保持不动；继续后跳到最新事件。',

  /* filters */
  'events.filter.type': '事件类型',
  'events.filter.project': '项目',
  'events.filter.project.all': '全部项目',
  'events.filter.search': '搜索事件',
  'events.search.placeholder': '搜索类型 / 来源 / 摘要 / JSON…',
  'events.filter.limit': '条数',
  'events.limit.option': '{n} 条',
  'events.filter.clear': '清空过滤',

  /* event types */
  'events.type.all': '全部',
  'events.type.chat': '对话',
  'events.type.project': '项目',
  'events.type.task': '任务',
  'events.type.agent': 'Agent',
  'events.type.unit': '单元',
  'events.type.approval': '审批',
  'events.type.rule': '规则',
  'events.type.tool': '工具',
  'events.type.failure': '故障',
  'events.type.sys': '系统',

  /* table */
  'events.table.seq': 'Seq',
  'events.table.type': '类型',
  'events.table.source': '来源',
  'events.table.project': '项目',
  'events.table.time': '时间',
  'events.table.summary': '摘要',
  'events.source.unknown': 'sys',

  /* empty states */
  'events.empty.none.title': '还没有事件',
  'events.empty.none.text': 'hub 产生的事件会实时出现在这里。',
  'events.empty.filtered.title': '没有匹配的事件',
  'events.empty.filtered.text': '换一个事件类型、项目或搜索条件。',
  'events.empty.cleared.title': '本地视图已清空',
  'events.empty.cleared.text': '新事件会继续出现在这里；服务端保存的历史记录不受影响。',

  /* expanded row */
  'events.detail.data': '事件数据',
  'events.detail.id': '事件 ID',
  'events.detail.seq': '序号',
  'events.detail.source': '来源',
  'events.detail.project': '项目',
  'events.detail.ts': '时间',
};

export const en = {
  /* live header / actions */
  'events.live': 'Following live',
  'events.paused': 'Paused',
  'events.pause': 'Pause',
  'events.resume': 'Resume',
  'events.clear': 'Clear view',
  'events.cleared': 'Local view cleared; server events are untouched.',
  'events.connection.live': 'Live',
  'events.connection.connecting': 'Connecting',
  'events.connection.reconnecting': 'Reconnecting',
  'events.connection.offline': 'Offline',
  'events.count': 'Showing {shown} / {total}',
  'events.last_seq': 'Latest seq #{seq}',
  'events.new_since_pause': '{n} new while paused',
  'events.paused_hint': 'While paused the list stays put; resuming jumps to the newest events.',

  /* filters */
  'events.filter.type': 'Event type',
  'events.filter.project': 'Project',
  'events.filter.project.all': 'All projects',
  'events.filter.search': 'Search events',
  'events.search.placeholder': 'Search type / source / summary / JSON…',
  'events.filter.limit': 'Limit',
  'events.limit.option': '{n} rows',
  'events.filter.clear': 'Clear filters',

  /* event types */
  'events.type.all': 'All',
  'events.type.chat': 'Chat',
  'events.type.project': 'Project',
  'events.type.task': 'Task',
  'events.type.agent': 'Agent',
  'events.type.unit': 'Unit',
  'events.type.approval': 'Approval',
  'events.type.rule': 'Rule',
  'events.type.tool': 'Tool',
  'events.type.failure': 'Failure',
  'events.type.sys': 'System',

  /* table */
  'events.table.seq': 'Seq',
  'events.table.type': 'Type',
  'events.table.source': 'Source',
  'events.table.project': 'Project',
  'events.table.time': 'Time',
  'events.table.summary': 'Summary',
  'events.source.unknown': 'sys',

  /* empty states */
  'events.empty.none.title': 'No events yet',
  'events.empty.none.text': 'Events emitted by the hub show up here in real time.',
  'events.empty.filtered.title': 'No matching events',
  'events.empty.filtered.text': 'Try another event type, project or search term.',
  'events.empty.cleared.title': 'Local view cleared',
  'events.empty.cleared.text': 'New events keep arriving here; the history stored on the server is untouched.',

  /* expanded row */
  'events.detail.data': 'Event data',
  'events.detail.id': 'Event ID',
  'events.detail.seq': 'Seq',
  'events.detail.source': 'Source',
  'events.detail.project': 'Project',
  'events.detail.ts': 'Time',
};
