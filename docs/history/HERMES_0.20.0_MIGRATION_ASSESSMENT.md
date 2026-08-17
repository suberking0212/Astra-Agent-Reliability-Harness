# Hermes 0.20.0 最小迁移评估（阻塞记录）

> **Historical notice:** This preserves the original version investigation and
> its point-in-time evidence. For the current migration status, see
> [docs/STATUS.md](../STATUS.md). It is not current implementation authority.

评估日期：2026-08-07  
隔离分支：`codex/hermes-0.20.0-migration`

## 结论

推荐 **B：暂留 0.18.2，等待可验证的上游发布物。**

截至评估时，`NousResearch/hermes-agent` 没有 `0.20.0` 或
`v0.20.0` Git tag、GitHub Release 或 PyPI 发布物。不能用浮动的
`main` 充当该版本，也不能凭名称编造 commit。因此没有安全、可复现的
0.20.0 来源可以 vendor，最小迁移不能开始。

这不是由 Astra Plugin、Business Sandbox 或 Tool Gateway 导致的兼容性
问题，而是目标版本的固定来源不可取得。没有依赖 Hermes 私有实现、
monkey-patch、第二套 CLI，且未实现任何 Learning Layer、Execution API
或 Compatibility Layer。

## 来源身份核查

| 项目 | 结果 | 复核方式 |
| --- | --- | --- |
| 请求的版本 | `0.20.0` | 用户请求 |
| 可用 Git tag | 不存在 | `git ls-remote --tags https://github.com/NousResearch/hermes-agent.git` 中筛选 `0.20.0`；`0.20.0` 和 `v0.20.0` 均无结果 |
| 可用 GitHub Release | 不存在 | `GET /repos/NousResearch/hermes-agent/releases/tags/v0.20.0` 返回 404 |
| 可用 PyPI release | 不存在 | `https://pypi.org/pypi/hermes-agent/json`：最高版本为 `0.19.0`，release keys 不含 `0.20.0` |
| 可下载固定 tarball | 不存在 | 跟随 `https://api.github.com/repos/NousResearch/hermes-agent/tarball/{0.20.0,v0.20.0}` 重定向后均为 404 |
| 0.20.0 revision | 无 | 上述版本不存在，故无可记录的 commit SHA |
| 用户给出的 `48f0587` | 无法作为上游身份核验 | `GET /repos/NousResearch/hermes-agent/commits/48f0587` 返回 422；`git ls-remote` 亦无匹配 ref |

现有 0.18.2 记录仍保持原样：

- 源目录：`hermes-agent-main/`；其 `pyproject.toml` 声明 `0.18.2`。
- 归档：`artifacts/hermes-source-snapshot-0.18.2.tar.gz`。
- manifest：`artifacts/hermes-source-manifest.json`，其中 `commit_sha` 已是
  `null`；它不是已验证的 `48f0587` 来源。

没有创建 0.20.0 source manifest 或 snapshot；创建它们会把不存在的
发布身份写成事实。现有 0.18.2 manifest 和归档未修改。

## 与 Astra 直接相关的差异

没有可固定的 0.20.0 源树，因此不能做诚实的 0.18.2 → 0.20.0 文件/API
差异，也不能判断 0.20.0 是否新增公开的 session/task correlation、
interaction、resume 或 artifact-observation 接口。该项状态为
**上游发布物阻塞，未验证**，不是“不存在”的结论。

当前 0.18.2 的已观察公开边界是项目插件的 `PluginContext`：
`register_tool(...)` 与 `register_hook(...)`。Astra adapter 只调用
`register_tool(...)`，不导入 Hermes 内部模块。

## 门禁结果

