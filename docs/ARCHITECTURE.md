# PyCat 架构与代码组织

## 当前架构重点

这次架构优化的重点不是替换 UI 技术栈，而是把 PyCat 自身的运行时组织方式收口为清晰边界：

- `bootstrap / coordinator`
  - 负责应用启动装配、状态初始化与会话级投影
- `TurnEngine`
  - 负责统一执行主循环边界与运行时事件输出
- `AppState + SessionState + runtime state`
  - 负责把会话级能力状态从 UI 私有字段中收口出来
- `Store`
  - 负责最小应用状态容器与观察式投影
- `tool / capability contract`
  - 负责统一内置工具、MCP、模式能力与权限边界
- `workspace / session memory`
  - 负责记忆索引、片段选择、裁剪与 prompt 注入
- `channel notice / policy`
  - 负责把外部通道视为会话级能力，而不只是适配器配置项

本次 Phase 1 已先把这条迁移主线落到 4 个运行时基础件上：

- `core/llm/llm_config.py`
- `core/prompts/assembler.py`
- `core/runtime/turn_policy.py`
- `core/runtime/events.py`

并进一步补上了第一层执行包装：

- `core/runtime/turn_engine.py`

同时，最近一轮继续强化了 3 条系统级主线：

- **统一 turn lifecycle**
  - PyCat 现状：`TurnEngine + MessageRuntime + StreamingMessagePresenter`
  - 当前落地点：runtime event 已作为一等执行通道进入 UI，而不是只靠 token/最终消息反推状态
- **分层 memory system**
  - PyCat 现状：`MemoryService + build_context_messages()` 已可从工作区 `.pycat/memory/` 读取 Markdown 记忆并注入 `<relevant_memory>`
  - 当前进展：已经支持会话级 `memory_sources`（session/workspace）选择，prompt 只注入当前会话允许的来源
  - 当前差距：还没有完整的 user/repo tier 目录规范
- **channel session state**
  - PyCat 现状：已有 `core/channel/` 协议、`ChannelQueue`、settings channels 页面、prompt 中的 `<active_channels>`、StatsPanel 的 channel 卡片
  - 当前进展：`ConversationSettingsDialog + ConversationSettingsUpdate + AppState` 已支持 allowlist / trust / notice 的会话级 policy
  - 当前差距：还缺真正的 adapter reply policy 与独立 channel timeline

## 当前分层

```
pycat/
├── main.py             # 应用入口
├── models/              # 纯数据模型（无外部依赖）
│   ├── conversation.py  # Conversation, Message (含 seq_id, state_snapshot)
│   ├── state.py         # SessionState, Task, TaskStatus (状态管理核心)
│   ├── provider.py      # Provider 模型
│   ├── mcp_server.py    # MCP 服务器配置
│   └── search_config.py # 搜索配置
│
├── utils/               # 真正通用的辅助函数（避免承载领域逻辑）
│
├── services/            # 本地持久化 + 外部能力（Provider/Search）
│   ├── storage_service.py   # 本地 JSON 持久化
│   ├── app_settings_service.py # App settings load/save/patch
│   ├── provider_catalog_service.py # Provider 目录持久化与列表操作
│   ├── provider_service.py  # Provider 管理
│   ├── search_service.py    # 网络搜索服务 (Tavily/Google/SearXNG)
│   └── importers/           # 导入格式解析
│       ├── parse.py         # 统一入口
│       └── *.py             # 各格式解析器
│
├── core/               # 可复用业务能力（LLM/Prompt/Modes/Tools/State）
│   ├── app/            # AppState / AppCoordinator / lightweight store
│   ├── llm/            # LLM 请求构建 + HTTP/流式解析
│   │   ├── client.py   # LLMClient（原 ChatService 的定位）
│   │   ├── llm_config.py      # Conversation 级别 LLM 配置事实来源
│   │   └── request_builder.py # 兼容层 + payload 构建
│   ├── prompts/        # system prompt 生成（Mode 驱动）
│   │   └── assembler.py       # Prompt/history/request 组装门面
│   ├── modes/          # 多模式系统（defaults + work_dir/modes.json）
│   ├── runtime/        # TurnPolicy / TurnEvent 等运行时契约
│   ├── tools/          # MCP/系统工具注册与执行（ToolRegistry/ToolManager）
│   ├── condense/       # 会话压缩/总结
│   ├── prompt_optimize/ # 提示词优化 prompts
│   ├── attachments.py  # 附件处理与图片编码
│   ├── context/        # 工作区/文件树等上下文构建
│   ├── commands/       # slash command -> runtime action
│   ├── task/           # 多步任务循环与子任务编排
│   └── state/          # 会话 state 相关服务
│
└── ui/                  # PyQt6 UI 层
    ├── main_window.py   # 主窗口
    ├── presenters/      # UI 事件桥接与会话/消息协调
    ├── runtime/         # Qt 线程/信号桥接运行时
    ├── widgets/         # 可复用组件
    ├── dialogs/         # 对话框
    ├── settings/        # 设置模块 (单文件)
    │   └── settings_dialog.py  # 统一设置对话框
    └── utils/           # Qt 相关工具
        ├── image_loader.py  # QPixmap 加载
        └── image_utils.py   # 剪贴板/拖放图片处理
```

