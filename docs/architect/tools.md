# Tools 架构

工具系统是 Agent 执行能力的边界层。PyCat 同时支持内置工具和 MCP 工具。这里的 MCP 指 **Model Context Protocol**，是外部工具/资源服务与模型客户端之间的协议扩展方式。

当前代码主要落点：`core/tools/base.py`、`core/tools/registry.py`、`core/tools/manager.py`、`core/tools/permissions.py`、`core/tools/system/`、`core/tools/mcp/`。

## 1. 核心目标

工具系统需要同时满足：

- **统一 schema**：所有工具暴露 OpenAI-compatible function schema。
- **统一执行**：模型 tool call 通过 `ToolExecutor -> ToolManager -> ToolRegistry` 执行。
- **统一权限**：读、写、命令、MCP、浏览器、子任务等都必须受权限控制。
- **统一状态同步**：工具可通过 `ToolContext.state` 更新会话状态，执行后同步回 `Conversation`。
- **统一结果管理**：工具结果可被模型继续使用；过长结果要截断或落盘。
- **可扩展**：内置工具、MCP 代理、技能资源和后续子任务能力都通过注册进入。

## 2. 当前执行链路

```text
LLM tool_calls
	-> Task._execute_tool_calls()
	-> ToolExecutor.parse_tool_call()
	-> ToolExecutor.is_tool_allowed()
	-> ToolExecutor.build_tool_context()
	-> ToolManager.execute_tool_with_context()
	-> ToolRegistry.execute()
	-> ToolPermissionResolver.wrap_context()
	-> BaseTool.execute()
	-> ToolResult
	-> state sync / tool message
```

## 3. 工具接口

### 3.1 `BaseTool`

所有工具继承 `BaseTool`，必须提供：

- `name`
- `description`
- `input_schema`
- `execute(arguments, context)`

并可声明：

- `group`：用于模式工具组过滤，例如 `read`、`edit`、`command`、`mcp`、`search`、`browser`、`modes`。
- `category`：用于权限策略，例如 `read`、`edit`、`command`、`misc`。
- `max_output_chars`：工具输出截断上限。

### 3.2 `ToolContext`

工具执行上下文包含：

- `work_dir`
- `approval_callback`
- `questions_callback`
- `state`
- `llm_client`
- `conversation`
- `provider`

路径类工具应通过 `ToolContext.resolve_path()` 限制访问范围，避免越过工作区。

### 3.3 `ToolResult`

工具返回 `ToolResult`：

- `content`：文本或 MCP-style content blocks。
- `is_error`：是否为错误结果。

`ToolRegistry.execute()` 会对字符串输出调用 `truncate_output()`。随后 `Task._build_tool_message()` 把原始工具文本交给 `ToolResultPipeline`。流水线只做保真预算控制：短结果原样进入 tool message；超长结果写入 `.pycat/tool-results/`，并在 tool message 中保留首尾预览、字符数、完整文件路径和 `read_file` 提示。

## 4. 工具注册

`ToolManager._register_default_system_tools()` 当前注册：

- 文件读取/搜索：`ls`、`read_file`、`grep`
- Python 执行：`python_exec`
- 文件写入/编辑/删除：`write_file`、`edit_file`、`delete_file`
- Shell：`execute_command`、`shell_start`、`shell_status`、`shell_logs`、`shell_wait`、`shell_kill`
- Patch：`apply_patch`
- 状态：`manage_state`
- 文档：`manage_document`
- 交互：`ask_questions`
- 技能：`skill__load`、`skill__read_resource`
- 默认子 Agent/模式：`subagent__read_analyze`、`subagent__search`、`subagent__custom`、`attempt_completion`、`switch_mode`
- 能力工具：按配置动态注册多个 `capability__*` 工具，例如 `capability__translate`、`capability__summarize_text`
- 搜索：按配置动态注册 `builtin_web_search`
- MCP：按启用的 MCP Server 动态注册代理工具

## 5. 权限系统

### 5.1 当前权限

`ToolPermissionPolicy` 当前包含：

- `auto_approve_read`
- `auto_approve_edit`
- `auto_approve_command`

`ToolPermissionResolver` 会包装工具的 approval callback：

- 若该工具 `category` 对应的 auto approve 为 true，则直接允许。
- 否则调用原始 approval callback。
- 无 callback 时拒绝。

`RunPolicy` 还会通过以下字段限制工具：

- `enable_search`
- `enable_mcp`
- `tool_allowlist`
- `tool_denylist`

### 5.2 近期权限目标

近期只增加最小 source 维度，避免复杂策略 DSL：

| source | 默认建议 |
| --- | --- |
| `desktop` | 按现有 auto approve 和确认弹窗执行 |
| `channel` | 默认允许只读展示类能力，拒绝 write/command/MCP，trusted channel 可单独放开 |
| `sub_task` | 默认无 edit/command/MCP/browser，只能读主 Agent 给出的上下文 |
| `system` | 内部维护动作，必须有明确调用点和审计日志 |

风险分类保持简单：

- `read`：读取本地上下文或安全状态。
- `edit`：修改文件、状态或文档。
- `command`：执行 shell、安装依赖、启动进程。
- `mcp`：外部服务能力，默认按中高风险处理。
- `network/browser`：访问外部网络或浏览器自动化。

## 6. MCP 工具

PyCat 当前通过 `ToolManager` 管理 MCP：

