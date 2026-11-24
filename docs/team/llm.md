# LLM 工作流交接（llm）

> 任务板：`task-2`（LLM：原生 tool calling、Anthropic/Gemini、流式、token/配额/路由/降级）
> 所有权：`stackflowos/ai/**`、`stackflowos/session/chat.py`、`stackflowos/session/agent_chat.py`、
> `tests/test_llm_*.py`、`tests/test_chat_*.py`、本文档。
> 未修改 `app.py`、`config.py`、`web/api.py`、`agents/**`、`l2/eventlog.py`，未运行任何 git 写操作。

## 1. 改动文件

新增：

| 文件 | 作用 |
|---|---|
| `stackflowos/ai/types.py` | `LLMResult` / `Usage` / `ToolCall`，OpenAI/Anthropic/Gemini 工具调用归一化 |
| `stackflowos/ai/telemetry.py` | `UsageTracker`（按 project/role/provider/model 聚合、原子持久化）、`LLMRuntime`、token 估算、`attach_provider_runtime` |
| `stackflowos/ai/limits.py` | `RateLimiter`（asyncio 信号量 + RPM/TPM 滑动窗口）、`CircuitBreaker`、`RateLimited` |
| `stackflowos/ai/context.py` | `trim_messages` 上下文裁剪、工具结果截断、token 估算 |
| `stackflowos/ai/providers.py` | `BaseProvider`、OpenAI/Anthropic/Gemini/Mock 四种实现，原生 tools 与 SSE |
| `stackflowos/ai/factory.py` | spec → provider 构造、route/fallback 合并 |
| `stackflowos/ai/router.py` | `Router`：按 role 路由 + `llm.fallback` 降级 + 熔断跳过 |
| `stackflowos/ai/llm_api.py` | 扩展模块：`build(app)` + `GET /api/llm/usage`、`GET /api/llm/models` |
| `tests/test_llm_native.py` | 原生工具调用、三家 provider、SSE、错误映射/脱敏、usage 持久化 |
| `tests/test_llm_router.py` | 路由/降级/预算/限流/熔断/裁剪 |
| `tests/test_llm_api.py` | 扩展 API 形状 |
| `tests/test_chat_streaming.py` | ChatService/AgentChatService 的 `chat.delta` / `agent.chat.delta` 流式接线 |

修改：

| 文件 | 改动 |
|---|---|
| `stackflowos/ai/llm.py` | 重写：`make_provider`、`complete()`、`LLMBrain` 原生工具优先、结构化结果导出；保留 `chat()` 返回 `str` |
| `stackflowos/ai/__init__.py` | 导出新类型/provider/Router |
| `stackflowos/session/chat.py` | provider 运行时挂钩、`on_delta` → ephemeral `chat.delta`、无重复模拟流、`on_config` 热更新重挂 |
| `stackflowos/session/agent_chat.py` | 原生 tool loop（tool role + `tool_call_id`）、prose 增量 → `agent.chat.delta`、`on_config` |
| `docs/team/llm.md` | 本文档 |

## 2. 接口

### 2.1 结构化结果

```python
LLMResult(
    text: str,             # 兼容旧接口；properties: result.content == result.text
    tool_calls: list[ToolCall],   # ToolCall(id, name, arguments: dict, raw_arguments)
    usage: Usage,          # Usage(prompt, completion, total)
    finish_reason: str,    # stop | tool_calls | length | content_filter
    model: str,
    provider: str,
    raw: dict | None,
)
```

### 2.2 Provider 接口

```python
async chat(messages, *, tools=None, on_delta=None, role=None, project_id=None) -> str
async chat_with_tools(messages, *, tools=None, tool_choice=None,
                      role=None, project_id=None, on_delta=None,
                      stream=None) -> LLMResult
async stream(messages, *, tools=None, tool_choice=None,
             role=None, project_id=None) -> AsyncIterator[dict]
async ping() -> dict
health() -> dict           # calls/failures/last_latency_ms/circuit/limiter
describe() -> dict         # 不含 api_key
```

`stream()` 的 delta 形状：

