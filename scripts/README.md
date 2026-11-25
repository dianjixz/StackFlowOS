# scripts

## test-report.sh — pytest 结果与跳过项汇总

一条命令（在仓库根目录执行）：

```bash
scripts/test-report.sh
```

并行开发时只跑相关子集，避免抢端口/state 目录：

```bash
scripts/test-report.sh tests/test_address.py
scripts/test-report.sh tests/test_sec_sandbox.py -k landlock
```

脚本做的事：

1. 用真实 pytest 跑测试（不会把 skipped 当 passed，也不会伪造结果）；
2. 把 JUnit XML 写到 `.stackflowos/test-report/junit.xml`、完整输出写到
   `.stackflowos/test-report/pytest.log`（`.stackflowos/` 已在 `.gitignore`）；
3. 打印 `passed / failed / errors / skipped / total` 计数；
4. 按原因分组列出 skipped（例如 Landlock 不可用、pyzmq 未安装、IPv6 不可用、
   `SFO_TEST_MDNS` 未开启、openssl CLI 缺失、IPC socket 路径过长），每条给出
   一个示例用例；
5. 列出每个 failed/errored 用例及其消息首行。

退出码就是 pytest 的退出码，可直接用于 CI 判定。

解释器顺序（测试要 import `pynng`，请用已装好项目依赖的解释器）：

1. `SFO_PYTHON`（显式指定）
2. `/tmp/sfo-venv/bin/python`（本机既定环境，见 `docs/team-plan.md`）
3. `python3`

```bash
SFO_PYTHON=/path/to/venv/bin/python scripts/test-report.sh
SFO_TEST_REPORT_DIR=/tmp/sfo-report scripts/test-report.sh   # 报告输出目录
```

> 当前实际可用的解释器是 `/tmp/sfo-venv/bin/python`（pytest 9.2；该 venv 没有
> `pytest-subtests`，但 pytest 本身会执行并汇总 `subTest` 断言）。dev extra 里仍
> 把 `pytest-subtests` 作为推荐测试插件声明，装不装都不影响本机运行。

### 校验 dev extras 声明（不安装任何包）

一条命令解析 `pyproject.toml` 并打印声明：

```bash
python -c "import tomllib, pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['optional-dependencies']['dev'])"
```

预期输出 `['pytest>=8.0', 'pytest-subtests>=0.12']`。
`tests/test_test_infra.py::DevExtrasTests` 还会把 pytest、pytest-subtests、
`testpaths` 与 `-rs` 钉成回归测试，防止 dev 声明再次退化为空数组。

有 `uv` 且能访问包索引时，可以额外做一次**只解析不安装**的检查：

```bash
uv pip install --dry-run --python /tmp/sfo-venv/bin/python -e ".[dev]"
```

它会列出将安装的 `pytest`、`pytest-subtests`、`pynng` 等（`--dry-run` 不修改环境）。

## 测试的临时目录与 UNIX socket 路径

Linux 的 `sockaddr_un.sun_path` 约 108 字节，而 StackFlowOS 把 IPC endpoint
上限设为 100 字节（`stackflowos.transport.address.MAX_IPC_PATH_BYTES`）。
测试 scratch 目录不要建在 checkout 里：默认用 `SFO_TEST_TMPDIR` 或
`<系统临时目录>/sfo-tests`，helper 在 `tests/conftest.py`：

- `make_temp_dir(prefix)` / `git_scratch_dir(prefix)`：短路径 scratch 工厂，
  git 测试用它保证“独立 repo 根、不在 checkout 内”；
- `require_ipc_path(self, path)` / `require_ipc_dir(self, dir)`：超过 100 字节时
  以清晰 reason `skipTest`（提示用更短的 `SFO_TEST_TMPDIR`/`--basetemp`），
  而不是让 `bind()` 抛 `OSError: AF_UNIX path too long`。
