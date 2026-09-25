<div align="center">
  <img src="pycat/assets/pycat.svg" width="96" height="96" alt="PyCat cat logo" />
  <h1>PyCat</h1>
  <p><strong>A Python-native agent workbench · One core, five interfaces, inspectable files</strong></p>
  <p>Work on the desktop, automate with Python, and keep the process and results in your project.</p>
  <p>
    <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&amp;logoColor=white" alt="Python 3.11+" /></a>
    <a href="https://www.riverbankcomputing.com/software/pyqt/"><img src="https://img.shields.io/badge/Desktop-PyQt6-41CD52?logo=qt&amp;logoColor=white" alt="PyQt6 native desktop" /></a>
    <a href="https://nuitka.net/"><img src="https://img.shields.io/badge/Build-Nuitka-146C43" alt="Nuitka standalone build" /></a>
    <a href="https://github.com/nanhuayu/pycat/releases"><img src="https://img.shields.io/github/v/release/nanhuayu/pycat" alt="Latest public release" /></a>
    <a href="https://github.com/nanhuayu/pycat/stargazers"><img src="https://img.shields.io/github/stars/nanhuayu/pycat?style=flat" alt="GitHub Stars" /></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue" alt="AGPL-3.0-only" /></a>
  </p>
  <p><a href="https://github.com/nanhuayu/pycat#readme">简体中文 · Repository home</a> · English · <a href="https://github.com/nanhuayu/pycat/releases">Download</a> · <a href="docs/product/user-guide.md">User guide</a> · <a href="docs/product/developers.md">For developers</a></p>
</div>

**Py = Python; CAT = Chat · Agent · Tools.** Chat clarifies the goal, the agent advances the task, and tools connect real files, programs and services. The cat logo reflects a companion for everyday work.

PyCat is for developers, automation builders and advanced users working on real projects. Its **Python SDK, GUI, TUI, WebUI and CLI** share agent execution capabilities. Start with a local or SSH workspace, analyze code, run commands, organize material, generate images and inspect what happened along the way.

- **A Python core you can call directly:** embed the SDK in scripts, debug it and change its behavior in the same language as the native desktop.
- **Multiple interfaces, shared capabilities:** desktop, terminal, browser and messaging adapters reuse execution, tools and permission rules.
- **Sign in with an existing account:** connect ChatGPT / Codex or WorkBuddy / CodeBuddy, use an API key, or connect a local model service from the same workbench.
- **Files you can inspect and reuse:** conversations, input snapshots, original tool results, run records and delivered files stay locally available for search, backup and further work.

![PyCat brings project files, tasks and results together](media/pycat-hero.png)

*A product illustration. Actual interfaces and task examples appear in the capability sections below.*

## Quick start