```python
{"type": "text", "text": "..."}
{"type": "tool_call", "index": 0, "id": "...", "name": "...", "arguments": "<json fragment>"}
{"type": "usage", "usage": {"prompt": 1, "completion": 2, "total": 3}}
{"type": "finish", "finish_reason": "tool_calls"}
```

`chat_with_tools(..., on_delta=cb)` 在 `llm.stream=True` 时内部走 SSE，`cb` 收到同样的 delta；
`llm.stream=False` 时走非流式请求，不产生任何 provider 增量（旧行为由 ChatService 的 48 字模拟分片保留）。
三家 provider 都走 `urllib` + `asyncio.to_thread`（SSE 用读取线程 + `asyncio.Queue`），无第三方依赖。

### 2.3 工厂 / 路由

```python
make_provider(cfg)  # 无 routes/fallback → 单一 provider；有 → Router
```

- `llm.provider` 支持 `mock | openai | anthropic | gemini`（`openai-compatible` 等别名保留）。
- `llm.routes`：`{"role": "coder", "provider": "anthropic", "model": "...", "base_url": "...", "api_key": "...", "temperature": ..., "max_tokens": ...}`；也支持 `roles: [...]`。
- `llm.fallback`：按顺序尝试；每个 entry 可覆盖 provider/base_url/model/api_key/temperature/max_tokens/价格/熔断参数。
- 路由选择顺序：role route → primary → fallback。熔断打开（且冷却未到）的 provider 被跳过。
- 预算超限（`BudgetExceeded`）**不会**降级，直接 fail-closed。
- `Router` 暴露 `name/model/usage_snapshot()/health()/describe()/provider_for(role)`。
- 热更新：`App.update_config` 已经 `make_provider(candidate.llm)` 并替换 `app.provider/chat/agent_chat.provider`；
  `ChatService/AgentChatService.on_config` 负责把新 provider 重新挂到共享 usage runtime，计数器不重置。

### 2.4 流式事件接线

- `ChatService`：provider 文本增量 → `eventlog.append("chat.delta", {"message_id", "delta", "role": "assistant"})`；
  最终 `chat.message` 仍完整持久化。没有 provider 增量时才走原 48 字模拟分片。
- `AgentChatService`：工具循环里 provider 增量经 prose 过滤器（抑制以 `{`/`[`/`<`/``` 开头的动作 JSON）→
  `agent.chat.delta`；最终 `agent.chat.message` 持久化。`llm.stream=False` 时行为与旧实现一致。
- 两类事件都在 `EventLog.EPHEMERAL_TYPES` 中，不落盘；未新增 HTTP 流端点。

## 3. 配置（读取现有 `LLMCfg`，未改 config.py）

`stream, rpm, tpm, max_concurrency, max_context_tokens, project_token_budget, fallback[], routes[], retries, retry_backoff_s`
以及 `provider/base_url/model/api_key/temperature/max_tokens/timeout_s`。

route/fallback entry 额外识别（不要求主配置有这些字段）：

- `price_per_1k_input` / `price_per_1k_output`：成本统计
- `circuit_threshold` / `circuit_cooldown_s`：熔断阈值与冷却（默认 3 次 / 30s）

token 估算使用保守的 `chars/3`（`telemetry.CHARS_PER_TOKEN`），无 tokenizer 依赖。

## 4. 持久化 / 限流语义

- usage 文件：`<state_dir>/state/llm_usage.json`，`write_json` 原子替换；`UsageTracker` 用 `threading.RLock` 保护。
- 聚合：`totals`、`projects`、`roles`、`providers`、`models`、`recent`（最近 200 条）；
  重启后由 `UsageTracker` 从文件恢复。
- 预算：`llm.project_token_budget` 按 project 已用 token + 本次 prompt 估算判断；超限抛
  `BudgetExceeded`（`Unavailable` 语义，http 429），Router 不降级。
- RPM/TPM：60s 滑动窗口 + 并发信号量；超限抛 `RateLimited`（`LLM HTTP 429` 前缀，可重试语义）。
- 熔断：连续 transient 失败（429/5xx/连接/超时）达到阈值 open；冷却后 half-open 放一个探测。
- 错误：HTTP 429/5xx 可重试；4xx 不重试；错误正文中的 api_key 用 `***` 脱敏；URL 不含 key
  （Gemini 用 `x-goog-api-key` header，Anthropic 用 `x-api-key` header）。

## 5. API 扩展

扩展模块 `stackflowos.ai.llm_api:build(app)` 已在 `extensions.EXTENSION_MODULES` 中（Lead 接线）。
路由（`routes()`，绑定方法）：

- `GET /api/llm/usage?project_id=` → `{ok, data:{project_id, totals, projects, roles, providers,
  models, recent, project, cost, budget{project_token_budget, used, remaining, exceeded},
  limits{rpm/tpm/used/...}, circuit{state,...}, providers_health[], updated_at}}`
- `GET /api/llm/models` → `{ok, data:{primary, providers[], health, configured{...}, routes[], fallback[]}}`
  （只含缓存的最近延迟/失败/熔断状态；不主动打网络，不含 `api_key`）。

前端已有的 SSE `/api/events/stream` 直接消费 `chat.delta` / `agent.chat.delta`，无需新端点。

## 6. 测试

命令：

```bash
/tmp/sfo-venv/bin/python -m pytest -q tests/test_llm*.py tests/test_chat*.py tests/test_sse.py \
  --basetemp=/tmp/pytest-llm
