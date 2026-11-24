# UNITS 工作流交接（units）

> 任务板：`task-4`（UNITS：Capability Catalog、SkillRegistry、PluginLoader、MCP 宿主、单元动作/探针/UnitClient）
> 所有权：新增 `stackflowos/capabilities.py`、`stackflowos/skills/**`、`stackflowos/plugins/**`、
> `stackflowos/mcp/**`、`stackflowos/units/control.py`；`tests/test_units_*.py`、`examples/plugins/**`、本文档。
> **未修改** `app.py`、`config.py`、`web/api.py`、`web/server.py`、`extensions.py`、`agents/**`；未运行任何 git 写操作。

## 1. 改动文件

新增：

| 文件 | 作用 |
|---|---|
| `stackflowos/capabilities.py` | 统一能力目录扩展（`/api/capabilities`）、provider/available 推导、`call_capability` 执行入口 |
| `stackflowos/skills/manifest.py` | skill 目录解析/校验（`SKILL.md` + 可选 front-matter，或 `skill.json`） |
| `stackflowos/skills/__init__.py` | SkillRegistry：扫描、状态文件、项目级启用、注入渲染、`skill.0` 单元、`/api/skills` |
| `stackflowos/plugins/__init__.py` | PluginLoader：manifest 校验、Cordis `effect` 加载/卸载、`plugin.0`/`plugin.<id>` 单元、`/api/plugins` |
| `stackflowos/mcp/client.py` | stdio JSON-RPC 客户端（newline + `Content-Length` 双 framing、超时、stderr、进程退出） |
| `stackflowos/mcp/__init__.py` | MCPHost：配置、`mcp.0`/`mcp.<name>` 单元、工具目录、`/api/mcp/*`、审批门、重连 |
| `stackflowos/units/control.py` | `UnitStateStore`、`HealthTicker`、`/api/units/{id}`+summary+probe/actions、`UnitClient`/`unit://` |
| `examples/plugins/hello/**` | 受信 in-process 示例插件（贡献 `plugin.echo` 工具 + `hello-skill`） |
| `tests/test_units_capabilities.py` | 目录条目、provider 状态、项目隔离、`call_capability` |
| `tests/test_units_skills.py` | 扫描/校验、启用、注入、事件、`skill.0` 状态、restart/disable 动作 |
| `tests/test_units_plugins.py` | 示例插件加载/卸载、工具/skill 目录、审批执行、失败隔离 |
| `tests/test_units_mcp.py` | 两种 framing、initialize/tools、stderr 脱敏、超时、崩溃、审批、重连 |
| `tests/test_units_control.py` | 迟滞、ticker、summary/detail/actions、UnitClient stub 替换 |

修改：

| 文件 | 改动 |
|---|---|
| `tests/test_units_visibility.py` | `skill.0` 的断言从硬编码 `disabled` 改为 Phase 2 的 `ready|degraded`（真实扫描结果） |

`l2/registry.py` 未改（worker 注册/心跳语义不需要扩展）。

## 2. 单元 id 与状态

| 单元 | kind/tier | 状态来源 | 说明 |
|---|---|---|---|
| `skill.0` | skill/service | 真实扫描：0 个 skill 也 `ready`；任一条 invalid → `degraded`（reason 带 id）；扫描异常 → `failed`；operator disable → `disabled` | 单个 skill 不是单元，是目录条目 |
| `plugin.0` | plugin/service | 宿主：任一 invalid/failed → `degraded`；0 个也 `ready`；disable 宿主 → `disabled` | 聚合计数 |
| `plugin.<id>` | plugin/service | `ready`（已 apply）/`degraded`（声明了 contributes 但未注册对应条目）/`failed`（import/apply 失败）/`disabled`（未加载或 operator 禁用） | 每个受信插件一个单元 |
| `mcp.0` | mcp/service | 任一已启用 server `failed` → `degraded`；0 个也 `ready`；disable 宿主 → `disabled` | 聚合计数 |
| `mcp.<name>` | mcp/service | `ready`（initialize+tools/list 成功）/`starting`/`failed`（进程退出/连接失败）/`disabled`/`stopped` | 真实子进程 pid；断线后重连 |

