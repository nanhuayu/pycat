# Memory 架构

Memory 是 Agent 长期可用性的关键能力，但必须避免“自动记一切”。PyCat 的记忆系统应以可解释、可控、可删除、可预算为原则。

当前代码主要落点：`models/state.py`、`core/state/services/memory_service.py`、`core/tools/system/state_mgr.py`、`core/tools/system/document_tools.py`、`core/prompts/context_assembler.py`、`core/prompts/system_builder.py`、`services/workspace_session_service.py`。

## 1. 当前现状

### 1.1 Session memory

`SessionState` 当前包含：

- `summary`
- `tasks`
- `memory: dict[str, str]`
- `documents: dict[str, SessionDocument]`
- `archived_summaries`
- `last_updated_seq`

其中 memory 以两种形式存在：

- 结构化 key-value：`SessionState.memory`
- 长文档：`SessionState.documents["memory"]`

### 1.2 Workspace memory

`MemoryService.load_workspace_memory()` 当前从：

```text
<work_dir>/.pycat/memory/
```

读取 Markdown/Text 记忆。支持入口文件：

```text
<work_dir>/.pycat/memory/MEMORY.md
```

若存在 `MEMORY.md`，会读取其中链接的 topic 文件；否则读取目录下最新的 Markdown/Text 文件。

### 1.3 Prompt 注入

`MemoryService.build_prompt_section()` 会按查询相关性选择片段，并生成：

```text
<relevant_memory sources="session, workspace">
- [source] key: value
</relevant_memory>
```

`core/prompts/context_assembler.py` 会根据 `conversation.settings["memory_sources"]` 选择来源。

当前支持的 sources：

- `session`
- `workspace`
- `global`

## 1.4 Global memory

`MemoryService.load_global_memory()` 从全局用户目录读取：

```text
~/.PyCat/memory/
```

入口文件为 `SOUL.md`（向用户说明这是 PyCat 全局 Memory 的说明文件）。若存在 `SOUL.md`，会读取其中链接的 topic 文件；否则读取目录下最新的 Markdown/Text 文件。

全局 Memory 文件命名规范：

```text
memory__<topic>.md
```

例如 `memory__user.md`、`memory__feedback.md`、`memory__reference.md`。

全局 Memory 默认**不自动注入**，需在对话设置中手动启用。写入通过 `manage_state` 的 `memory_tier="global"` 完成。

## 2. 记忆分层

| 层级 | 作用 | 存储位置 | 生命周期 | 默认注入 |
| --- | --- | --- | --- | --- |
| Session Memory | 当前会话事实、计划、临时上下文 | `Conversation.state` | 当前会话 | 是，按预算 |
| Workspace Memory | 当前工作区长期事实 | `<work_dir>/.pycat/memory/` | 工作区 | 是，按来源设置 |
| Global Memory | 跨项目偏好、用户画像、通用知识 | `~/.PyCat/memory/` | 跨工作区 | 否，需手动启用 |
| Repo Memory | 仓库级规范、命令、约定 | 未来 repo store | 仓库 | 长期可选，需确认 |

## 3. 写入原则

Memory 写入必须满足：

- **有用**：未来任务可能再次用到。
- **稳定**：不是临时状态或很快过期的信息。
- **可解释**：知道为什么写入、来源是什么。
- **可删除**：用户能查看和删除。
- **有权限**：敏感信息、个人信息、token、密钥不能自动写入。

不应写入：

- API key、cookie、token、密码。
- 用户明确只在当前消息使用的临时信息。
- 未验证的猜测。
- 大段工具输出原文。
- 与未来任务无关的对话闲聊。

## 4. Memory 检查能力

用户要求后续增加专门负责 memory 管理的能力。近期建议先做成“只给建议”的受控子任务，而不是直接写入器。

### 4.1 职责

Memory 检查能力负责：