## 依赖关系（重要）

```
models  ←─ utils
  ▲
  │
services ←─ core ←─ ui
```

说明：这是当前维护上的目标依赖方向。UI 负责展示与事件转发，core 负责命令/模式/prompt/任务循环，services 负责持久化与外部服务访问。

## 建议的简化架构（路线图）

目标：把“UI 事件 / 会话状态 / LLM 请求 / 模式&提示词 / 工具能力”分离成清晰的 4 层，减少 `ui/main_window.py` 里夹杂的业务逻辑。

### 1) Domain（纯数据）
- `models/conversation.py`
  - `Conversation`/`Message` 只管数据结构与序列化
  - `Conversation.mode` 作为“模式 slug”（例如 `chat`/`agent`/`debug`）

### 1.5) Services（面向 UI 的应用服务）
- `storage_service.py`
  - 负责底层 JSON 持久化
- `app_settings_service.py`
  - 负责 app settings 的 load / save / patch merge
- `provider_catalog_service.py`
  - 负责 provider catalog 的 load / save / upsert / remove / move / merge defaults
- `provider_service.py`
  - 负责 provider 连接测试、模型发现、配置校验
- `conversation_service.py`
  - 负责会话 CRUD、消息操作、对话级配置应用

### 2) Core（可复用业务能力）
- `core/app/`
  - `bootstrap.py`：启动装配加载（settings/providers/catalog）
  - `state.py`：轻量 `AppState`
  - `store.py`：极简 observable store
  - `coordinator.py`：应用级编排入口，消费纯 `ConversationSelection` / settings update，收口 provider/model 同步与运行时状态投影，不再直接认识 UI widget
  - 当前已集中：provider/model/api_type、streaming、selected memory sources、allowed/trusted channel sources、channel notice policy
  - 下一步继续把 invoked skills / channel activity summary 等会话级状态进一步集中化
- `core/modes/`
  - `types.py`：`ModeConfig`/groups 等
  - `defaults.py`：内置默认模式
  - `manager.py`：加载内置 + 可选 `work_dir/modes.json`
- `core/llm/llm_config.py`
  - 收口 provider / model / temperature / stream / system override
  - 作为 Conversation 的 LLM 配置事实来源
- `core/prompts/system.py`
  - system prompt 的“主体”由 mode 决定
  - tool/workspace framing 由策略决定（避免 Chat/Agent 混用规则）
- `core/channel/`
  - 提供统一 channel envelope / metadata / queue / prompt section 组装
  - 外部通道输入必须先正规化，再进入 conversation / prompt / UI
  - 当前 prompt section 已同时携带 active channels 与当前会话的 allow / trust / notice policy
- `core/prompts/assembler.py`
  - 为 runtime 暴露统一的 prompt/history/request 组装门面
- `core/state/services/memory_service.py`
  - 负责 session memory relevance scoring + workspace memory (`<work_dir>/.pycat/memory`) 读取
  - 当前已经具备第一层工作区记忆桥接能力
  - 当前已支持 conversation-scoped `memory_sources` 过滤，不再无条件把全部 memory 注入 prompt
- `core/llm/request_builder.py`
  - 只做“结构化 payload 构建”，不关心 UI
  - 现在消费 `LLMConfig`，不再只依赖 `Conversation.settings`
  - 对 openai-compatible thinking provider，会在请求前清洗缺失 reasoning 的 assistant 历史，并把坏历史降级为 recovered context，避免 `reasoning_content` 回放错误
- `core/runtime/turn_policy.py`
  - UI/runtime-facing policy，承载 `LLMConfig`
- `core/runtime/events.py`
  - TaskEvent → TurnEvent 桥接层，降低 UI 对底层执行细节的耦合