服务单元（含以上全部）不伪造 `pid/rpc_address`：`runtime.mode` 为 `in_process`（MCP server 为 `subprocess` 且有真实 pid），
旧平铺字段为 `null`，`ref=unit://<id>`。扩展单元描述符由各扩展 `units()` 返回，并在返回前应用
`UnitController.overlay()`，所以状态变化会实时反映到 `/api/units`。

`/api/units` 核心路由仍由 `app.logical_units()` 组合；`app.py` 中核内服务（`store.*`、`approval.0`…）的原始
state 由 app 现有探针计算，控制模块的迟滞结果会覆盖在扩展单元描述符、`/api/units/{id}`、`/api/units/summary`
和事件中（见 §9 残余风险 1）。

## 3. 公开 API（给 Lead/agent/前端接线）

### 3.1 Capability Catalog — `app.capability_catalog`（别名 `app.capabilities`）

```python
class CapabilityCatalog:
    def register(self, entry: dict, *, provider: str | None = None) -> Callable[[], None]
    def register_tool_executor(self, provider: str, name: str, executor) -> Callable[[], None]
    def entries(self, kind: str | None = None, project_id: str | None = None) -> list[dict]
    def capabilities(self, kind: str | None = None) -> list[dict]      # 扩展 hook
    def find_tool(self, name: str, project_id: str | None = None, *, provider=None) -> list[dict]
    async def call_capability(self, name: str, args: dict, project_id: str | None,
                              principal, approval_id: str | None = None) -> dict
```

条目字段：`type(tool|skill|plugin|mcp_tool)`、`name`、`description`、`capability`、`risk`、`schema`、
`args`（内置工具）、`provider`（**单元 id**，如 `tool.0`/`plugin.hello`/`mcp.echo`）、`enabled_in`
（`["*"]` 或项目 id 列表）、`available`、`provider_state`、`metadata`。
`available` **严格**由 provider 单元 state 推导：只有 `state == "ready"` 才是 true（`degraded` 也算不可用，
符合“provider 非 ready 时 false”）。`project_id` 不存在 → 404；无 `project_id` 只返回全局条目，项目专属条目
不会跨项目泄漏。

`call_capability` 的审批/执行语义：

1. 只接受目录中已注册且 `available` 的 `tool`/`mcp_tool`；`provider=tool.0` 的内置工具直接 `BadRequest`
   （让 hub 走 worker executor 原路径）。
2. `principal` 可为 `security.Principal` 或字符串。unit principal 的 `project_id` 强制为自己的项目，跨项目
   抛 `Denied`；operator 可显式传 `project_id`。
3. 必走 `app.approvals`（`ensure_approved`）：未批准时**不执行**，返回
   `{"status": "pending"|"denied"|"claimed", "executed": false, "approval_id": ...}`；
   一次批准只能执行一次，重复用同一个 `approval_id` 重试得到 `status="claimed"`。
4. 执行成功返回 `{"status":"ok","executed":true,"result":...}`；审计事件
   `capability.pending` / `capability.called` / `capability.failed`（MCP 另发 `mcp.tool.called`）。
5. 审批 capability 使用条目声明的 `capability`（不在闭集则回退 `shell.exec`），risk 取条目 risk 与
   `security.capabilities` 基础风险的较大值；默认 fail-closed。

**Lead 接线（app.py 的 `capability.call`）**：

```python
async def _capability_call(self, data, src, principal=None):
    catalog = getattr(self, "capability_catalog", None)
    if catalog is None:
        raise BadRequest("capability catalogue is unavailable")
    return await catalog.call_capability(
        str(data.get("name") or ""), data.get("args") or {},
        data.get("project_id"), principal,
        approval_id=data.get("approval_id"))
```

`capability.call` 已在 `PRINCIPAL_ACTIONS`；把 handler 注册到 `self._actions` 即可。建议 RPC 协议把可选
`approval_id` 透传，否则 worker 在 `pending` 后只能等策略规则自动放行。

### 3.2 SkillRegistry — `app.skills`（别名 `app.skill_registry`）

