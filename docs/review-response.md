# 评审问题修复对照表

> 对应 2026-11-24 用户给出的六类评审结论。状态在集成验收后更新为「已修复 / 部分 / 未做（说明）」。
> 工作流见 `docs/team-plan.md`；逐小时记录见 `docs/worklog.md`。

## 一、规划了但没有实现的单元

| # | 评审条目 | 负责 | 修复内容 | 状态 |
|---|---|---|---|---|
| U1 | `skill.0` 状态写死 disabled，没有扫描/校验/版本/项目启用/注入 | units | `skills/`：目录扫描、manifest 校验、版本、项目级启用、状态文件、注入渲染；`skill.0` 状态由真实扫描推导 | 进行中 |
| U2 | `plugin.0` 插件数固定 0，没有 PluginLoader/manifest | units | `plugins/`：manifest 校验、`plugin.<id>` 单元、in-process Cordis apply、load/unload、贡献目录项、示例插件 | 进行中 |
| U3 | `mcp.0` 只有占位，没有协议、`mcp.<name>`、`mcp.call_tool` | units | `mcp/`：stdio JSON-RPC、initialize/tools/list/tools/call、生命周期单元、能力目录、审批/审计 | 进行中 |
| U4 | 没有 `/api/capabilities`，TOOLS 静态 | units | 统一能力目录，TOOLS 注册为 `tool.0` provider，`available` 由 provider 状态推导，项目 allowlist | 进行中 |
| U5 | 统一重启/熔断/配额：LLM RPM/TPM/预算、Agent 超时/工具次数 | llm/agent | LLM 限流/预算/熔断；Agent 任务超时、工具调用上限、统一重启策略接入 | 进行中 |
| U6 | 单元动作 API：probe/restart/disable/enable/drain | units | `/api/units/{id}/probe`、`/actions`，动作白名单，状态事件 | 进行中 |
| U7 | UnitClient 与 `unit://` 引用解析只是命名约定 | units | UnitClient + inproc/NNG 同一接口解析器；approval 调用可替换 stub 的契约测试 | 进行中 |
| U8 | 服务单元是逻辑单元，无独立失败域/依赖图 | units | 真实探针 + 迟滞状态机 + requires/wants 依赖图 + 状态事件；独立进程化列为长期项 | 部分（进程内） |
| U9 | 阶段 3：审批拆进程、沙箱执行器、不受信插件子进程、llm-gateway、状态回放、灰度 | sec/agent/units | 沙箱执行器（Landlock/bwrap）、worker 沙箱、灰度用 drain；审批拆进程/插件子进程/llm-gateway 明确为后续 | 部分 |

## 二、LLM 与 Agent 能力

| # | 评审条目 | 负责 | 修复内容 | 状态 |
|---|---|---|---|---|
| L1 | 没有原生 tool calling（`tools` 被丢弃） | llm | Provider 原生 tools/tool_calls（OpenAI/Anthropic/Gemini），JSON 文本回退保留 | 进行中 |
| L2 | 只有 OpenAI 兼容一种 provider | llm | 新增 Anthropic Messages、Gemini generateContent 原生实现 | 进行中 |
| L3 | 没有流式输出 | llm | provider stream → `chat.delta`/`agent.chat.delta` SSE 增量 | 进行中 |
| L4 | 没有 token 计数/上下文窗口/成本/限流 | llm | usage 统计持久化、上下文裁剪、成本、RPM/TPM/并发/项目预算、熔断 | 进行中 |
| L5 | 默认 mock 大脑不是智能 | — | mock 明确标注为确定性离线替身，不冒充智能；真实能力需配置 provider（文档） | 说明 |
| L6 | reviewer 缺少 fs.write（§14） | agent | reviewer 工具表补 fs.write，仍受路径/审批约束 | 进行中 |
| L7 | 没有多模型路由/降级链 | llm | `llm.routes` 按角色路由 + `llm.fallback` 降级链 | 进行中 |
| L8 | 已运行 Agent 不热更新配置 | agent | hub→worker 配置推送 + 周期对账；限制与模型配置生效 | 进行中 |
| L9 | Agent 数量上限写死，无优先级排队 | agent | 优先级队列 `agents.queue_max`、自动补位、队列 API/事件 | 进行中 |
| L10 | 没有 git 集成（attached-rw 分支隔离/回滚/diff） | agent | 每任务分支、提交、diff、rollback；`/api/projects/{id}/git*` | 进行中 |