- 从 `StorageService.load_mcp_servers()` 加载 MCP Server 配置。
- 用 `mcp.client.stdio` 建立持久会话。
- 将 MCP Server 暴露的工具包装为 `McpProxyTool`。
- 使用 public prefix 避免工具名冲突。
- 根据 `enable_mcp` 决定是否注入工具 schema。

设计约束：

- MCP 是外部能力，默认比内置 read 工具更严格。
- MCP schema 可以缓存，但执行仍要实时检查权限。
- MCP 输出同样要截断、审计和落盘。
- 频道和子任务默认不应自动拥有 MCP 权限。

## 7. 状态工具

### 7.1 `manage_state`

`StateMgrTool` 负责：

- 更新 summary。
- 创建/更新/删除 Todo。
- 更新 memory key-value（支持 tier 选择）。
- 触发 archive context。

memory 写入支持 `memory_tier` 参数：
- `session`（默认）：写入 `SessionState.memory`，由 `ToolExecutor.sync_state()` 写回 `Conversation`。
- `workspace`：写入 `<work_dir>/.pycat/memory/memory__<key>.md`。
- `global`：写入 `~/.PyCat/memory/memory__<key>.md`。

### 7.2 `manage_document`

`ManageDocumentTool` 负责维护 `SessionState.documents`：

- `plan`
- `memory`
- `notes`
- `report`
- 其他命名文档

文档会在 `system_builder` 中按规则注入 prompt。近期应优先注入摘要和关键片段，长文档按需读取。

## 8. 能力、子 Agent 与模式工具

能力和默认子 Agent 不再各自维护重复工具实现：

- `CapabilityConfig` 是可复用任务定义，包含提示词、默认模型、运行模式、工具组和选项。
- 每个 enabled Capability 默认注册为稳定的独立 `capability__<id>` 工具；`options.expose_as_tool=false` 可显式隐藏。
- 不再存在独立的配置型 `SubAgentConfig` 实体，也不再导出该别名。
- 默认子 Agent 统一命名为 `subagent__{subagentname}`，当前包含 `subagent__read_analyze`、`subagent__search`、`subagent__custom`。

`core/tools/system/capability_tools.py` 是能力工具适配层，负责加载能力配置、生成 `capability__*` 工具、渲染子任务消息、标准化子任务工具组并写入 `ToolContext.state["_pending_subtask"]`。`core/tools/system/multi_agent.py` 复用这些 helper，提供固定语义的默认子 Agent。

旧摘要工具和旧子任务入口不保留兼容壳。单文件/单长文本总结通过 `capability__summarize_text` 调度；多文件或跨来源综合通过 `subagent__read_analyze` 调度。

## 9. 子任务工具

当前 `capability__*` 和 `subagent__*` 都会让 `Task` 创建子 `Task` 并运行。能力是配置化 child agent，默认子 Agent 是内置委托入口。

近期处理：

1. 不保留 `new_task` 或 `run_sub_agent` 兼容入口。
2. 子任务默认禁用 `edit`、`command`、`mcp`、`browser`，除非父 Agent 通过 `subagent__custom` 显式放开。
3. 子任务只拿到主 Agent 裁剪后的上下文。
4. 子任务输出回到主 Agent，由主 Agent 决定是否执行真实工具或写入状态。
5. 后续新增默认子 Agent 时，命名必须使用 `subagent__{name}` 并说明固定工具链。

首批稳定入口：`capability__summarize_text`、`capability__tool_result_analyzer`、`subagent__read_analyze`、`subagent__search`、`subagent__custom`。

## 10. 工具与命令的关系

工具调用和显式命令不是同一层：

- `!command` 是用户显式命令，由 `CommandService` 执行。
- `execute_command` 是模型工具调用，由 `ToolRegistry` 执行。

两者应共享风险分类和审计字段，但保留不同来源：

```text
source = explicit_command | agent_tool | channel_command | sub_task
```

## 11. 工具输出与上下文预算

工具输出是上下文膨胀的主要来源。当前由 `ToolResultPipeline` 统一处理 tool message 之前的预算策略：

- 读取类工具优先在工具层分页，例如 `read_file` 默认最多返回 2000 行并提示下一段读取参数。
- 长命令输出、长 MCP 输出、长搜索输出保存到 `.pycat/tool-results/`。
- prompt 中只保留首尾预览、路径和恢复提示，不自动清洗、不自动摘要。
- Agent 按需用 `read_file` 读取完整文件。
- 如需分析或总结，Agent 显式调用 `capability__summarize_text` 或 `capability__tool_result_analyzer`；多文件或跨来源综合使用 `subagent__read_analyze`。
- memory、history、document、工具结果分别有预算。
- 频道回复不直接外发超长工具原文。

## 12. 扩展规范

新增工具时必须说明：

- 工具名是否稳定。
- `group` 和 `category`。
- 输入 schema 是否限制明确。
- 是否可能写文件、执行命令、访问网络或泄露隐私。
- 输出最大长度、分页能力和是否应由 `ToolResultPipeline` spill。
- 是否可被频道、技能、子任务调用。
- 失败时是否返回可恢复错误。

## 13. 近期改造建议

1. 扩展 `ToolPermissionPolicy`，增加 source 维度。
2. 持续限制 `capability__*` 与 `subagent__*` 子任务默认工具权限。
3. 命令权限和工具权限共享 read/edit/command/MCP 等风险分类。
4. 对 MCP 工具增加 per-server/per-tool 权限开关。
5. 工具结果预算持续接入模型上下文裁剪，但保持原文可恢复。
6. 工具执行审计记录 source、category、risk、duration、artifact path。