```python
class SkillRegistry:
    def scan(self) -> list[SkillEntry]
    def all_entries(self) -> list[SkillEntry]
    def get(self, skill_id) -> SkillEntry
    def is_enabled(self, skill_id, project_id=None) -> bool
    def set_enabled(self, skill_id, enabled: bool, project_id=None) -> SkillEntry
    def add_external_skill(self, data: dict, *, provider: str) -> tuple[SkillEntry, Callable[[], None]]
    def render_injection(self, project_id=None, role=None, prompt=None, *,
                         triggers=None, max_chars=None) -> str
    def injection_text(self, ...) -> str   # 兼容别名
    async def restart(self) -> dict        # 重新扫描
    async def disable_unit(self) / enable_unit(self)
```

注入渲染规则（供 agent 提示词接线）：

* `project_id`：全局启用 ∪ 项目启用，项目级 `disabled` 优先；
* `role`：`allowed_roles` 为空或包含 role；无 role 时角色限定 skill 不注入；
* `prompt`/`triggers`：有 trigger 的 skill 只有命中 prompt 子串或显式 trigger 才注入；
* `requires_tools`：role 已知时，role 的工具集必须包含全部依赖；
* 总字符数受 `skills.inject_max_chars`（或 `max_chars`）限制，截断带 `...[skill truncated]...`。

Manifest 支持：
- `skill.json`：`id/name/version/description/instructions/triggers/allowed_roles/requires_tools`；
  `SKILL.md` 可提供正文（`instructions` 缺失时使用），`skill.json` 的 `instructions` 优先。
- 仅 `SKILL.md`：可选 `---` front-matter（含 `- ` 列表语法），`id` 默认目录名，`version` 默认 `0.0.0`，
  `name` 默认标题/目录名。
- 非法 manifest 只让该条目 `invalid`（`error` 给原因），不拖垮整个 registry；重复 id、超过 `max_skills`
  也标 invalid。
- 状态文件：`<state_dir>/state/skills.json`（`global.enabled/disabled`、`projects.<pid>.enabled/disabled`），
  `config.skills.enabled` 作为全局启用的声明基线。

**Agent 接线建议**：在组装 system prompt 时调用
`app.skills.render_injection(project_id=..., role=..., prompt=user_text)`，为空则不追加；
`unit.state_changed` 可用于下一轮刷新，不需要每步重扫。

### 3.3 PluginLoader — `app.plugin_loader`（别名 `app.plugins`）

```python
class PluginLoader:
    def discover(self) -> list[PluginRecord]
    def list(self) -> list[PluginRecord]
    def get(self, plugin_id) -> PluginRecord
    async def load(self, plugin_id) -> PluginRecord
    async def unload(self, plugin_id) -> PluginRecord
    async def restart(self, plugin_id) -> PluginRecord
    async def on_config(self, config) -> None
    def units(self) -> list[dict]
```

Manifest：`stackflow.plugin.json`，校验 `id`（唯一、`[A-Za-z0-9._-]`）、`name`、`version`、`entry
{type:"python", module:"module:callable"}`、`runtime`（只支持 `in_process`）、`requires`、`contributes
{tools,skills}`、`permissions`、`config_schema`。`contributes` 是声明性元数据；**实际注册以
`provides.tool()/provides.skill()` 为准**（示例插件同时声明和注册）。

`apply(ctx, provides)` 必须是同步函数；loader 用 `app.ctx.child("plugin.<id>")` + `ctx.effect(...)`
执行，`provides` 注册的目录条目/executor/skill 都挂在子 context 的 disposer 上，`unload` 时原子注销。
`provides.tool(name, description=..., capability=..., risk=..., schema=..., handler=...)` 的 `handler`
签名为 `handler(args: dict, *, principal=None, project_id=None)`（也支持 `**kwargs`），返回值必须可 JSON 序列化。

启动加载由 `config.plugins.enabled` 控制（支持 `["*"]`）；`GET/POST /api/plugins*` 由 operator 操作；
事件 `plugin.loaded` / `plugin.unloaded` / `plugin.failed` / `plugin.invalid`。

### 3.4 MCP — `app.mcp_host`（别名 `app.mcp`）

