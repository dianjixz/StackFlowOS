# StackFlowOS 自举与自改进指南

本文说明如何用 StackFlowOS 管理 StackFlowOS 自己：把已有仓库接入为项目，让操作员助手与工人 Agent 在同一套权限/审批/事件约束下修改本仓库，并跑通测试与端到端验证。

适用对象：本仓库的操作员（人类）与开发者 Agent。

---

## 1. 把已有仓库接入为项目

StackFlowOS 的项目默认有两种来源：新建空 workspace，或**接入一个已存在的本地目录**（本仓库就是这样接入的）。三种等价入口：

### 1.1 通过 `project.attach` 能力

由操作员助手（对话框）发起，参数是宿主机上的绝对路径、可选的名称/目标，以及是否允许写入：

```json
{"tool": "project.attach", "args": {"path": "/home/nihao/work/workspace/StackFlowOS", "name": "StackFlowOS", "allow_writes": true}}
```

`project.attach` 的参数是 `path`（必填）、`name?`、`goal?`、`allow_writes?`（默认 `false`，即只读接入），**没有 `mode` 参数**。

### 1.2 通过项目页的「接入已有目录」

1. 打开项目页 → 点「接入已有目录」；
2. 填入目录绝对路径（例如 `/home/nihao/work/workspace/StackFlowOS`）；
3. 勾选「允许 Agent 写入」（不勾选即只读接入；界面上没有模式下拉框）；
4. 确认后系统会创建项目记录，workspace 指向该目录（不复制、不移动原仓库）。

### 1.3 通过对话

在对话框里直接说：

```
打开 /home/nihao/work/workspace/StackFlowOS 这个项目
```

助手会解析路径并调用 `project.attach`。若该路径已在项目中注册，则直接切换过去。

### 1.4 `attached-ro` 与 `attached-rw` 的区别

| 方面 | `attached-ro`（只读接入） | `attached-rw`（可写接入） |
| --- | --- | --- |
| 读取文件（`fs.read`/`fs.list`） | 允许 | 允许 |
| `shell.exec` | **一律拒绝**（即使命令本身只是只读，如 `git log`、`ls`） | 允许（命令须留在 workspace 内，危险命令仍需审批） |
| 写文件（`fs.write`/`fs.delete`） | 拒绝，需先由操作员切换为可写 | 允许（仍在 workspace 边界内） |
| 典型用途 | 代码审阅、只读调研、生成实现说明 | 自举开发、修 bug、改文档 |

要点：

- 所有路径必须留在项目 workspace 内，`../` 逃逸会被拒绝；
- 读写开关存放在项目记录上（`workspace_mode`，由 `allow_writes` 设置）；切换由操作员的 `project.access` 直接执行，写 `operator.action` / `project.access.changed` 事件，**不走审批**；
- 只读模式下想让 Agent 改代码，先由操作员切换为可写（`project.access` 或 UI 勾选「允许 Agent 写入」），不要让 Agent 换路径绕过审批。

### 1.5 切换读写：`project.access` 与 `POST /api/projects/{id}/access`

操作员可随时切换已接入目录的读写模式：

```json
{"tool": "project.access", "args": {"project_id": "p-3", "allow_writes": true}}
```

- 参数：`project_id`、`allow_writes`（布尔，必填）；
- 由操作员**直接执行**，不会生成审批单，也不需要审批；审计落在 `operator.action` 与 `project.access.changed` 事件里；
- HTTP 等价入口：`POST /api/projects/{id}/access`，请求体 `{"allow_writes": true}` 或 `{"allow_writes": false}`。

切换为只读后，工人 Agent 的 `fs.write` / `fs.delete` 与所有 `shell.exec` 都会被拒绝。

---

## 2. 操作员助手与工人 Agent 的权限边界

系统里有两类行动者，能力集不同：

**操作员助手（对话框）**——面向人类，能力更宽：

- 项目管理：`project.create` / `project.attach` / `project.update` / `project.archive` / `project.delete`；
- 任务编排：`task.create`、`task.list`、`task.update`；
- 拉起/终止工人：`agent.spawn`、`agent.list`、`agent.kill`；
- 代操作员执行敏感动作：`operator.action`（例如确认高危命令、批准审批单）。

