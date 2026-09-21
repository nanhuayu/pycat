# 使用 Python、终端与远程工作区

[项目介绍](../../README_zh.md) · [桌面使用指南](user-guide_zh.md) · [English](developers.md)

PyCat 面向需要实际使用 Agent、编写自动化和扩展工具的开发者。Python SDK、CLI、TUI、原生桌面、Web 工作台和消息通道复用应用服务、任务执行、工具权限与会话内容。你可以先在界面中验证任务，再通过 Python 或终端接入自己的工作流。

## 选择入口

从项目源码根目录安装，要求 Python 3.11+：

```shell
python -m pip install .              # Python SDK 与 CLI
python -m pip install ".[gui]"        # 加上原生 Qt 桌面
python -m pip install ".[tui,web]"    # 加上 TUI 和 Web 工作台
python -m pip install ".[ocr]"        # 可选本地 OCR
```

基础安装不引入 Qt、Textual、FastAPI 或本地 OCR 推理库。应用与 SDK 的主要逻辑采用 Python；文件处理、加密、终端和界面等仍使用含原生代码的依赖，因此“Python 实现”不等于“全依赖纯 Python”。源码开发的桌面命令是 `pycat-gui`；Windows 便携包中的桌面程序是 `pycat.exe`。

已有模型配置后，可用 CLI 执行或继续任务：

```shell
pycat exec "解释当前项目的入口与依赖" -C . --mode chat
pycat exec "分析当前项目并给出重构计划" -C . --mode plan --output-format json
pycat resume --last
pycat serve
```

`json` 返回结构化最终结果，`stream-json` 输出逐行事件。非交互调用不会自动批准需要确认的工具；问题也不会被自动代答。每个命令的完整参数可通过 `--help` 查看。

## 可以连接哪些协议

| 用途 | 当前协议 | 使用方式 |
| --- | --- | --- |
| 聊天与工具调用 | OpenAI Chat Completions、OpenAI Responses、Anthropic Messages、Ollama Chat | 在“模型与服务”选择聊天接口类型，按模型设置输入能力、上下文和推理参数 |
| 图像生成与编辑 | OpenAI Images、Qwen Image 的 DashScope 同步接口、Seedream 的 Ark 接口 | 图像协议与聊天协议分开配置；生成与编辑地址可分别指定 |
| 外部工具 | MCP stdio、SSE、Streamable HTTP | 在 MCP 设置中配置服务、传输、凭据和工具 |
| Python 嵌入 | 进程内 `PyCat` 公共接口 | 由 Python 宿主管理生命周期、请求、事件和结果 |
| Web 与附加客户端 | HTTP API、SSE 事件 | 连接独立启动的 Web 宿主，使用访问令牌 |

协议适配不代表所有服务和模型拥有相同能力。工具调用、视觉输入、推理参数、图像尺寸等以所选模型和服务为准。ChatGPT / Codex 与 WorkBuddy / CodeBuddy 账号连接属于实验性集成，受上游权限及协议变化影响。

## 模式、权限和子 Agent

| 模式 | 主要用途 |
| --- | --- |
| Chat | 问答、解释、写作，按配置读取资料或调用工具 |
| Agent | 读取与修改项目、执行命令、检查结果 |
| Plan | 调查上下文、澄清取舍、形成方案；默认不开放内置编辑与执行类别 |
| Review | 检查实现、整理问题和证据，可作为主模式或子 Agent |

模式可以配置指令、工具类别和完成方式。子 Agent 还可选择模型、轮次上限与共享上下文范围。内置 Explore、Search、Read Analyze 等配置用于任务委派；它们与主会话模式分别管理。

有效工具取模式、当前会话和调用方等范围的交集。模式名称与指令不是操作系统沙箱：外部 MCP 或程序仍可能有副作用，需要结合其权限与实际行为配置。当前桌面默认自动执行和允许所有本地路径，可在输入区收窄。

## 一个可运行的 SDK 起点

先设置 `MODEL_BASE_URL`、`MODEL_ID`，以及服务需要时的 `MODEL_API_KEY`。下面使用独立数据目录和 OpenAI Chat Completions 兼容接口；不会依赖桌面进程或点击界面。

```python
import asyncio
import os
from pathlib import Path

from pycat import ModelProfile, Provider, PyCat, RunRequest


async def main():
    async with PyCat(data_dir="./.pycat-sdk-demo") as app:
        provider = Provider(
            name="sdk-demo",
            api_type="openai_compatible",
            api_base=os.environ["MODEL_BASE_URL"],
            api_key=os.environ.get("MODEL_API_KEY", ""),
            models=[ModelProfile(model_id=os.environ["MODEL_ID"])],
        )
        if not app.models.save([provider]):
            raise RuntimeError("Could not save model configuration")
        result = await app.run(RunRequest(
            text="Explain the purpose of this workspace in three sentences.",
            model=f"{provider.name}|{os.environ['MODEL_ID']}",
            mode="chat",
            work_dir=str(Path.cwd()),
        ))
        if result.error:
            raise RuntimeError(result.error)
        print(result.status.value, result.conversation.id)
        if result.final_message:
            print(result.final_message.content)


if __name__ == "__main__":
    asyncio.run(main())
```