```python
class MCPHost:
    async def start(self) / stop(self) / on_config(self, config)
    def servers(self) -> list[ServerState]
    def get(self, name) -> ServerState
    async def connect(self, name, *, manual=True) -> dict
    async def disconnect(self, name, *, manual=True) -> dict
    async def call_tool(self, server, tool, args=None, *, project_id=None,
                        principal=None, approval_id=None) -> dict
    async def call_with_approval(...) -> dict   # call_tool 的同义名
    def units(self) -> list[dict]
    def capabilities(self, kind=None) -> list[dict]
```

`config.mcp.servers` 每项支持 `name/command/args/env/cwd/enabled/framing(newline|content-length)`。
`mcp.<name>` 是真实子进程单元；`call_tool`/`call_with_approval` 走 `app.approvals`：principal 为 `mcp.<name>`（非 operator），
capability 暂用 `shell.exec`（high risk），`scope.command="mcp.<name>:<tool>"`；pending 返回 202 且不执行，
调用方带 `approval_id` 重试；每次成功/失败发 `mcp.tool.called` / `mcp.tool.failed` 审计事件。进程退出由
reader 检出 → 单元 `failed`，monitor 每 5s 按 5s→…→60s backoff 重连；手动 `disconnect` 不再自动重连。

### 3.5 Unit control — `app.unit_control`（别名 `app.units_control`）

```python
class UnitStateStore: ensure/get/states/observe/set_state/disable/enable/drain/undrain
class HealthTicker:   start/stop/tick/probe_unit         # 5s，单单元 2s 超时
class UnitController:
    def register_provider(self, unit_id, *, probe=None, restart=None, disable=None, enable=None)
    def state_of(self, unit_id) -> str | None
    def states(self) -> dict[str, str]
    def sync_state(self, unit_id, state, reason=None)
    def overlay(self, descriptor: dict) -> dict
    def summary(self) -> dict
    def detail(self, unit_id) -> dict
    async def act(self, unit_id, action) -> dict
    async def probe_now(self, unit_id) -> dict
class UnitClient:
    def register(self, unit_id, method, handler) -> Callable[[], None]
    def has(self, ref, method) -> bool
    async def call(self, ref, method, payload=None, *, timeout=8.0) -> Any
def parse_unit_ref(ref) -> str          # unit://<id> 或裸 id
```

`HealthTicker`：每 5s 对非 worker 单元逐个探针（`asyncio.wait_for` 超时），迟滞：连续 2 失败→`degraded`、
3→`failed`，连续 2 成功→`ready`；`disabled`/`draining` 跳过生命周期。所有状态变化发
`unit.state_changed`（EventLog + kernel bus），并写 `UnitController.recent_events`。
探针覆盖：`sys.0`（transport）、`store.eventlog`（events 目录可写）、`store.project`（可枚举）、
`store.memory`、`scheduler.0`、`gateway.0`/`context.0`（ctx 未 dispose）、`approval.0`（队列/policy）、
`llm.0`（provider/凭证）、`tool.0`（TOOLS 非空）；扩展单元用扩展注册的 probe。
支持 sticky `degraded`（如 invalid skill manifest、插件/server 失败）——不会被成功探针“洗白”。

动作只允许 descriptor `actions` 中声明的：`probe`；`restart`（worker → `supervisor.restart`；服务单元只有
注册了 rebuild hook 才执行，否则 409）；`disable`/`enable`（需 hook，disable 后 `summary.online` 剔除但描述符
保留）；`drain`/`undrain`（状态标记，draining 仍计入 online）。前端可用 `GET /api/units/summary` 获得
`summary.health/by_tier/problems`；`GET /api/units/{id}` 返回描述符 + 最近 20 条 `unit.state_changed` +
requires/wants/dependents 依赖图 + actions。

`UnitClient` 内置 `approval.0` 的 `policy.evaluate` / `approval.request` / `approval.decide` 与
`tool.0` 的 `tool.catalog`；测试证明注册 stub 后同一调用方代码无需改动（将 approval.0 拆进程时只替换
resolver 的 `transport.call` 分支即可）。subprocess/remote 单元走 `app.transport.call` 占位。

## 4. 新增 HTTP API

