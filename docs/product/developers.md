# Python, terminal and remote workspaces

[Project introduction](../../README.md) · [Desktop user guide](user-guide.md) · [简体中文](developers_zh.md)

PyCat is for developers who want to use agents, build automation and extend tools. Its Python SDK, CLI, TUI, native desktop, web workbench and messaging channels share application services, task execution, tool permissions and conversation content. Try a task in the interface, then connect the same capabilities to your own Python or terminal workflow.

## Choose an entrypoint

Install from the project source root with Python 3.11+:

```shell
python -m pip install .              # Python SDK and CLI
python -m pip install ".[gui]"        # Add the native Qt desktop
python -m pip install ".[tui,web]"    # Add TUI and web workbench
python -m pip install ".[ocr]"        # Optional local OCR
```

The base install does not require Qt, Textual, FastAPI or local OCR inference libraries. Application and SDK logic is written primarily in Python; document processing, cryptography, terminals and GUI still use dependencies containing native code. A Python implementation does not mean every dependency is pure Python. The desktop command in a source installation is `pycat-gui`; the Windows portable desktop executable is `pycat.exe`.

With a model configured, run or resume tasks from the CLI:

```shell
pycat exec "Explain this project's entrypoints and dependencies" -C . --mode chat
pycat exec "Investigate this project and propose a refactoring plan" -C . --mode plan --output-format json
pycat resume --last
pycat serve
```

`json` returns a structured final result; `stream-json` emits events line by line. Noninteractive calls do not automatically approve tools that require confirmation or answer questions on your behalf. Use `--help` for command options.

## Supported protocols

| Purpose | Current protocols | Configuration |
| --- | --- | --- |
| Chat and tool calls | OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, Ollama Chat | Choose the chat API in Models & services; configure model inputs, context and reasoning options |
| Image generation and editing | OpenAI Images, Qwen Image through synchronous DashScope, Seedream through Ark | Configure image protocols separately from chat; generation and editing addresses can differ |
| External tools | MCP stdio, SSE, Streamable HTTP | Configure transport, service, credentials and tools in MCP settings |
| Python embedding | In-process `PyCat` API | The Python host owns application lifetime, requests, events and results |
| Web and attached clients | HTTP API and SSE events | Connect to a separately started web host using an access token |

Protocol support does not imply identical model capabilities. Tool calls, vision inputs, reasoning settings and image dimensions depend on the model and service. ChatGPT / Codex and WorkBuddy / CodeBuddy account connections are experimental and depend on upstream access and protocol behavior.

## Modes, permissions and subagents

| Mode | Main purpose |
| --- | --- |
| Chat | Questions, explanations and writing, with configured reading and tools |
| Agent | Read and edit a project, run commands and inspect results |
| Plan | Investigate context and tradeoffs; built-in edit and execution categories are disabled by default |
| Review | Inspect an implementation and report issues with evidence, as a main mode or subagent |

Configure instructions, tool categories and completion behavior per mode. Subagents also have model selection, turn limits and shared-context scope. Built-in Explore, Search and Read Analyze profiles support delegation and are managed separately from main conversation modes.

Effective tools are narrowed by the mode, conversation and caller. A mode name or instruction is not an operating-system sandbox: external MCP servers and programs may have side effects. Match their permissions to the task. The current desktop defaults allow automatic execution and access to all local paths; narrow these in the composer when needed.

## A runnable SDK starting point

Set `MODEL_BASE_URL`, `MODEL_ID` and, when required, `MODEL_API_KEY`. This example uses a separate data directory and an OpenAI Chat Completions compatible endpoint. It does not depend on the desktop process or UI automation.

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

The example saves its model catalog in a dedicated demo directory. A real application should use its own configuration workflow. Pass `conversation_id` to continue; omitting model and mode preserves the saved choices. A data directory has one active application owner. An independent SDK process must not open the directory an active desktop owns.

For streaming, inside the already-open `app`:

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

Request cancellation with `run.cancel()`. Leaving the run context cleans up unfinished work. Each run has one event consumer, which should keep up with the stream. Cancellation does not undo file changes or external actions. Check result status, saved content and task acceptance criteria, rather than relying on a final statement of completion.

## Two kinds of remote use

**SSH workspaces** operate on remote projects. Select an SSH host, port and project directory in the workspace picker. SDK and CLI use the same workspace URI, such as `ssh://user@host/home/user/project`. The local application keeps the agent, model connection and conversation; the remote machine provides file and process operations. The remote host needs SSH and Python 3.11+, without a full PyCat installation. The current implementation supports Windows / Linux remote hosts; macOS remote hosts are outside the supported scope. Automatic continuation after a disconnect is not implemented.

**The web host** lets a browser or another terminal access a hosted PyCat application. `pycat serve` listens locally by default. A terminal can attach using `--endpoint` and the access token. Workspace paths belong to the host machine; the web host and desktop do not automatically synchronize separate data owners. Nonlocal listening requires an explicit token; the deployment environment provides network access controls and HTTPS.

## Extend a workflow

| What you want to change | Entry point |
| --- | --- |
| Reusable task methods | Skills; built-in find-skills and skill-creator guide discovery and authoring |
| External services or programs | MCP or the existing shell tools |
| Roles, instructions, allowed tools and delegation | Main modes and subagent profiles |
| Dedicated image, OCR or other model calls | Model connections, capabilities and OCR settings |
| Scripts, evaluation programs or another application | Python SDK, structured CLI output or the web host interface |
| Native desktop interactions | Extend the PyQt source and reuse existing application services |

The Python + PyQt6 desktop uses native terminal and content-viewing widgets, without JS / WebEngine. The optional web workbench has a separate web UI. The desktop already integrates a tray icon, screenshots, clipboard, file previews and shell interaction. Qt and Python make further system integrations practical, but permissions, widget lifetime, background tasks and platform behavior still need validation. There is no general hot-loading interface for desktop UI plugins.

## Agent iteration and self-improvement experiments

Python source is straightforward to inspect, modify and debug. The SDK can drive task batches and expose structured results and diagnostic traces; modes, skills and models can be configured separately. These are useful foundations for **agent workflow and evaluation experiments**, not measured claims about iteration speed or success rates.

Reusing memories or skills, running an external candidate evaluator, and recursively modifying and validating agent code are different capabilities. PyCat does not currently provide a complete RSI loop covering candidate management, independent evaluation, selection, deployment and rollback. Using Python does not itself improve the underlying model. [SICA](https://arxiv.org/abs/2504.15228) and [DGM](https://arxiv.org/abs/2505.22954) are references for code self-improvement research; their results are not evidence of PyCat's performance.

A possible experiment fixes the model, task set and budget, creates candidates in separate branches or work directories, and compares quality, cost and time. Version the evaluator independently from candidates and use separate acceptance checks before adoption. This is a proposed experimental workflow that you must currently assemble yourself.

## Participate

Useful contributions include protocol compatibility, reproducible task examples, SSH and cross-platform behavior, native desktop interactions, skills and MCP integration. Include the version, reproduction steps, expected result and redacted evidence. Focused changes with repeatable validation are easier to review and maintain.

[Issues](https://github.com/nanhuayu/pycat/issues) · [Pull requests](https://github.com/nanhuayu/pycat/pulls) · [License](../../LICENSE)