- `core/runtime/turn_engine.py`
  - 统一运行时执行包装，作为 `Task` 的渐进替身

### 3) Runtime / Presenter（当前实际编排层）

当前项目没有单独的 `Application` 目录，实际编排已拆分到：

- `core/app/`
  - 负责 bootstrap / AppState / coordinator / application-level selection state
- `ui/presenters/`
  - `conversation_presenter.py`：负责会话切换、会话壳体创建、selection snapshot 采集、provider/model 同步、对话设置、compact 入口、task panel 操作、mode/toggle/work_dir 更新
  - `conversation_command_presenter.py`：负责 slash command 结果分发、prompt invocation、显式 shell、文档更新与会话导出
  - `message_presenter.py`：负责消息发送、编辑/删除与对 streaming/prompt optimization presenters 的委托
  - `streaming_message_presenter.py`：负责 run policy 准备、流式回调、响应/错误落地与运行时 mode 同步
    - 运行时错误 assistant 消息会标记为 `runtime_error`，避免后续 thinking history 被脏错误消息污染
  - `prompt_optimization_presenter.py`：负责 prompt optimizer 请求准备、取消与 UI 回填生命周期
  - `settings_presenter.py`：负责 app settings / provider dialogs / theme / proxy / layout persistence 等壳层设置逻辑
  - `conversation_settings_dialog.py`（dialog）：现在同时承接 provider/model/mode 与 conversation-scoped memory/channel policy
  - `window_state_presenter.py`：负责把 `AppBootstrapState` / `AppState` / runtime 状态投影到 sidebar/input/header/menu 等窗口 chrome，并收口窗口关闭时的状态清理
  - presenter 之间的协作统一通过 `MainWindow` 上的公开协作属性（如 `conversation_presenter`、`message_presenter`、`settings_presenter`、`window_state_presenter`、`app_settings`）完成，避免跨对象依赖伪私有字段
- `ui/runtime/message_runtime.py`
  - 负责 Qt 线程、信号与后台执行桥接
  - 现在通过 `TurnEngine` / `TurnPolicy` / `TurnEvent` 做边界适配，而不是直接依赖所有 Task 细节
  - `askQuestions` 优先走聊天区内联卡片，`QuestionsDialog` 仅作 fallback
- `ui/widgets/chat_view.py`
  - 顶部 header 现在统一显示工作区、模型引用和紧凑运行态，主运行状态不再依赖右侧栏可见性
- `ui/widgets/stats_panel.py`
  - 右侧栏定位为可折叠辅助检查器，展示任务、记忆、文档、通道、会话概览和默认折叠的调试时间线
- `core/task/`
  - 负责多步执行、tool-call 回路、subtask 合并
- `core/commands/`
  - 负责 slash command 到 runtime action 的统一转换

### 4) UI（展示 + 事件转发）
- `ui/main_window.py`
  - 只负责 wiring：把 UI 信号转给 Application 层
  - 不直接拼装 prompt，不直接决定模式能力
- `ui/widgets/input_area.py`
  - 只负责采集输入、展示按钮、发信号
- `ui/widgets/*`
  - 对外优先暴露稳定的公开方法；presenter / `MainWindow` 不应依赖 widget 私有属性或私有方法（例如 `_mode_manager`、`_import_conversation` 这类内部实现细节）
  - 类似 provider/model/mode 同步这类 UI 状态恢复，应优先通过 `InputArea` 的公开同步方法完成，而不是直接操作其内部 combo 控件
  - `ChatView + MessageWidget` 现在同时承担两类轻交互投影：工具调用摘要卡片，以及 `askQuestions` 的内联问题卡片
  - `ModelRefCombo` 是集中模型引用选择器；全局设置中的压缩模型、提示词优化模型等应优先复用统一 `provider|model` 池，而不是各自使用普通文本框
  - `CollapsibleSection` 是折叠分组的通用组件，右侧辅助面板和后续长表单应优先复用它，而不是各自手写展开/收起逻辑

### 为什么要这样
- `MainWindow` 复杂度下降：不再同时承担“状态机 + 业务规则 + UI”
- 模式系统可扩展：新增/调整模式只改 `modes.json` 或 defaults
- Prompt 更可控：mode 驱动 prompt，避免把工具规则泄漏到纯聊天模式

## 项目模式配置（可选）
在工作区根目录放 `modes.json`：
- 结构参考仓库中的 `modes.example.json`
- 应用会在切换 work_dir 时自动刷新模式列表

## 数据流