**工人 Agent（coder / tester / lead 等）**——随任务生灭，能力窄且有边界：

- 只能读写项目 workspace 内的文件（`fs.read` / `fs.list` / `fs.write` / `fs.delete`）；
- `shell.exec` 限于 workspace 内、不联网、不执行危险命令；
- 可写自己任务的产物与备注：`task.artifact` / `task.note` / `task.update`；
- 不能创建项目、不能拉起其他 Agent、不能修改权限策略。

### `operator.action` 审计

凡是工人无法自行完成的敏感动作，都收敛到操作员的 `operator.action`，并写审计事件：

```json
{"tool": "operator.action", "args": {"action": "approve", "request_id": "apr-xxxxxxxx", "note": "确认修改 CI 配置"}}
```

每次审批都会在事件流留下 `approval.request` / `approval.auto` / `approval.decided` 记录，包含：`request_id`、`principal`（哪个 Agent）、`capability`、`scope`（project_id / path）、`status`。审计查询：

```bash
# 查看最近事件（按 seq 增量拉取）
curl -s 'http://127.0.0.1:17800/api/events?since=0&limit=100'
```

规则：工人遇到「工具被拒绝」时，必须如实上报并停下，**不得换路径或换工具绕过审批**。

---

## 3. 用 Agent 修改本仓库的流程

自改进的循环：任务 → 工人 → 产物 → 评审 → 合并。

### 3.1 建任务

```json
{"tool": "task.create", "args": {"project_id": "p-3", "title": "修复 X", "description": "只改 src/x.py，跑 python3 -m py_compile src/x.py", "role": "coder"}}
```

任务描述里写清：**允许改哪些文件**、**用什么命令自检**、**完成的判定标准**。

### 3.2 拉起工人

```json
{"tool": "agent.spawn", "args": {"project_id": "p-3", "role": "coder", "task_id": "t-14"}}
```

### 3.3 规则

- 一次只做一个动作（一次一个工具调用）；
- 路径必须留在项目 workspace 内；
- 不声称做过未执行的工作，不编造命令输出；
- 工具被拒绝时不绕过审批；
- 无可推进事项时立即 `finish` 并说明原因。

### 3.4 审批

高风险操作（写 `attached-ro` 项目、删除文件、危险 shell 命令、改权限/策略、归档或删除项目）走审批：

1. 工人发起 → 事件 `approval.request`；
2. 规则命中「自动批准」的白名单则记 `approval.auto`（如 `fs.list`、`fs.read` 在项目内只读）；
3. 否则等操作员 `operator.action approve/deny`；
4. 结果落 `approval.decided`，工人继续或被拒绝并上报。

### 3.5 租约（lease）

执行单元（`unit:coder.N` / `unit:tester.N`）靠租约保证同一资源不被并发写：

- 租约有 TTL，持有者周期性 `lease.renewed` 续约；
- 过期后资源可被其他单元抢占；
- 一个 workspace 的写操作应由单个持租约的单元完成，避免互相覆盖。

事件流里可以直接观察：

```bash
grep -o '"type": "lease\.[a-z]*"' /tmp/stackflow-events.log | sort | uniq -c
```

### 3.6 失败域

- **单个工人失败**只影响它自己的任务：`finish` 报 `failed`/`blocked`，其他任务继续；
- **工具级失败**（拒绝、超时）记在该 step 上，不污染项目状态；
- **审批超时**视为拒绝，工人应停下并上报，不重试绕过；
- 任务卡住时由 lead 重新分配或由操作员 `agent.kill` 后重新 spawn。

### 3.7 事件流

所有动作（工具调用、审批、租约、Agent 生命周期）都写事件流，`seq` 单调递增，可回放、可审计：

```bash
# 全量导出
curl -s 'http://127.0.0.1:17800/api/events?since=0&limit=100000' > /tmp/events.json
# 只看某任务的步骤
curl -s 'http://127.0.0.1:17800/api/events?since=0&limit=100000' | jq '.data.events[] | select(.data.task_id=="t-14")'
```

---

## 4. 运行测试与端到端验证

所有命令都在项目 workspace 根目录执行。

### 4.1 语法自检（最小验证）

```bash
python3 -m py_compile $(git ls-files '*.py')
```

单文件：

```bash
python3 -m py_compile stackflowos/app.py
```

