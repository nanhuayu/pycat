# 能力与默认子 Agent 架构

## 1. 目标

PyCat 将提示词、模型、运行模式、工具权限和执行预算统一抽象为**能力（Capability）**。能力不是 wrapper，也不是隐藏路由器；每个启用的能力都是一个可自动配置的 child agent，并以独立 `capability__*` 工具暴露给主 Agent。

默认子 Agent 是内置委托入口，用于处理跨来源、多文件或运行时自定义的复杂任务。所有默认子 Agent 统一命名为 `subagent__{subagentname}`。

核心约束：

- **能力 = 配置化 child agent**：由 `CapabilityConfig` 提供系统提示词、模式、模型、工具组和轮次预算。
- **能力工具 = `capability__<id>`**：每个 enabled capability 默认注册为独立工具；不再有单一 `run_capability` 路由器。
- **默认子 Agent = `subagent__*`**：用于模型运行时委托，不再保留 `new_task` 或 `run_sub_agent` 入口。
- **路由清晰**：单文件或单段长文本用 `capability__summarize_text`；多文件、跨来源综合用 `subagent__read_analyze`。

## 2. 概念边界

| 概念 | 职责 | 不应包含 |
| --- | --- | --- |
| Provider | API 地址、密钥、接口类型、可用模型来源 | 模型角色、任务策略 |
| ModelProfile | 单个模型自身能力：视觉、工具调用、推理、上下文、输出上限 | 主/副/备用等会话角色 |
| Capability | 可复用任务能力：提示词、模型引用、运行模式、工具组、输入输出约束 | 服务商连接细节、手写工具逻辑 |
| Capability Tool | 将单个 Capability 暴露为 `capability__<id>` 工具并调度 child task | 聚合多个能力、替主 Agent 选择子 Agent |
| Default Subagent Tool | 内置委托入口：`subagent__read_analyze`、`subagent__search`、`subagent__custom` | 作为设置页实体、兼容旧任务入口 |
| Conversation | 会话级主模型、副模型、备用模型、模式等运行策略 | 服务商密钥、模型固有能力 |

### 2.1 能力工具 vs 子 Agent 工具

| 维度 | Capability 工具 (`capability__*`) | Subagent 工具 (`subagent__*`) |
|------|----------------------------------|------------------------------|
| 本质 | 配置化的专用子 agent | 通用委托入口 |
| 配置 | 系统提示词、模型、工具组预配置 | 运行时由父 agent 指定 |
| 使用场景 | 固定用途（翻译、总结、标题提取等） | 动态任务（分析、搜索、自定义） |
| 命名 | `capability__<id>` | `subagent__<name>` |
| 新增方式 | 添加 `CapabilityConfig` | 修改 `multi_agent.py` |
| 典型示例 | `capability__translate`、`capability__summarize_text` | `subagent__read_analyze`、`subagent__custom` |

路由优先级：

- 单个文件、单段长文本、单个工具结果文件 → `capability__summarize_text`
- 多文件、跨来源、需要对照证据的综合报告 → `subagent__read_analyze`
- 单主题研究简报 → `capability__research_brief`
- 多来源搜索与事实核查 → `subagent__search`
- 需要特殊工具组或执行策略的委托 → `subagent__custom`
- 标准翻译/润色 → `capability__translate`
- 上下文压缩/归档 → `capability__context_compress` / `capability__context_curator`

运行链路：

```text
CapabilityConfig
  -> capability__* tool
  -> ToolContext.state["_pending_subtask"]
  -> Task._run_subtask()

subagent__* tool
  -> ToolContext.state["_pending_subtask"]
  -> Task._run_subtask()
```

`ToolResultPipeline` 不参与摘要或分析决策，只负责 inline/spill 和原文可恢复。

## 3. Capability 数据结构

```json
{
  "id": "summarize_text",
  "name": "长文总结",
  "kind": "summarize_text",
  "enabled": true,
  "model_ref": "openai|gpt-4o-mini",
  "mode": "agent",
  "system_prompt": "你负责把单个文件、单段长文本或单个工具结果文件压缩为结构化摘要。",
  "tool_groups": ["read"],
  "input_schema": {},
  "output_schema": {},
  "options": {
    "outline_first": true,
    "max_turns": 8
  }
}
```

注册规则：

- `enabled=false` 的能力不注册为工具。
- `options.expose_as_tool=false` 可显式隐藏能力工具。
- 其他 enabled capability 默认注册为 `capability__<id>`。
- 能力执行时统一写入 `_pending_subtask`，由 `Task._run_subtask()` 创建独立 child conversation。