### 1) 启动

`main.py` → `MainWindow()` →
- `StorageService.load_settings/load_providers/list_conversations`
- `InputArea.set_providers()`
- `Sidebar.update_conversations()`

### 2) 发送消息

`InputArea.message_sent` → `MainWindow._send_message()`：
- 校验 provider/model
- 追加 user `Message` 到 `Conversation`
- `StorageService.save_conversation()`
- `ChatView.add_message()`
- 然后启动流式：`MessageRuntime.start(...)`

### 3) 并发流式

**核心设计**：UI runtime 只负责线程和信号桥接，真实的消息/工具推进由 core runtime 完成。

- `MessageRuntime` 负责后台执行与 Qt 主线程通信
- `AppBootstrap` 负责把 settings/providers/catalog 的启动装配从 `MainWindow` 拆出来
- `WindowStatePresenter` 负责把 `AppBootstrap.load()` 的结果投影到 input/sidebar/header/layout，而不是让 `MainWindow._load_data()` 手工逐项恢复
- `TurnEngine` 负责对底层 `Task` 做统一执行包装，向上暴露更稳定的运行时边界
- `TurnPolicy` 负责把 UI 侧 mode / model / sampling 参数封装成统一运行时契约
- `TurnEvent` 负责把底层 `TaskEvent` 映射成更稳定的 UI/runtime 事件语义
- `ConversationPresenter` 负责通过 `InputArea` 的公开 API 采集 selection snapshot，再把纯 `ConversationSelection` 交给 `AppCoordinator`
- `AppCoordinator` 负责把 selection/settings 纯数据投影为可持久化会话状态
- `WindowStatePresenter` 负责把 `AppCoordinator.store` 中的 state 投影回 `ChatView` header、菜单可用态和输入区 streaming 状态
- `MessageRuntime` + `StreamingMessagePresenter` 负责把 `TurnEvent` 投影到 UI runtime strip，这相当于 PyCat 当前版本的轻量“runtime channel”
- `ConversationSettingsDialog + AppCoordinator` 负责把 memory/channel 会话级 policy 收口为可持久化状态，再交给 prompt layer 和 stats panel 投影
- `ProviderCatalogService` 负责 provider 目录的统一变换与持久化，避免 `MainWindow` / 设置页直接改列表再手动落盘
- `AppSettingsService` 负责 app settings 的统一 merge 与持久化，避免 `MainWindow` 手工拼 patch
- `core.task.Task` 负责多步工具调用与完成态判断
- `core.commands` 负责把 `/plan`、`/{skill}` 等命令转成统一的 runtime action

### 4) 切换会话时的恢复

`Sidebar.conversation_selected` → `MainWindow._on_conversation_selected()`：
- `StorageService.load_conversation()`
- `ChatView.load_conversation()`
- 如果该会话仍在生成：从 runtime/presenter 持有的流式状态恢复 UI 展示

### 5) system prompt / mode / 能力开关

- Mode：`Conversation.mode` 为模式 slug，由 `core/modes/ModeManager` 提供内置默认模式，并可从 `work_dir/modes.json` 覆盖。
- system prompt：`core/prompts/system.py` 的 `PromptManager` 负责生成系统提示词，主体由 mode 驱动；`core/llm/request_builder.py` 会把 system message 注入到 payload。
- request assembling：`core/prompts/assembler.py` 作为统一组装门面，供 `LLMClient` / runtime 使用。
- selection/app state：`core/app/coordinator.py` + `core/app/store.py` 提供应用级状态收口，避免 `MainWindow` 与 presenter 直接散写会话元数据。
- bootstrap loading：`core/app/bootstrap.py` 负责 initial settings/providers/catalog 装配。
- provider/settings catalog：`services/provider_catalog_service.py` + `services/app_settings_service.py` 提供 provider 与 app settings 的统一 load/save/update 入口。
- 能力开关：UI 的 MCP/Search/Thinking 开关会影响 `LLMClient.send_message(... enable_mcp/enable_search/enable_thinking ...)`，并进一步影响工具 schema 注入与 tool-call 执行。

## 模块职责