## 三、安全与远程使用

| # | 评审条目 | 负责 | 修复内容 | 状态 |
|---|---|---|---|---|
| S1 | TLS 监听未实现 | sec | `web.tls_cert/key` 真正启用 HTTPS 监听，secure cookie，TLS 版本下限 | 进行中 |
| S2 | 没有 OS 沙箱 | sec/agent | Landlock/bwrap 能力检测与命令包装；shell.exec 与 worker 进程接线；诚实降级 | 进行中 |
| S3 | worker 本地执行，恶意 worker 可绕过检查 | sec/agent | worker 进程 Landlock 写白名单（workspace/tmp/logs），hub 侧审批仍强制；文档写残余风险 | 进行中 |
| S4 | launch token 可重复兑换 | sec | 默认单次兑换 + operator control socket（同 uid）+ `sfo url` 铸造新 token | 进行中 |
| S5 | lease.acquire 的 resource 可写别人项目 | dist/lead | 租约资源作用域校验 + fencing token | 进行中 |
| S6 | operator._audit 的 result/error 未脱敏 | sec | 递归脱敏 + 截断，审计仍 append-only | 进行中 |
| S7 | net.fetch 存在 DNS rebinding TOCTOU | sec | 解析一次固定 IP 连接，逐跳重定向重新校验 | 进行中 |
| S8 | 只有单用户，无角色/账号 | sec | `web.users` token_sha256 + operator/viewer 角色 + 服务端会话表 | 进行中 |
| S9 | 同用户进程仍可读写状态/发信号 | — | 明确残余风险（OS 用户边界假设），README/架构文档写明 | 说明 |

## 四、分布式与基础设施

| # | 评审条目 | 负责 | 修复内容 | 状态 |
|---|---|---|---|---|
| D1 | 单 hub，无选主/集群，重启期间暂停 | dist | `cluster.standby` 同状态目录冷备进程级 failover（非 Raft），文档写清 | 进行中 |
| D2 | 没有设备发现（mDNS/注册中心） | dist | stdlib mDNS advertise/browse + `/api/discovery/peers` | 进行中 |
| D3 | 租约只有 TTL，时钟偏移可能双持有 | dist | fencing token、时钟偏移容忍、幂等校验 | 进行中 |
| D4 | 事件流单 JSONL，无归档/压缩/轮转 | dist | 按大小轮转 gzip 归档，query/since/tail 跨归档 | 进行中 |
| D5 | 没有存储后端抽象（JSON 文件，无事务/迁移） | dist | storage 抽象 + SQLite backend（eventlog/memory/counters）+ 迁移入口 | 进行中 |
| D6 | 与 StackFlow 不互通（pynng vs ZMQ） | dist | 可选 pyzmq ZMQ bridge + 脚本（import guard） | 进行中 |
| D7 | 平台限制：无容器/systemd/打包；pynng 不默认 | dist | Dockerfile、systemd unit、部署文档；pynng 作为依赖声明已存在 | 进行中 |

## 五、审批与运营

| # | 评审条目 | 负责 | 修复内容 | 状态 |
|---|---|---|---|---|
| O1 | 审批依赖人在线，无通知渠道 | ops | webhook 通知 sink（事件过滤/超时/重试/脱敏）+ 测试接口 | 进行中 |
| O2 | 没有预算/配额/优先级/任务依赖可视化 | llm/agent/front | LLM 预算面板、Agent 队列、任务依赖图 | 进行中 |
| O3 | 没有监控导出（Prometheus/OTel/告警） | ops | `/api/metrics` Prometheus 文本指标；告警通过 webhook | 进行中 |
| O4 | 没有备份/恢复/导入导出 | ops | tar.gz 备份/安全恢复/事件导出 + CLI | 进行中 |

## 六、前端

| # | 评审条目 | 负责 | 修复内容 | 状态 |
|---|---|---|---|---|
| F1 | 原生 JS 无构建步骤 | — | 保留约束（不引构建），模块化原生 JS | 说明 |
| F2 | 没有认证后会话管理 UI | sec/front | 会话列表/轮换/撤销/登出、当前角色 | 进行中 |
| F3 | 单元页缺详情抽屉/依赖图/动作按钮 | units/front | 详情抽屉、requires/wants 图、状态事件、动作按钮 | 进行中 |
| F4 | 没有国际化 | front | zh/en 字典 + t() + data-i18n + 语言切换，缺词回退 | 进行中 |
