# front — Web 控制台（Astryx）交付说明

> 角色：前端 owner（`webui/**`、`stackflowos/webassets/**` 构建产物、`tests/test_front_*.py`、
> `tests/test_web_assets.py`、本文件）。工作目录 `/home/nihao/work/workspace/StackFlowOS`。

## 0. 结论

Web 控制台已从"原生 JS/CSS、无构建步骤"整体重写为 **React 19 + Astryx 设计系统 + Vite**：

- 源码在 `webui/`；`./scripts/build_webui.sh` 做类型检查并构建到 `stackflowos/webassets`；
  Python hub 只托管构建产物，**运行时不需要 Node**。
- 8 个屏幕：对话（含"现场"面板）/ 项目 / Agent（含单 Agent 对话）/ 单元 / 审批 / 事件 /
  自治 / 设置。明暗主题、中英双语、窄屏单列、深链（`#/projects/<id>`、`#agent/<id>`）齐备。
- 旧 `webassets/{js,css}` 已删除；门禁测试改为检查"构建产物 + 源码契约"，见
  `tests/test_web_assets.py`、`tests/test_front_{i18n,units,session_llm,autonomy}.py`。

## 1. 目录与所有权

```
webui/
  index.html main.tsx app.css     入口与全局样式（只放布局，颜色/间距用 token）
  api/client.ts                   唯一 HTTP 边界：ApiError、request、api.*
  state/store.ts                  /api/state 快照 + SSE reducer + useStore(selector)
  session/index.ts                身份、会话列表、viewer 写门禁、401 门
  i18n/                           dict.json（旧字典）+ extra.ts（壳文案）+ keys/<screen>.ts
  shell/                          router（hash）、manifest（导航注册表）、theme、BrandMark
  ui/                             Page 页框、Placeholder、format 时间/状态/事件摘要、toast 桥
  screens/<app>/                  每屏一个目录；默认导出 ScreenProps 组件
tests/
  _frontend.py                    解析 TS 源码的测试辅助（字典键、屏幕标记）
  test_web_assets.py              构建产物/安全/API 契约/设计系统门禁
  test_front_i18n.py              字典完整性、t() 键可解析、动态模板键
  test_front_units.py             单元/事件屏能力面
  test_front_session_llm.py       会话门禁、LLM 用量、能力目录、流式 reducer
  test_front_autonomy.py          审批队列/规则/自治策略/急停
```

约定：一个屏幕只改自己的 `screens/<app>/` 与 `i18n/keys/<app>.ts`；共享层（App/路由/
store/client/ui）由 lead 维护。布局用 Astryx 组件，不写裸 `div`；颜色/圆角/间距用
`var(--color-*)/var(--radius-*)/var(--spacing-*)`，不写死 hex/px（结构性列宽除外）。

## 2. 构建与本地开发

```bash
cd webui && npm install
npm run dev                 # :5180，/api 代理到 scripts/ui_mock.py(:17877)
npx tsc --noEmit -p tsconfig.json
node scripts/shot.mjs URL OUT.png 1440 900 2500 [theme=dark] [lang=en]
../scripts/build_webui.sh   # 类型检查 + 构建到 stackflowos/webassets
```

fixture：`python3 scripts/ui_mock.py`（内存 API + SSE ticker，覆盖会话/LLM/能力/MCP/
自治/单元详情/项目健康与 Git/单 Agent 对话），仅用于开发与截图，产品不依赖它。

## 3. 设计系统接入要点

- `astryx-themes.ts` 由 `npx astryx theme add neutral` 生成（neutral 0.7.17），
  `shell/theme.tsx` 控制 `light|dark|system` 并持久化到 localStorage。
- 组件用法以 `npx astryx component <Name>` / `npx astryx docs layout` 为准；页面骨架是
  `AppShell + TopNav + SideNav`，内容区用 `Layout/LayoutHeader/LayoutContent/LayoutPanel`。
- 组件尺寸/定位依赖内联 CSS 变量，运行时还注入一段 position-try 样式表，因此 CSP 的
  `style-src` 放宽为 `'self' 'unsafe-inline'`（`script-src` 仍 `'self'`），见
  `docs/security-fix-plan.md` §14.2。
- Astryx 0.7.17 的 `TabList` 在未提供 panelId 时渲染为按钮而非 `role=tab`；
  功能与深链不受影响，记录为已知差异。

## 4. 验证

```bash
/tmp/sfo-venv/bin/python -m pytest -q \
  tests/test_web_assets.py tests/test_front_i18n.py \
  tests/test_front_units.py tests/test_front_session_llm.py \
  tests/test_front_autonomy.py
```

另外用真实浏览器（Playwright + 系统 Chrome）对每个屏幕截图检查：
桌面 1440×900、窄屏 420×900、明/暗、中/英、关键交互（发送消息、开抽屉、筛选、
暂停事件流、打开设置各分区、viewer 只读态）。截图位于 `docs/img/`。

## 5. 已知边界

- 构建产物提交进仓库（`stackflowos/webassets`），换的是"改 UI 需要 Node"，不是"运行需要 Node"。
- fixture 只是开发数据：单 Agent 对话的 POST 只追加用户消息，不产生真实增量；
  流式/工具卡片按真实 SSE 契约实现，最终验证以真实 hub 为准。
- 事件屏清屏只影响本地视图（`clearedSeq`），服务端历史不变。
- 控制台是单页应用，未知路径由 server 回退到 `index.html`；404 只对带扩展名的路径生效。