- 从对话、工具结果、文档中提取候选记忆。
- 判断记忆层级：session/workspace/repo/user。
- 生成写入、更新、删除或忽略建议。
- 给出 reason、source、confidence。
- 检查敏感信息风险。

### 4.2 不允许行为

Memory 检查能力不应：

- 直接修改 `SessionState`。
- 直接写入文件。
- 直接读取超出主 Agent 授权范围的文件。
- 直接把 thinking 原文存为 memory。

### 4.3 输出内容

建议输出普通结构化建议即可，包含：

- action：create / update / delete / ignore。
- tier：session / workspace / repo / user。
- key / value。
- reason。
- source refs。
- confidence。
- sensitivity。

主 Agent 收到建议后，再决定：

- 调用 `manage_state`（`memory_tier="session"`）写 session memory。
- 调用 `manage_state`（`memory_tier="workspace"`）写 workspace memory 文件。
- 调用 `manage_state`（`memory_tier="global"`）写 global memory 文件。
- 询问用户确认。
- 忽略建议。

## 5. 记忆选择与上下文预算

记忆不应无条件注入 prompt。近期先用轻量规则控制：

- enabled_sources。
- max_snippets。
- max_chars。
- min_score。
- source_priority。
- allow_sensitive。

选择步骤：

1. 获取当前用户请求。
2. 分词/关键词匹配，后续可选 embedding。
3. 按 source、score、freshness 排序。
4. 按 memory 预算裁剪。
5. 注入 `<relevant_memory>`，保留来源标签。

当前 `MemoryService` 已有确定性 token/关键词计分，短期不急于引入 embedding。

## 6. 记忆与 SessionDocument

`SessionDocument` 适合保存长内容：

- `plan`
- `memory`
- `notes`
- `report`
- `reference`

设计建议：

- key-value memory 保存短事实。
- `memory` document 保存当前会话重要背景。
- workspace memory 保存跨会话稳定知识。
- 大文件解析结果保存为 artifact path，不直接塞入 key-value。

## 7. 记忆与工具结果

工具结果可以成为 memory 来源，但不能自动全部写入：

- 文件路径、验证命令、架构决策、稳定约定可以写。
- 临时日志、完整命令输出、错误堆栈全文不写。
- 长工具结果应写入 `.pychat/tool-results/`，memory 只保存摘要和路径。

## 8. 记忆与 Channel

Channel 输入可能来自外部用户或群聊，默认不应写入长期 memory。

建议：

- 未信任 Channel：只使用 session memory。
- trusted Channel：可写 session memory，但写 workspace/user memory 需确认或策略允许。
- 群聊消息写 memory 时必须包含来源和上下文。
- Channel 默认不接收 memory 敏感内容展示。

## 9. UI 展示

UI 应提供：

- 当前会话 memory facts。
- 当前会话 documents。
- workspace memory 列表和入口文件。
- 每条 memory 的来源、更新时间、层级。
- 删除/编辑/禁用来源。
- 当前 prompt 注入了哪些 memory 的可解释视图。

当前 `StatsPanel` 已可展示部分会话状态，后续可扩展为 Memory Inspector。

## 10. 权限与审计

每次 memory 写入建议记录：

- source：user / agent / tool / channel / memory_check。
- reason。
- refs：消息 seq、文件路径、工具调用 id。
- tier。
- sensitivity。
- created_at / updated_at。

删除策略：

- 用户删除优先级最高。
- 敏感信息一经发现应支持快速删除。
- Memory 检查能力可建议删除过期记忆，但不直接删除。

## 11. 近期改造建议

1. 保持 session/workspace 两层为主，将 `memory_sources` 整理为更清晰配置。
2. 增加 memory 检查能力，只输出建议，不直接写入。
3. 为 workspace memory 增加写入入口，但默认需要确认。
4. UI 增加 Memory Inspector 和 prompt 注入解释。
5. memory 注入纳入模型上下文预算。
6. 禁止自动保存敏感内容，并增加简单 secret 扫描。