```

结果：**81 passed + 26 subtests passed**（2026-11-24，本机）。另跑过相关集成回归：
`tests/test_extensions.py tests/test_web.py tests/test_integration.py` → 27 passed。

覆盖点：

- OpenAI 原生 `tools/tool_choice`、`choices[0].message.tool_calls`、`usage`；`chat()` 仍返回 str。
- Anthropic `/v1/messages`（x-api-key/anthropic-version/input_schema/tool_use）与 tool_result 回填。
- Gemini `:generateContent`（functionDeclarations/functionCall/systemInstruction/usageMetadata）与 `alt=sse`。
- OpenAI/Anthropic/Gemini SSE 增量、tool_call 分片聚合、usage/finish delta。
- 429/5xx 重试语义、错误脱敏、非流式 `llm.stream=False` 不发 stream 参数。
- Router role 路由、fallback、熔断跳过、预算 fail-closed、限流（RPM/TPM/并发）、裁剪事件、usage 持久化/重启恢复。
- `LLMBrain`：原生 tool_calls → Action、observe 用 tool role/tool_call_id、无 tool_calls 回退 JSON 文本。
- ChatService/AgentChatService 真实增量写入 ephemeral delta，最终消息只持久化一次。

## 7. 需要 Lead 注意 / 残余风险

1. **Worker 进程的 LLMBrain usage 不落主状态目录**：`units/worker.py` 每进程 `make_provider(cfg.llm)`，
   没有把 `state_dir` 传给 runtime；worker 的 token 只在本进程内存。若要把 worker 用量汇总到 hub，
   需要在 worker 初始化时调用 `stackflowos.ai.telemetry.get_runtime().configure(state_dir=...)`
   （或由 Lead 在 worker 侧接线）。hub 内 chat/agent_chat 的用量已持久化。
2. `stackflowos.ai.llm_api` 已由 Lead 加入 `EXTENSION_MODULES`；若后续调整扩展顺序，注意它依赖
   `app.provider` 已创建（`load_extensions` 在核心服务之后，满足）。
3. 成本价格只能通过 route/fallback entry 的 `price_per_1k_input/output` 配置；主配置 `LLMCfg`
   没有这两个字段（Lead 拥有 config.py）。要支持 primary 价格，需要 Lead 加字段。
4. 流式降级只在**首个 delta 之前**重试；一旦向用户输出了部分文本，再失败会直接报错，避免拼接两个模型的内容。
5. 上下文裁剪使用 chars/3 启发式；对代码/CJK 是保守估计（可能多裁），不会超窗。若以后接入真实
   tokenizer，只需替换 `telemetry.estimate_tokens`。
6. 熔断阈值/冷却对 primary 使用默认 3 次/30s（`LLMCfg` 没有对应字段）；route/fallback entry 可覆盖。
7. `chat.delta`/`agent.chat.delta` 是 ephemeral 事件，重启后不能回放；前端应以最终
   `chat.message`/`agent.chat.message` 为准（现有 UI 已这样处理流式气泡）。
