# PyCat

**A programmable local agent workbench: Python SDK, native desktop and remote projects.**

English · [简体中文](README_zh.md) · [Releases](https://github.com/nanhuayu/pycat/releases) · [User guide](docs/product/user-guide.md) · [For developers](docs/product/developers.md) · [Report an issue](https://github.com/nanhuayu/pycat/issues)

![PyCat brings your own materials and tasks together into useful results](media/pycat-hero.png)

PyCat is for developers, automation builders and advanced users working on real projects. In a local or SSH workspace, analyze code, run commands, organize material and work with images, keeping results as files you can inspect and revise. The Python SDK, CLI, TUI, native desktop, web workbench and messaging channels share task execution and tool capabilities.

## Why developers might try it

| Capability | What it enables |
| --- | --- |
| **Python SDK and multiple interfaces** | Submit tasks, consume events and inspect results from scripts or the desktop; the base SDK does not require Qt. |
| **Multiple model protocols** | Configure OpenAI Chat Completions / Responses, Anthropic Messages and Ollama; image connections also support OpenAI Images, Qwen DashScope and Seedream Ark. |
| **Modes and subagents** | Choose Chat, Agent, Plan or Review; configure instructions, tool categories, subagent models and context scope. |
| **Local and remote projects** | Operate on Windows / Linux project files and processes over SSH, or connect browsers and terminal clients to a separate web host. |
| **Extensible workflows** | Reuse methods with Skills, connect services through MCP, choose dedicated capability models and take over interactive shells. |
| **Native desktop and visible execution** | Python + PyQt6 integrates screenshots, clipboard, tray, previews and run inspection; desktop terminal and preview widgets need no JS / WebEngine. |

These are current integration and configuration capabilities, not a claim of equal compatibility across every service, system or task. See the [developer guide](docs/product/developers.md) for protocol scope, remote requirements and SDK examples.

## Start with the work in front of you

| What you need | Something you can ask |
| --- | --- |
| Analyze or change a project | “Locate the relevant code and callers, propose a plan, then implement and verify the agreed change.” |
| Make sense of a folder of material | “Read these documents. Compare their main points, flag open questions, and save a brief.” |
| Extract text from images | “Read these screenshots, keep the headings and paragraphs, and mark anything uncertain.” |
| Create or revise an illustration | “Create an image for this introduction. Keep the subject and simplify the background.” |
| Handle repetitive file work | “Explain a plan first, then organize these files by date and list the changes.” |

These are example tasks. Results depend on the model, the quality of your materials and the tools you have configured. Review important work before relying on it.

![Three steps: bring your materials, describe the task, and review the result](media/pycat-workflow.png)

1. **Bring your materials:** choose a project folder and add files or screenshots.
2. **Describe the task:** explain the goal, the scope and the output you want.
3. **Review and continue:** inspect the files and progress, then ask for changes.

## See the work and keep the result

Organize conversations by project. Inspect the steps and status of longer tasks. Generated images appear directly in the conversation; files can be previewed, copied or saved. Continue in the same conversation with the materials already at hand.

![An actual image-generation task with an inline result and a delivered file](media/screenshots/image-generation.png)

*This user-supplied screenshot shows an actual image-generation task and file delivery. The generic AI agent architecture diagram is task output, not documentation of PyCat's architecture; image content and replies still need review. The paper illustrations explain use cases, while settings screenshots use isolated demonstration configurations. Consult release notes for the features in a downloaded package.*

<details>
<summary>View screenshot recognition, SSH workspaces and run inspection</summary>

**Screenshot recognition:** switch between image and text views after capture, then select or copy extracted text. This screenshot shows the image view and extraction-complete status.

![Screenshot window with image/text views and extraction-complete status](media/screenshots/capture-ocr.png)

**SSH workspaces:** configure the host, port and remote directory to use file and process tools on a remote project. This is the connection setup screen; see the [developer guide](docs/product/developers.md) for requirements.

<img src="media/screenshots/ssh-workspace.png" width="522" alt="SSH workspace connection settings" />

**Run inspection:** follow model and tool steps in the run tree, then inspect requests, responses and events to understand task behavior and failures.

![An actual task's run tree and structured event inspection](media/screenshots/run-inspector.png)

*These user-supplied interface screenshots show specific features. They are not benchmarks of OCR accuracy, remote connectivity or task quality.*

</details>

## Add capabilities when you need them

- **Choose your models.** Connect a cloud service or a local model you have deployed. Configure chat, image generation and text recognition separately.
- **Reuse working methods.** Manage skills and connect external tools in settings. Skills and MCP have management, discovery and update entrypoints.
- **Keep useful material.** Organize results as project material, and manage reusable memories and preferences.
- **Work through longer tasks.** Inspect run records. When a task needs commands, view its shell, provide more input or stop the process.

## Define how the work gets done

Configure models, modes, tools and permissions separately. Changing a model need not rebuild the workflow; adding a skill does not automatically expand permissions. Images and OCR can use dedicated models instead of the chat model.

<details>
<summary>View the actual model, Agent mode and skill settings</summary>

**Models & services:** separate chat protocols and image interfaces, with model capabilities and reasoning options.

![Model settings with separate chat and image interfaces](media/screenshots/models.png)

**Modes & permissions:** configure main modes, subagents, tool categories, instructions and completion behavior.

![Agent mode and tool category settings](media/screenshots/modes.png)

**Tools & capabilities:** manage Skills, MCP, model capabilities, search, computer/browser tools and OCR.

![Skills management and the built-in find-skills entry](media/screenshots/settings.png)

*These screenshots use isolated demonstration settings without real credentials. Configure other settings as your tasks require.*

</details>

## Connect Python or the terminal

Install from the source root with Python 3.11+. With a model configured:

```shell
python -m pip install .
pycat exec "Investigate this project and propose a refactoring plan" -C . --mode plan --output-format json
pycat resume --last
```

Use `from pycat import PyCat, RunRequest` to embed execution in a Python host, consume events, cancel work and continue conversations. The [developer guide](docs/product/developers.md) includes model setup, runnable examples and remote access details.

Python source and structured results also make it practical to connect external evaluators and compare models, modes or skills. **PyCat provides foundations for agent iteration experiments.** It does not currently provide a complete RSI loop for self-modification, independent evaluation, selection, deployment and rollback.

## Use a familiar messaging app

After configuring a bot and binding it to a project, interact with PyCat through **Feishu / Lark, DingTalk, Telegram, QQ or WeChat**. Supported images and files can be sent as input, and explicitly delivered results can return to the same chat.

![A desktop project connected to a messaging conversation that receives a file result](media/pycat-connected.png)

The computer running PyCat must remain online, and the platform connection must be configured. This is not a separate mobile app. Supported attachments, permissions and size limits vary by platform.

<details>
<summary>View an actual image exchange through WeChat</summary>

This user-supplied conversation shows an image sent to a WeChat bot and a reply about its content. Recognition and response quality should be checked for the intended use.

<img src="media/screenshots/wechat-image.jpg" width="320" alt="A real WeChat conversation with an image attachment and a content-related reply" />

*The bot name is the display name in this conversation. This demonstrates messaging integration, not a separate PyCat mobile application.*

</details>

## Get started

1. Check [Releases](https://github.com/nanhuayu/pycat/releases) for an available package. For the Windows portable build, extract the whole archive and run **`pycat.exe`** inside `pycat-windows-x64`.
2. Configure a working model connection in **Settings → Models & services**. PyCat does not include model credits; charges depend on the service you choose.
3. Pick a project folder and try a small set of sample materials before expanding the task.

Use the permission menu beside the composer to choose confirmation prompts or a narrower file scope. The current desktop defaults allow automatic execution and access to all local paths.

See the [user guide](docs/product/user-guide.md) for practical steps. Available operating-system packages are those actually listed on the release page.

## Before you begin

**Does everything stay on my computer?**

Projects and conversations are stored locally. Cloud models, online tools and messaging platforms receive the relevant content sent to them. A local model does not make every extension offline.

**Can it generate images or operate a browser immediately?**

Configure an image service or a browser/computer tool first. A chat model does not automatically provide these capabilities.

**Can I leave it entirely unattended?**

Define the scope and review important results. Models can misunderstand a request; file operations and external tools have real effects.

**How do I get help?**

Open an [issue](https://github.com/nanhuayu/pycat/issues) with your version, system and reproduction steps. Remove credentials and private material before sharing screenshots or logs.

PyCat is licensed under [AGPL-3.0-only](LICENSE). Contributions to protocol compatibility, native desktop interactions, remote workspaces, skills and reproducible task examples are welcome through [pull requests](https://github.com/nanhuayu/pycat/pulls), alongside feedback and practical use cases.