| Method | Path | 说明 |
|---|---|---|
| GET | `/api/capabilities?type=&project_id=` | 统一目录；返回 `{data:{capabilities,count,by_type}}` |
| GET | `/api/units/summary` | units-plan 口径的 `health/by_tier/problems`（核心 `/api/units` 保持旧字段） |
| GET | `/api/units/{id}` | 描述符 + 20 条状态事件 + 依赖图 + actions |
| POST | `/api/units/{id}/probe` | 立即探针 |
| POST | `/api/units/{id}/actions` | `{"action":"restart\|disable\|enable\|drain\|undrain\|probe"}` |
| GET | `/api/skills?project_id=` | skill 目录 + 校验状态 + summary |
| POST | `/api/skills/{id}/enable`、`/disable` | body `{"project_id":...}`，空=全局 |
| GET | `/api/plugins` | 插件目录、状态、贡献、单元 id |
| POST | `/api/plugins/{id}/load`、`/unload` | 受信 in-process 插件生命周期 |
| GET | `/api/mcp/servers` | 配置（env 仅 key）/连接状态/stderr tail |
| POST | `/api/mcp/servers/{name}/connect`、`/disconnect` | 生命周期 |
| GET | `/api/mcp/tools?server=` | MCP 工具目录 |
| POST | `/api/mcp/call` | `{"server","tool","args","project_id"?,"approval_id"?}`；pending=202 |

写操作（actions/load/unload/enable/disable/connect/disconnect/call）在 `current_auth().role` 存在且非
`operator` 时返回 400；无 auth（内部调用/测试）保持现有核心 API 行为。

## 5. 事件

`unit.state_changed`、`skill.scanned`、`skill.invalid`、`skill.enabled`、`skill.disabled`、
`plugin.loaded`、`plugin.unloaded`、`plugin.failed`、`plugin.invalid`、
`mcp.starting`、`mcp.connected`、`mcp.connect_failed`、`mcp.disconnected`、`mcp.tool.called`、`mcp.tool.failed`、
`capability.pending`、`capability.called`、`capability.failed`。
所有事件经 `emit_both` 同时写 EventLog 与 kernel bus（SSE 可见）；字符串字段先 `redact_text`。

## 6. 配置（已由 config.py 提供，未改）

`skills`：`dirs`（相对 `state_dir`）、`enabled`、`inject_max_chars`、`max_skills`。
`plugins`：`dirs`、`enabled`（`["*"]` 全载）、`max_plugins`、`allow_untrusted`（保留；本轮只支持受信 in-process）。
`mcp`：`servers[]`、`call_timeout_s`、`max_servers`、`max_tools`。
`on_config` 已实现：skills 重新扫描、plugins 重载期望集合、MCP 重连仍期望的 server。

## 7. 测试结果

命令（按 team-plan 约定，只跑本域）：

```
/tmp/sfo-venv/bin/python -m pytest -q \
  tests/test_units_capabilities.py tests/test_units_skills.py \
  tests/test_units_plugins.py tests/test_units_mcp.py \
  tests/test_units_control.py tests/test_units_visibility.py \
  tests/test_registry.py --basetemp=/tmp/pytest-units
```

结果：**65 passed**。另跑相关既有套件：
`tests/test_extensions.py tests/test_web.py tests/test_integration.py tests/test_sse.py
tests/test_self_hosting_contract.py tests/test_config.py tests/test_context_dispose.py`
→ **59 passed + 42 subtests passed**。

覆盖点：TOOLS 全量目录/provider/risk/schema；provider failed → available=false；项目 allowlist 不泄漏；
skill front-matter/JSON/非法 manifest/重复/0 个；注入 role/project/trigger/字符截断；项目级 enable/disable；
plugin 示例加载→工具/skill 出现→unload 消失→reload；apply 异常→`plugin.<id>` failed + `plugin.0` degraded；
声明 contributes 但未注册→`plugin.<id>` degraded；
invalid manifest 隔离；插件工具审批（pending→allow once→执行→再次重试用同一 approval 失败）、RPC `capability.call` 桥接；
MCP newline 与 Content-Length、initialize/tools/list/tools/call、timeout、stderr 脱敏、崩溃 failed、
mcp.0 degraded、手动重连、`/api/mcp/call` 202→批准→执行；迟滞 2/3/2、sticky degraded、ticker 事件、
summary health down、probe timeout；worker restart 走 supervisor；UnitClient 真实调用、stub 替换、approval.request 及 NNG 占位路径。