| 模块 | 职责 | 行数约 |
|-----|-----|-------|
| `AppBootstrap` | 启动装配加载（settings/providers/catalog） | ~60 |
| `AppSettingsService` | 应用设置 load/save/patch | ~20 |
| `ProviderCatalogService` | provider 目录持久化与列表变换 | ~80 |
| `WindowStatePresenter` | `AppBootstrapState` / `AppState` → sidebar/input/header/menu 的窗口状态投影与关闭清理 | ~140 |
| `ConversationPresenter` | 会话生命周期、壳体创建、selection snapshot 采集、provider/model 同步、compact、task panel、对话设置与工作区/mode/toggle 更新 | ~360 |
| `ConversationCommandPresenter` | command/export/shell/document update 相关 UI 编排 | ~260 |
| `StreamingMessagePresenter` | run policy 准备、流式回调、响应/错误落地与 mode 同步 | ~320 |
| `PromptOptimizationPresenter` | prompt optimizer 请求准备、取消与完成态 UI 回填 | ~120 |
| `SettingsPresenter` | app settings/provider 对话框、主题/代理应用与布局持久化 | ~180 |
| `MessagePresenter` | 消息发送、编辑/删除与 presenter 协调 | ~180 |
| `LLMClient` | LLM 请求编排（构建 payload、注入工具、HTTP/流式收发） | ~300 |
| `AppCoordinator` | 应用级会话编排与状态投影 | ~150 |
| `AppState/Store` | 轻量应用状态容器 | ~60 |
| `LLMConfig` | Conversation 级 provider/model/sampling 配置事实来源 | ~150 |
| `PromptAssembler` | prompt/history/request 组装门面 | ~80 |
| `TurnEngine` | 统一执行包装层 | ~60 |
| `request_builder` | 构建 API messages/body（system message 注入、消息结构化） | ~200 |
| `PromptManager` | system prompt 生成（Mode 驱动，含可选 workspace/tool framing） | ~200 |
| `TurnPolicy` | UI → runtime 执行策略对象（携带 `LLMConfig`） | ~70 |
| `TurnEvent` | Task 事件到 runtime/UI 事件的桥接 | ~40 |
| `ModeManager` | 模式加载（defaults + `work_dir/modes.json`） | ~120 |
| `ToolManager` | 工具 schema 汇总（系统工具/搜索/外部 MCP）+ 工具执行入口 | ~250 |
| `ToolRegistry` | 工具注册与权限封装执行（read/edit/command） | ~120 |
| `http_utils` | HTTP 错误格式化、SSE 解析 | ~100 |
| `thinking_parser` | `<think>`/`<analysis>` 标签提取 | ~80 |
| `PromptOptimizer` | 提示词优化（非流式、无工具、system override） | ~120 |
| `MessageRuntime` | Qt 流式运行桥接 | ~200 |
| `Task` | 多步执行、子任务与完成态处理 | ~250 |
| `Condenser` | 会话压缩/总结（可配置 summary model/system） | ~250 |
| `StorageService` | JSON 持久化（委托 importers） | ~300 |

## MCP/工具调用（Tool Calling）

### 1) 内置系统工具（无需外部 MCP Server）

当启用工具能力后，`core/tools/manager.py` 的 `ToolManager.get_all_tools(include_mcp=True)` 会注入一组“本地系统工具”（工具名为 OpenAI tool schema 的 function name）。当 `include_mcp=False` 时，这些系统工具不会暴露给模型（避免纯聊天模式意外拿到工具）。

- 只读：`list_directory` / `read_file` / `search_code`
- 编辑：`write_file` / `edit_file` / `delete_file` / `apply_patch`
- 命令：`execute_command` / `python_exec`
- 其它：`manage_state`（会话 state 维护）、`skill`

这些工具用于支持多步 tool-call 回路：模型先请求工具 → 应用执行工具 → 将 `role=tool` 结果回填给模型继续推理。

### 1.5) 网络搜索工具（可选）

当启用搜索能力后，`ToolManager.get_all_tools(include_search=True, ...)` 会额外注入：

- `builtin_web_search`

### 2) 外部 MCP Server（可选）

如果配置了 MCP servers（由 `StorageService` 读取本地配置），`ToolManager` 会按需通过 `stdio_client + ClientSession` 临时连接：

- `list_tools()` 获取工具列表并以 `mcp__{server}__{tool}` 形式命名，避免与内置工具冲突
- `call_tool()` 执行工具并把结果回填为 `role=tool` 消息，交给下一轮 LLM 继续推理

## 设计原则

1. **单一职责**：每个模块只做一件事
2. **依赖倒置**：上层依赖下层抽象，不反向
3. **薄门面**：`LLMClient` 负责编排，细节下沉到 `core/llm/*`、`core/prompts/*`、`core/tools/*`
4. **避免循环导入**：纯工具放 `utils/`，Qt 工具放 `ui/utils/`