**0.2.2** improves concurrent conversations, context compaction and durable memory, streamlines tool and run status displays, and adds an English desktop preview. See the [release notes and downloads](https://github.com/nanhuayu/pycat/releases/tag/v0.2.2).

**Use the desktop**

1. Download the Windows portable package from [Releases](https://github.com/nanhuayu/pycat/releases), extract it completely, and run **`pycat.exe`** inside `pycat-windows-x64`.
2. Configure a model in **Settings → Models and services**, then select a project folder.
3. Describe the goal, inspect execution and output files, and ask for further changes.

PyCat does not include model credits. The desktop currently defaults to automatic execution and access to all local paths; narrow access or require confirmation from the input area's permission menu. See the [user guide](docs/product/user-guide.md).

**Start with Python or the terminal**

Install from a cloned source checkout with Python 3.11+:

```shell
git clone https://github.com/nanhuayu/pycat.git
cd pycat
python -m pip install .                 # SDK + CLI
python -m pip install ".[gui,tui,web]"   # Optional desktop, TUI and WebUI

# After configuring a model
pycat exec "Analyze this project and propose a refactoring plan" -C . --mode plan --output-format json
pycat resume --last
```

The base SDK does not require Qt, Textual, FastAPI or local OCR. See the [developer guide](docs/product/developers.md) for model configuration and a runnable SDK example.

## Use an existing account or your own model service

Choose a connection in **Settings → Models and services → Login method**. Account sign-in authorizes in your browser; return to PyCat and select an available model. You do not need to launch a Codex or CodeBuddy CLI process first.

| Connection | How you can use it |
| --- | --- |
| **ChatGPT / Codex sign-in** | Sign in with an existing ChatGPT account and use models available to that account |
| **WorkBuddy / CodeBuddy sign-in** | Connect a mainland China WorkBuddy / CodeBuddy account and use its available models |
| **API key / local service** | Configure the address, protocol and model for a cloud API or an existing local service such as Ollama |

Account sign-in and API keys are separate access methods. The service determines quotas and available models; PyCat does not supply credits or guarantee that a subscription includes every model. Account integrations are experimental and may change with upstream services. See the [user guide](docs/product/user-guide.md#1-install-and-connect-a-model) for setup.

## What Python makes practical

Agent orchestration, tools, model adapters, application services and desktop interactions live in a Python codebase. Developers with existing Python projects can make agent capabilities part of their own programs.

| What you want to do | PyCat's approach | Practical value |
| --- | --- | --- |
| Embed an agent in a program | `from pycat import PyCat, RunRequest`, called in process | Reuse Python objects, exceptions and debugging without launching a desktop or creating an HTTP bridge |
| Change model strategy, tools or execution | Edit the Python core and verify through SDK / CLI | Read, experiment and check behavior in one language |
| Add screenshots, clipboard or tray interactions | Native Python + PyQt6 components | Integrate with the desktop; terminal and preview widgets need no WebEngine |
| Move from manual use to batch automation | Explore in the GUI, drive tasks through SDK / CLI | Reuse the agent core, tool boundary and configuration contracts |
| Deliver to users without Python installed | Nuitka standalone Windows builds | Extract and run; the desktop needs no Electron, Node.js or browser engine |

**“Python-native” describes the core and desktop implementation.** The optional WebUI contains HTML / CSS / JavaScript; Qt, OCR and other dependencies contain native code. Some external MCP servers may require Node.js. This reduces the language boundary between the core and desktop; actual speed, memory and package size still depend on features and dependencies.

## One core, five interfaces

| Interface | Best suited to | How to use it |
| --- | --- | --- |
| **SDK** | Python applications, batches and evaluation experiments | Async calls, events, results, cancellation and conversation continuation |
| **GUI** | Daily project work, settings, images and run inspection | Native PyQt6; run `pycat-gui` after a source installation |
| **TUI** | Ongoing work in an interactive terminal | Install `tui`, then run `pycat` |
| **WebUI** | Browser access to a running PyCat host | Install `web`, then run `pycat serve` |
| **CLI** | Scripts, pipelines and scheduled jobs | `pycat exec` / `resume`, with JSON or streamed events |

Feishu / Lark, DingTalk, Telegram, QQ and WeChat can also serve as task entry points. Interfaces expose controls suited to their use cases; not every interface has the same settings panels.

Python, Qt and terminal tooling provide a basis for cross-platform work. **Windows portable builds are available; SSH remote hosts support Windows / Linux.** Source use on other systems requires appropriate dependencies and validation of system features. The web host and desktop manage separate data directories; they must not concurrently write to the same directory.

## Architecture overview

![PyCat architecture, with Chinese labels: five interfaces and messaging channels share a Python core, connecting model accounts, tools and workspaces to inspectable files](media/pycat-architecture.png)

*A capability overview based on the current implementation. Interfaces reuse the same core code; independently running applications still own separate data directories. Arrows show calls and data exchange, not a mandatory sequence through every module.*

## Keep the process and results inspectable

**Files are the basic medium for PyCat's work products and execution evidence.** After a conversation ends, use editors, file managers, scripts or version control to keep working with the material.

| Content | Stored as | What you can do with it |
| --- | --- | --- |
| Conversations and configuration | Local JSON | Inspect, export, back up and continue conversations |
| Inputs and tool results | Session input snapshots, original content and image archives | Check sources, read complete tool output and inspect material beyond summaries |
| Execution | JSONL events and optional request / response attachments | Investigate steps in the run inspector or analyze records with scripts |
| Deliverables and knowledge | Ordinary project files, Markdown knowledge pages and memory files | Search, compare, edit, archive and reuse in later tasks |

Large session resources for local projects live under `.pycat/sessions/<conversation-id>/`. Archived originals and compressed summaries are separate, allowing concise displays and model context while preserving archived sources.

Lightweight run events are recorded by default. Enable detailed logging for full request / response diagnostics. These records explain execution; they are not a complete machine snapshot or automatic replay of every external action. See [file storage and inspectable runs](docs/product/developers.md#file-storage-and-inspectable-runs) for locations, SQLite indexes and backup scope.

![Run inspector: a call tree, requests, responses and structured events](media/screenshots/run-inspector.png)

## Capabilities for real projects

| Capability | Current support |
| --- | --- |
| **Model protocols** | OpenAI Chat Completions / Responses, Anthropic Messages and Ollama |
| **Image generation and editing** | OpenAI Images, Qwen DashScope and Seedream Ark; separate generation and edit addresses |
| **Modes and subagents** | Chat, Agent, Plan and Review; configurable models, instructions, tool categories and delegation scope |
| **Skills and MCP** | Management, discovery and update controls; built-in find-skills / skill-creator; MCP stdio, SSE and Streamable HTTP |
| **Interactive shells** | Create shells, inspect status, send more input, take over and end processes |
| **Images and OCR** | Screenshot recognition, local or vision-model OCR, SVG / image previews and inline Markdown images |
| **Local and remote work** | Local workspaces, SSH file and process operations, and an independent web host |
| **Knowledge and memory** | Project deliverables, knowledge pages, reusable memories and preferences |

### Image generation and delivery

Generated images appear inline in Markdown conversations. Delivered files remain available for preview, further editing and use in your project.

![An actual image task: the result appears in the conversation and is delivered as a reusable file](media/screenshots/image-generation.png)

*An actual user-supplied task screenshot. The generic agent diagram is that task's output, separate from the PyCat architecture overview above.*

### Screenshot recognition

Switch between image and text views after capturing content, then select or copy recognized text. OCR can also use a dedicated vision model and prompt.

![Capture window: image, text and extraction status](media/screenshots/capture-ocr.png)

### SSH projects

Operate on remote files and processes from the local interface. The remote host needs SSH and Python 3.11+, without a full PyCat installation.

<img src="media/screenshots/ssh-workspace.png" width="522" alt="SSH host, port and project directory settings" />

### Define how you work in settings

**Models and services:** configure chat and image protocols separately, with model roles, reasoning options and dedicated capabilities.

![Model and service settings](media/screenshots/models.png)

**Execution and permissions:** configure main modes, subagents, tool categories, instructions and completion behavior.

![Mode, tool category and instruction settings](media/screenshots/modes.png)

**Tools and capabilities:** manage Skills, MCP, model capabilities, search, computer and browser tools, and OCR.

![Skill management and built-in find-skills](media/screenshots/settings.png)

### Start tasks from messaging platforms

Configure a bot and bind a project to use **Feishu / Lark, DingTalk, Telegram, QQ or WeChat**. Send supported images and files as inputs, and receive explicitly delivered results.

<img src="media/screenshots/wechat-image.jpg" width="320" alt="An actual WeChat exchange: an image followed by a reply about its content" />

The computer running PyCat must remain online. Attachment and permission limits differ by platform. This user-supplied screenshot shows an actual WeChat bot exchange; the bot name is its display name in that chat.

*Image generation, capture, SSH, run inspection and WeChat screenshots are user-selected actual interfaces. Settings screenshots use isolated example configuration. Screenshots illustrate operations; model output and OCR content still need review.*

## Choosing an implementation approach

Compare the integration work you need, as well as feature lists:

| Your priority | The tradeoff | PyCat's choice |
| --- | --- | --- |
| **An existing Python project** | Remote APIs, process boundaries or an in-process SDK | An in-process Python SDK, plus CLI and web interfaces |
| **Desktop and browser experiences** | System integration, web reuse and interface maintenance | A PyQt6 desktop and optional WebUI sharing the Python core |
| **Inspectable data** | Access to conversations, original tool output and deliverables | Local files for primary content, with run inspection and structured output |
| **Customization and iteration** | Changing behavior and checking it on your own tasks | Python source, model and mode settings, Skills and MCP |
| **Deployment and maintenance** | Runtime dependencies and platform validation | Optional SDK extras and a Windows distribution with selected interfaces and local OCR |

Languages and UI frameworks serve different needs. PyCat emphasizes **direct integration with Python, shared execution and inspectable file workflows**. Package size, speed and task success require comparable measurements, rather than blanket claims.

## A foundation for agent iteration

Use the SDK to fix a task set and budget, compare models, modes, skills or code candidates, and inspect differences through files and run records. Python source is accessible to agents for reading, editing, execution and checking; the native desktop supports human review.

This makes PyCat a useful engineering foundation for **agent self-improvement and RSI experiments**. Execution, extension, recording and evaluation integration are available today. A complete autonomous loop for candidate management, independent evaluation, selection, deployment and rollback must still be built separately. See the [developer guide](docs/product/developers.md).

## Participate

Contribute reproducible task examples, model adapters, skills, MCP integrations, cross-platform fixes or native desktop improvements. Report issues with the version, reproduction steps, expected result and redacted evidence.

If Python-native integration, multiple interfaces and inspectable files are useful to you, **Star** the project, try it and share reproducible feedback. Follow its interest over time on [Star History](https://www.star-history.com/#nanhuayu/pycat&Date).

[Report an issue](https://github.com/nanhuayu/pycat/issues) · [Contribute](https://github.com/nanhuayu/pycat/pulls) · [Developer guide](docs/product/developers.md) · [User guide](docs/product/user-guide.md)

PyCat is licensed under [AGPL-3.0-only](LICENSE). Projects and conversations are stored locally; cloud models, network tools and messaging platforms receive the content required for their configured operations.