## 4. 内置能力

当前默认能力包括：

1. `prompt_optimize`：提示词优化。
2. `title_extract`：提取会话标题。
3. `translate`：翻译与润色。
4. `context_compress`：长对话压缩。
5. `summarize_text`：单文件、单段长文本或单个工具结果文件总结。
6. `image_generate`：图像生成提示词整理。
7. `research_brief`：研究简报。
8. `tool_result_analyzer`：读取保真落盘的长工具结果并生成结构化报告。
9. `context_curator`：上下文整理。
10. `researcher`：研究助手。

这些能力默认暴露为 `capability__prompt_optimize`、`capability__summarize_text`、`capability__tool_result_analyzer` 等稳定工具名。

## 5. 默认子 Agent

默认子 Agent 放在 `core/tools/system/multi_agent.py`，命名规则固定为 `subagent__{subagentname}`。

| 工具 | 场景 | 默认工具组 | 约束 |
| --- | --- | --- | --- |
| `subagent__read_analyze` | 多文件、跨来源、长文档综合分析 | `read`、`modes` | 只读；不能写文件、执行命令或访问网络 |
| `subagent__search` | 搜索、事实核查、外部信息综合 | `search`、`read`、`modes` | 可搜索和读取；不能修改工作区或执行命令 |
| `subagent__custom` | 运行时自定义委托任务 | 默认 `read`、`modes`，可由父 Agent 指定 | 父 Agent 必须显式收窄工具组和目标 |

路由优先级：

- 单个文件、单个长文本、单个工具结果文件：`capability__summarize_text`。
- 多个文件、多个来源、需要对照证据的综合报告：`subagent__read_analyze`。
- 搜索和事实核查：`subagent__search`。
- 需要特殊工具组或执行策略的委托：`subagent__custom`。

## 6. 设置页结构

设置页只需要管理能力：启用状态、名称、说明、模式、模型、工具组、最大轮次、系统提示词和 JSON 选项。

默认子 Agent 是内置运行时工具，不作为用户配置实体；设置页不再维护“子 Agent / 工具入口”分栏。工具入口由 `ToolManager` 根据能力和默认系统工具自动注册。

## 7. 运行时落点

- `core/capabilities/types.py`：`CapabilityConfig`、`CapabilitiesConfig`。
- `core/capabilities/defaults.py`：默认能力事实来源，包含 `summarize_text`。
- `core/capabilities/manager.py`：合并默认能力、用户能力、项目能力。
- `core/capabilities/exposure.py`：决定 enabled capability 是否暴露为工具。
- `core/tools/system/capability_tools.py`：生成 `capability__*` 工具，渲染子任务消息，并写入 `_pending_subtask`。
- `core/tools/system/multi_agent.py`：默认 `subagent__*` 工具和 `attempt_completion`、`switch_mode` 控制工具。
- `core/tools/manager.py`：注册系统工具、默认子 Agent、能力工具、搜索工具和 MCP 工具。
- `core/task/task.py`：读取 `_pending_subtask`，创建 child conversation，并根据 `tool_groups` 限制 child policy。

## 8. 长工具结果关系

长工具结果处理不属于 Capability 自动管线。`ToolResultPipeline` 只保真落盘；需要总结或分析时，Agent 必须显式调用：

- `capability__summarize_text`：总结单个长文本、单个文件或单个工具结果文件。
- `capability__tool_result_analyzer`：围绕父任务分析一个长工具结果文件。
- `subagent__read_analyze`：综合多个文件或多个来源。

推荐流程：

1. 工具返回超长结果。
2. `ToolResultPipeline` 保存完整原文并返回首尾预览和路径。
3. 主 Agent 按任务选择 `capability__summarize_text`、`capability__tool_result_analyzer` 或 `subagent__read_analyze`。
4. 子任务用只读工具读取原文，生成结构化报告返回父 Agent。

## 9. 维护规则

- 新能力优先添加为 `CapabilityConfig`，不要新增 wrapper 工具。
- 新默认子 Agent 必须使用 `subagent__{name}` 命名，并在 description 中写清使用场景和权限边界。
- 能力描述应短而可判别；完整 system prompt 只在子任务 prompt 中注入。
- 文档、prompt 和测试中不得继续引导模型使用 `new_task`、`run_sub_agent` 或旧 `capability__summarize_long_text`。