## 8. 需要 Lead 接线/决定的点

1. **`capability.call` RPC action**：在 `app.py` 注册 handler 转发到
   `app.capability_catalog.call_capability(...)`（见 §3.1 代码），并把可选 `approval_id` 从 RPC data 透传。
2. **Agent 提示词**：在 worker/agent_chat 组装 system prompt 时调用
   `app.skills.render_injection(project_id=..., role=..., prompt=user_text)`；这是纯字符串函数，不依赖 hub。
3. **前端**：单元页的三层分组/详情/动作使用 `/api/units` + `/api/units/summary` + `/api/units/{id}`、
   `/api/units/{id}/actions`；`/api/units` 核心响应仍无 summary 字段（web/api.py 不动，summary 走
   `/api/units/summary`）。如果希望 `/api/units` 直接带 `summary`，需要 Lead 在 `web/api.py` 里调用
   `app.unit_control.summary()`（一行）。
4. **metrics（ops）**：`/api/metrics` 可直接用 `app.unit_control.summary()` 与 `states()`，无需重算探针。
5. **安全 capability（sec）**：MCP/插件执行目前借用 `shell.exec`（high）做审批。如要语义更准确，可在
   `security/capabilities.py` 增加 `mcp.tool`/`plugin.tool` capability；本扩展的 `ensure_approved` 已通过
   条目 `capability` 字段抽象，改闭集后无需改调用方。
6. **MCP server 名称**：`0` 保留给 `mcp.0` 宿主；配置里 `name="0"` 会被忽略并告警。
7. **worker restart**：`POST /api/units/{worker}/actions {"action":"restart"}` 会调用
   `supervisor.restart(unit, 1)`；失败/worker 未注册时返回 409。

## 9. 残余风险与已知限制

1. **核心单元在 `/api/units` 的 state 仍由 app.py 计算**：控制模块的迟滞状态会出现在 `/api/units/{id}`、
   `/api/units/summary`、`unit.state_changed` 和事件里，但核心 `/api/units` 列表仍是 app.logical_units 的
   即时探针结果（例如 eventlog 目录不可写会在列表里立即 degraded，而 summary 要等 2 次/3 次失败）。
   统一口径需要 Lead 改 `web/api.py` 的 `units_list` 使用 `app.unit_control.overlay/summary`（或 app.py
   的 logical_units），我未越界修改。
2. **插件只支持受信 in-process**：`runtime=subprocess`、`allow_untrusted` 都不支持；manifest 的
   `permissions` 只做记录/展示，未做网络/文件/密钥强制隔离。插件代码可影响 hub 进程，示例与测试只加载
   仓库内受信插件。
3. **MCP 审批 capability 为借用**：`shell.exec` high risk 的语义不精确（风险方向保守，不会放行更宽）；
   `mcp.call` 的 principal 固定为 `mcp.<name>` 而非 operator，符合“非 operator principal”的要求。
4. **MCP 重连**：monitor 5s 周期 + 指数 backoff；测试只验证手动重连 + 状态，不等待真实 5s 自动重连。
   `Content-Length` 是兼容 LSP 风格，MCP 规范默认 newline；发送 framing 由 server 配置决定，读取自动嗅探。
5. **skill 注入是字符上限**，未做 token 精确计数（按字符更保守；agent 侧可再按 token 截断）。
6. **`/api/skills` 与 `/api/capabilities` 无分页**：受 `max_skills/max_plugins/max_tools` 限制，规模大时
   单次响应会增长；本轮不引入分页。
7. **`unit.state_changed` 每次状态变化都写 EventLog**，抖动已被迟滞抑制，但探针故障仍会持续发事件；
   如需限流可后续在 ops/metrics 层聚合。
8. **drain/undrain 是生命周期标记**：没有任务路由/在途计数，draining 只改变状态并计入 online；真正的
   排空编排需要 scheduler/agent 层配合（本模块只提供统一动作入口）。
9. **UnitClient 的 NNG 分支是占位**：依赖 `app.transport.call`，没有独立契约测试；拆进程时应补同一套
   inproc/NNG 契约测试（本轮至少证明了 approval.0 stub 可无改调用方替换）。