示例保存的是专用演示目录中的模型目录。实际应用使用自己的配置流程；继续会话时传入 `conversation_id`，省略模型、模式等字段可保留已有选择。一个数据目录只能由一个活动应用持有，独立 SDK 不应同时打开桌面正在使用的数据目录。

需要实时输出时，在已打开的 `app` 中使用：

```python
from pycat import RunEventKind, RunRequest

async with app.start(RunRequest(
    text="Summarize the previous answer.",
    conversation_id=result.conversation.id,
)) as run:
    async for event in run.events():
        if event.kind == RunEventKind.TEXT_DELTA:
            print(event.data, end="", flush=True)
    followup = await run.result()
```

`run.cancel()` 可请求取消；退出运行上下文会收尾未完成的任务。每个运行只有一个事件消费者，接入后应及时消费；取消不会回滚已经发生的文件修改或外部操作。检查结果状态、保存内容和任务验收结果，不能只看最后一句“完成”。

## 两种不同的远程使用方式

**SSH 工作区**用于操作远端项目。在工作区选择器中填写 SSH 主机、端口和项目目录；SDK/CLI 使用同一工作区地址，例如 `ssh://user@host/home/user/project`。本地保留 Agent、模型连接和会话，远端提供文件与进程操作。远端需要 SSH 服务与 Python 3.11+；无需在远端安装完整 PyCat。当前实现覆盖 Windows / Linux 远端，macOS 远端未列入已支持范围；断线自动续跑也不属于当前能力。

**Web 宿主**用于从浏览器或另一个终端访问宿主中的 PyCat。`pycat serve` 默认监听本机，终端可以用 `--endpoint` 和访问令牌附加该宿主。工作区路径属于宿主机器；Web 宿主与桌面不是自动同步的两个数据所有者。非本地监听需要显式令牌，网络访问控制与 HTTPS 由部署环境提供。

## 扩展现有工作流

| 想改变什么 | 合适入口 |
| --- | --- |
| 可复用的任务方法 | Skill；内置 find-skills 和 skill-creator 提供发现与编写指导 |
| 外部服务或已有程序 | MCP，或在项目中使用现有 Shell 工具 |
| 角色、指令、可用工具与委派 | 主模式与子 Agent 配置 |
| 固定的图像、OCR 或其他模型调用 | 模型与服务、能力及 OCR 设置 |
| 嵌入脚本、评估程序或另一应用 | Python SDK、CLI 的结构化输出，或 Web 宿主接口 |
| 增加原生桌面交互 | 在源码中扩展 PyQt 界面并复用现有应用服务 |

桌面使用 Python + PyQt6，终端和内容预览采用原生组件，无需 JS / WebEngine；可选 Web 工作台另有自己的网页界面。当前已集成托盘、截图、剪贴板、文件预览和 Shell 交互。Qt 和 Python 生态便于继续接入系统功能，但系统权限、窗口生命周期、后台任务和各平台行为仍需验证；当前没有通用的桌面 UI 插件热加载接口。

## Agent 迭代与自改进实验

Python 源码便于检查、修改和调试，SDK 便于批量发起任务、读取结构化结果和诊断轨迹。模式、技能和模型可分别配置，因此 PyCat 适合作为 **Agent 工作流与评估实验的基础**。这是一项工程判断，不是对迭代速度或成功率的性能承诺。

需要区分：复用记忆或技能、外部程序驱动候选评估、Agent 修改自身代码并持续验证，是不同层次的能力。PyCat 尚未提供自动候选管理、独立评估、优胜选择和发布回滚组成的完整 RSI 闭环，也不因使用 Python 就自动提升底层模型能力。[SICA](https://arxiv.org/abs/2504.15228) 与 [DGM](https://arxiv.org/abs/2505.22954) 可作为代码自改进研究的参考，论文结果不能当作 PyCat 的效果证明。

做这类实验时，可以由外部评估程序固定模型、任务集与预算，在独立分支/工作目录中生成候选，分别比较质量、成本和耗时；评估器与候选分开版本管理，通过独立验收后再决定是否采用。这是建议的实验方法，当前需要自行搭建相应流程。

## 参与项目

欢迎围绕模型协议兼容、真实任务示例、SSH 与跨平台体验、原生桌面交互、技能与 MCP 接入提交问题或改进。反馈应包含版本、复现步骤、期望结果和已脱敏材料。一个范围清晰、可以重复验证的改进，比增加未验证的支持标签更容易评审和维护。

[Issues](https://github.com/nanhuayu/pycat/issues) · [Pull requests](https://github.com/nanhuayu/pycat/pulls) · [许可证](../../LICENSE)