| 最小迁移门禁 | 0.20.0 结果 | 证据/含义 |
| --- | --- | --- |
| 固定 tag、revision、snapshot、manifest | 阻塞 | 目标版本无 tag/release/package，不能创建可追溯 vendor 快照。 |
| Hermes 原生 CLI 启动 | 未验证 | 没有 0.20.0 可执行文件。0.18.2 的原生 CLI `--version` 成功。 |
| 真实外部 Provider 对话、流式输出、Tool Call | 未验证 | 环境无 Provider 凭据，且没有 0.20.0。不得把本地 fixture 误报成真实模型 Provider。 |
| project plugin discovery | 未验证 | 没有 0.20.0。0.18.2 黑盒测试通过，见下表。 |
| `ctx.register_tool(...)` / `ctx.register_hook(...)` 兼容 | 未验证 | 没有 0.20.0 源码或文档可检验。 |
| `astra-runtime` discovery / registration / invocation | 未验证 | 没有 0.20.0。0.18.2 黑盒测试通过。 |
| `astra_runtime_status` 黑盒 Tool Invocation | 未验证 | 没有 0.20.0。0.18.2 黑盒测试通过。 |
| Business Sandbox、ProductionRuntime、Governance、Tool Gateway 初始化 | 未验证 | 0.20.0 plugin 注册尚不能运行。0.18.2 测试路径已实际构造 Runtime。 |
| Astra 全量测试 | 未验证 | 0.20.0 不存在。当前基线为 `125 passed`。 |
| compileall、静态检查、diff check | 失败/未验证 | `compileall` 和 `git diff --check` 对当前工作树通过；Ruff 对既有 Astra 代码报 3 项错误，见“已知基线问题”。没有 0.20.0 diff 可检查。 |
| 无私有 API、monkey-patch、第二套 CLI | 仅基线通过 | Astra plugin 仍仅使用公开 PluginContext；本次没有新增实现。0.20.0 尚不能验证。 |
| 主线无明显退化 | 未验证 | 没有目标版本可以比较。 |

因此不满足“所有门禁均通过”这一 baseline 切换条件。

## 已执行的基线证据（不构成 0.20.0 通过）

| 检查 | 结果 | 实际证明的内容 | 未证明的内容 |
| --- | --- | --- | --- |
| `python -m pytest -q` | `125 passed in 4.56s` | 当前 Astra 主线的测试集在现有环境可运行。 | 不能证明任何 Hermes 0.20.0 行为。 |
| `python -m compileall -q astra business_sandbox tests` | 通过 | Astra 与 Business Sandbox Python 源可编译。 | 不是类型检查，也不覆盖 0.20.0。 |
| `tests/test_hermes_plugin_invocation.py` | `1 passed in 3.19s` | 真实 Hermes 0.18.2 CLI 子进程经 project plugin discovery 加载 `astra-runtime`；本地 OpenAI-compatible HTTP fixture 使用 SSE 流，模型响应触发五次 `astra_runtime_status` 调用，并校验所有工具结果。注册过程中实际初始化了 ProductionRuntime、Governance、Tool Gateway 和 Business Sandbox 配置。 | fixture 不是外部真实模型 Provider；未证明 0.20.0。未调用 `register_hook(...)`。 |
| `scripts/start_astra_cli.sh` 加 `HERMES_BIN=.../.venv/bin/hermes --version` | 通过 | 脚本初始化本地 Business Sandbox 后 `exec` 到 Hermes 原生 CLI；输出为 Hermes 0.18.2 的版本信息。 | 未证明 0.20.0 CLI，也未进行模型对话。 |

## 已知基线问题（不属于本次迁移）

执行 `ruff check astra business_sandbox tests scripts` 报告三项已有问题：

1. `astra/runtime.py:6134`：未定义 `ExecutionEventSink`（F821）。
2. `astra/tool_catalog.py:100`：单行复合语句（E701）。
3. `tests/test_phase4_persistent_runtime.py:30`：未使用导入（F401）。

这些问题没有在本次隔离评估中修改，以免把无关重构混入 Hermes migration。

## 文件变更

- 新增：本报告。
- 修改：无。
- 删除：无。
- 保留不变：0.18.2 源目录、manifest、归档与回退路径。

## 继续条件

只有上游提供以下任一可验证的 0.20.0 release identity 后，才重新开启本分支上的迁移：

1. `v0.20.0`（或明确等价）的 immutable Git tag 及其完整 commit SHA；或
2. 官方 PyPI `hermes-agent==0.20.0` release，带可核验文件哈希；或
3. 上游维护者明确声明的 commit SHA 与不可变 release artifact。

届时应将该 artifact 放入新的独立 vendor 目录，生成独立的
`hermes-source-manifest-0.20.0.json` 与
`hermes-source-snapshot-0.20.0.tar.gz`，再依次运行每一项门禁。若某项
只能通过 Hermes 私有模块、文件扫描、ContextVar、sidecar 或 monkey-patch
解决，应立即停止该项并归类为“需要私有实现”，不得绕过。