### 4.2 单元测试

```bash
python3 -m unittest discover -s tests -t .
```

只跑某个文件 / 某个用例：

```bash
python3 -m unittest tests.test_self_hosting_contract -v
python3 -m unittest tests.test_self_hosting_contract.SelfHostingContractTests.test_attach_existing_directory_is_read_only_and_not_copied -v
```

### 4.3 端到端验证

启动 hub 与工作节点（按本仓库的实际入口）：

```bash
python3 -m stackflowos hub --port 17800 &
```

冒烟检查：

```bash
curl -sf http://127.0.0.1:17800/api/health && echo OK
curl -s  http://127.0.0.1:17800/api/projects | jq
```

接入一个已有目录并验证只读语义（端到端契约）：

```bash
# 1) 以只读方式接入（attached-ro：不勾选「允许 Agent 写入」）
curl -s -X POST http://127.0.0.1:17800/api/projects/attach \
  -H 'content-type: application/json' \
  -d '{"path":"/tmp/demo-repo","name":"demo","allow_writes":false}' | jq

# 2) 让操作员助手读取项目文件（只读模式允许）
curl -s -X POST http://127.0.0.1:17800/api/chat \
  -H 'content-type: application/json' \
  -d '{"text":"读取 demo 项目的 README.md"}' | jq

# 3) 在 attached-ro 下派工人写文件：worker 侧直接拒绝，不产生审批单
#    直接用契约测试验证这三个工具都会被拒绝：
python3 -m unittest tests.test_self_hosting_contract -v
```

期望：第 2 步返回文件内容；第 3 步的契约测试通过（`fs.write` /
`fs.delete` / `shell.exec` 在 attached-ro 下均被拒绝，且不发 approval RPC）。

验证事件流有记录：

```bash
curl -s 'http://127.0.0.1:17800/api/events?since=0&limit=50' | jq '.data.events[].type' | sort | uniq -c
```

跑完整测试套件 + 类型检查（若已配置）：

```bash
python3 -m unittest discover -s tests -t . && python3 -m compileall -q stackflowos
```

### 4.4 清理

```bash
kill %1 %2 2>/dev/null; rm -rf /tmp/demo-repo
```

---

## 5. 已知限制

1. **工人 Agent 是可信的**：系统没有在沙箱层面强制隔离进程，规则与审批是「协作式」约束。恶意或幻觉的工人可能尝试越界，依赖审批与事件审计兜底，而不是内核隔离。
2. **单 hub**：只有一个中心 hub，没有多 hub 复制或 leader 选举；hub 是单点，重启期间调度暂停。
3. **无设备发现**：worker 必须手动配置 hub 地址（`--hub http://...`），没有 mDNS/注册中心自动发现。
4. **租约只有 TTL**：没有强一致的分布式锁，时钟偏移大时可能出现短暂双持有。
5. **事件流是有界内存/单文件追加**：没有内建长期归档与分页压缩，事件量大时需要外部日志系统。
6. **审批依赖人在环**：操作员长时间不在线时，高风险任务会停在等待审批状态（超时视为拒绝）。
7. **`attached-rw` 直连真实仓库**：Agent 的写入没有 git 分支隔离，误写会直接落到工作区，建议先提交或用独立 clone。
8. **端到端接口以上面路径为准**：若本仓库路由有调整，以 `src/` 中的实际注册为准，命令需同步更新。

---

## 附：一次完整的自改进循环（可复制）

```bash
# 0) 确认在项目 workspace
pwd   # /home/nihao/work/workspace/StackFlowOS

# 1) 建任务（操作员助手）
#    task.create{project_id:p-3, role:coder, title, description}

# 2) 拉起工人
#    agent.spawn{project_id:p-3, role:coder, task_id:t-xx}

# 3) 工人改文件 + 自检
python3 -m py_compile <changed>.py
python3 -m unittest discover -s tests -t .

# 4) 登记产物并把任务置 review
#    task.artifact{task_id:t-xx, path:<changed>}
#    task.update{task_id:t-xx, status:review}

# 5) 评审通过
#    task.update{task_id:t-xx, status:done}

# 6) 审计
curl -s 'http://127.0.0.1:17800/api/events?since=0&limit=100' | jq '.data.events[] | {seq, type}'
```
