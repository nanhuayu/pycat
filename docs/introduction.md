# PyCat Agent

> PyCat Agent | LLM chat / agent / tools

PyCat Agent 是一款基于 PyQt6 的桌面 AI 客户端，将 **Chat**、**Agent** 与 **Tools** 三类工作流统一到同一个应用中，适合希望在本地桌面环境中构建完整 AI 工作台的用户。

## ✨ 核心定位

- **LLM Chat**：面向日常问答、写作、分析与代码辅助。
- **Agent**：支持模式切换、任务流与更复杂的智能体式交互。
- **Tools**：通过 MCP、技能文件与工具调用扩展 AI 的实际执行能力。

## 🌟 关键能力

- 多模型供应商支持：OpenAI、Claude、Ollama、Gemini、DeepSeek 等。
- 深度思考渲染：自动解析并展示 `<think>` / `<analysis>` 内容。
- 会话管理：支持导入、导出、编辑、分支与多模态消息。
- MCP / Skills 扩展：支持工具调用、文件操作、搜索服务等扩展能力。
- 桌面体验：暗色 / 亮色主题、高 DPI 适配、清晰布局与可观测统计面板。

## 🚀 启动方式

```bash
python -m pip install -r requirements.txt
python main.py
```

## 🛠️ Windows 打包（Nuitka）

```powershell
python -m pip install -r requirements.txt
python -m pip install nuitka ordered-set zstandard
powershell -ExecutionPolicy Bypass -File .\build_nuitka.ps1
```

执行后将在项目根目录生成 `PyCat-Agent-windows-x64.zip`。

## 📜 开源协议

本项目采用 **GNU Affero General Public License v3.0 (AGPL-3.0)**。

可用于商业场景，但必须完整遵守 AGPL-3.0 的相关义务。完整协议见 [`LICENSE`](./LICENSE)。

## 📚 相关文档

- [`README.md`](./README.md)
- [`README_zh.md`](./README_zh.md)
- [`ARCHITECTURE.md`](./ARCHITECTURE.md)
- [`docs/PLAN_AGENT_RUNTIME_REFACTOR.md`](./docs/PLAN_AGENT_RUNTIME_REFACTOR.md)

